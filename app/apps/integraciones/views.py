"""
API REST para sistemas externos: landings, n8n, o el sistema del centro médico.

    POST /api/v1/leads/          Alta de prospecto (deduplicado por teléfono, asignado y con automatizaciones)
    GET  /api/v1/leads/buscar/   Buscar por teléfono / email
    POST /api/v1/whatsapp/enviar/  Enviar un WhatsApp a un contacto (texto o plantilla)

Autenticación: header `X-API-Key: <clave>`.
"""
import json
import logging

from django.contrib import messages
from django.core.cache import cache
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from core import tenancy
from core.permisos import PermisoRequeridoMixin

from .models import ApiKey, LogIntegracion, hash_clave

logger = logging.getLogger('apps.integraciones')

CAMPOS_LEAD = {'nombre', 'telefono', 'email', 'dni', 'fecha_atencion', 'especialidad_atencion', 'localidad',
               'provincia', 'fecha_nacimiento', 'telefono_alternativo'}
LIMITE_POR_MINUTO = 120


def _ip(request):
    fwd = request.META.get('HTTP_X_FORWARDED_FOR', '')
    return fwd.split(',')[0].strip() if fwd else request.META.get('REMOTE_ADDR', '')


@method_decorator(csrf_exempt, name='dispatch')
class ApiBase(View):
    def dispatch(self, request, *args, **kwargs):
        clave = request.headers.get('X-API-Key') or request.GET.get('api_key', '')
        # Solo claves por integración (cada cliente las genera en su CRM). No hay clave global: en el SaaS
        # serviría para entrar a la API de cualquier cliente.
        self.api_key = ApiKey.objects.filter(hash=hash_clave(clave), activa=True).select_related('embudo').first() \
            if clave else None
        if self.api_key is None:
            return self._resp(request, {'error': 'API key inválida o ausente'}, 401)
        limite_key = f'api_rate_{self.api_key.pk}_{timezone.now():%Y%m%d%H%M}'
        if not cache.add(limite_key, 1, 70):
            try:
                if cache.incr(limite_key) > LIMITE_POR_MINUTO:
                    return self._resp(request, {'error': 'Demasiadas solicitudes, reintentá en un minuto'}, 429)
            except ValueError:
                pass
        try:
            self.data = json.loads(request.body) if request.body and 'json' in (request.content_type or '') \
                else request.POST.dict() or request.GET.dict()
        except ValueError:
            return self._resp(request, {'error': 'JSON inválido'}, 400)
        if self.api_key:
            ApiKey.objects.filter(pk=self.api_key.pk).update(ultimo_uso_at=timezone.now())
        return super().dispatch(request, *args, **kwargs)

    def _resp(self, request, data, status=200):
        try:
            LogIntegracion.objects.create(
                api_key=getattr(self, 'api_key', None), endpoint=request.path[:200], metodo=request.method,
                status=status, ip=_ip(request), request=request.body.decode(errors='ignore')[:3000],
                response=json.dumps(data, default=str)[:3000],
            )
        except Exception:  # pragma: no cover
            pass
        return JsonResponse(data, status=status, json_dumps_params={'ensure_ascii': False})


class LeadCrearView(ApiBase):
    def post(self, request):
        from apps.crm.importacion import _fecha
        from apps.crm.models import Embudo, Oportunidad
        from apps.crm.services import ErrorNegocio, ingresar_prospecto
        d = self.data
        embudo = None
        if d.get('embudo'):
            embudo = Embudo.objects.filter(activo=True).filter(
                **({'pk': d['embudo']} if str(d['embudo']).isdigit() else {'slug': d['embudo']})).first()
        embudo = embudo or (self.api_key.embudo if self.api_key else None) or Embudo.objects.filter(activo=True).first()
        if embudo is None:
            return self._resp(request, {'error': 'No hay embudo configurado'}, 400)
        datos = {k: d[k] for k in CAMPOS_LEAD if d.get(k) not in (None, '')}
        try:
            for campo in ('fecha_atencion', 'fecha_nacimiento'):
                if campo in datos:
                    datos[campo] = _fecha(datos[campo])
        except ValueError as e:
            return self._resp(request, {'error': str(e)}, 400)
        # Pauta: "origen" libre (si no es un canal del sistema), o pauta / utm_campaign / campaign / ad_name
        canales = dict(Oportunidad.ORIGEN_CHOICES)
        claves_pauta = ('pauta', 'campania', 'campaña', 'campaign', 'utm_campaign', 'ad_name', 'origen_pauta')
        origen_pauta = next((str(d[k]) for k in claves_pauta if d.get(k)), '')
        if not origen_pauta and d.get('origen') and d['origen'] not in canales:
            origen_pauta = str(d['origen'])
        from apps.crm.importacion import _valor_personalizado
        from apps.crm.models import CampoPersonalizado
        personalizados = {c.slug: c for c in CampoPersonalizado.activos() if not c.es_archivo}
        extra = {}
        for k, v in d.items():
            if k in CAMPOS_LEAD | {'embudo', 'etapa', 'agente', 'fuente', 'origen', 'valor', 'nota', *claves_pauta} \
                    or v in (None, ''):
                continue
            if k in personalizados:
                try:
                    extra[k] = _valor_personalizado(personalizados[k], v)
                except ValueError as e:
                    return self._resp(request, {'error': str(e), 'campo': k}, 400)
            else:
                extra[k] = str(v)[:500]
        if extra:
            datos['datos_extra'] = extra
        origen = d.get('origen') if d.get('origen') in dict(Oportunidad.ORIGEN_CHOICES) else Oportunidad.ORIGEN_API
        # Etapa inicial (id o nombre) y vendedor (usuario o email) opcionales: ej. el bot ya calificó al lead
        etapa = agente = None
        if d.get('etapa'):
            from apps.crm.models import Etapa
            etapas = Etapa.objects.filter(embudo=embudo, tipo=Etapa.TIPO_NORMAL)
            etapa = (etapas.filter(pk=d['etapa']) if str(d['etapa']).isdigit()
                     else etapas.filter(nombre__iexact=str(d['etapa']).strip())).first()
            if etapa is None:
                return self._resp(request, {'error': f'La etapa "{d["etapa"]}" no existe (abierta) en {embudo}',
                                            'etapas': list(etapas.values_list('nombre', flat=True))}, 400)
        if d.get('agente'):
            from apps.users.models import User
            agente = User.objects.filter(is_active=True).filter(
                Q(username__iexact=str(d['agente'])) | Q(email__iexact=str(d['agente']))).first()
            if agente is None:
                return self._resp(request, {'error': f'No existe el usuario "{d["agente"]}"'}, 400)
        try:
            res = ingresar_prospecto(datos, embudo, origen,
                                     fuente=str(d.get('fuente') or (self.api_key.fuente if self.api_key else '') or 'API'),
                                     origen_pauta=origen_pauta, etapa=etapa, agente=agente)
        except ErrorNegocio as e:
            return self._resp(request, {'error': str(e)}, 400)
        movida = False
        if etapa is not None and res.motivo == 'ya_activa' and res.oportunidad.etapa_id != etapa.pk:
            from apps.crm.services import mover_etapa
            try:
                movida = mover_etapa(res.oportunidad, etapa, None, nota='API', automatico=True) is not None
            except ErrorNegocio:
                movida = False
            res.oportunidad.refresh_from_db()
        if d.get('nota') and res.oportunidad:
            from apps.crm.services import agregar_nota
            from apps.crm.models import Actividad
            Actividad.objects.create(contacto=res.contacto, oportunidad=res.oportunidad, tipo=Actividad.TIPO_NOTA,
                                     texto=f'[API] {d["nota"]}'[:2000])
        op = res.oportunidad
        return self._resp(request, {
            'ok': True, 'contacto_id': res.contacto.pk, 'contacto_nuevo': res.contacto_nuevo,
            'oportunidad_id': op.pk if op else None, 'oportunidad_nueva': res.oportunidad_nueva,
            'motivo': res.motivo or None, 'etapa': str(op.etapa) if op else None, 'movida': movida,
            'pauta': str(op.pauta) if op and op.pauta_id else None,
            'agente': op.agente.display_name if op and op.agente else None,
        }, 201 if res.oportunidad_nueva else 200)


class LeadBuscarView(ApiBase):
    def get(self, request):
        from apps.crm.services import buscar_contacto
        c = buscar_contacto(self.data.get('telefono', ''), self.data.get('email', ''), self.data.get('dni', ''))
        if c is None:
            return self._resp(request, {'encontrado': False}, 404)
        return self._resp(request, {'encontrado': True, 'contacto': {
            'id': c.pk, 'nombre': c.nombre, 'telefono': c.telefono, 'email': c.email, 'no_contactar': c.no_contactar,
            'oportunidades': [{'id': o.pk, 'embudo': str(o.embudo), 'etapa': str(o.etapa), 'estado': o.estado,
                               'agente': o.agente.display_name if o.agente else None}
                              for o in c.oportunidades.select_related('embudo', 'etapa', 'agente')],
        }})


class EnviarWhatsAppView(ApiBase):
    def post(self, request):
        from apps.crm.services import buscar_contacto, oportunidad_activa_de
        from apps.whatsapp.models import LineaWhatsApp, Plantilla
        from apps.whatsapp.services import ErrorEnvio, conversacion_para_contacto, enviar_mensaje, linea_para
        c = buscar_contacto(self.data.get('telefono', ''))
        if c is None:
            return self._resp(request, {'error': 'Contacto no encontrado'}, 404)
        linea = LineaWhatsApp.objects.filter(pk=self.data.get('linea'), activa=True).first() if self.data.get('linea') else None
        op = oportunidad_activa_de(c)
        linea = linea or linea_para(c, op)
        if linea is None:
            return self._resp(request, {'error': 'No hay líneas de WhatsApp activas'}, 400)
        conv = conversacion_para_contacto(c, linea, agente=op.agente if op else None)
        plantilla = Plantilla.objects.filter(nombre=self.data['plantilla']).first() if self.data.get('plantilla') else None
        try:
            msg = enviar_mensaje(conv, texto=self.data.get('mensaje', ''), plantilla=plantilla, automatico=True)
        except ErrorEnvio as e:
            return self._resp(request, {'error': str(e)}, 409)
        return self._resp(request, {'ok': True, 'mensaje_id': msg.pk, 'linea': str(linea)}, 202)


# ── Gestión de claves (UI) ──────────────────────────────────────────────────

class ApiKeysView(PermisoRequeridoMixin, View):
    permiso = 'integraciones'

    def get(self, request):
        from apps.crm.models import Embudo
        from .models import ConfigSMS
        return render(request, 'integraciones/lista.html', {
            'sms': ConfigSMS.get(),
            'claves': ApiKey.objects.select_related('embudo'), 'embudos': Embudo.objects.filter(activo=True),
            'logs': LogIntegracion.objects.select_related('api_key')[:30],
            'base_url': tenancy.url_sitio(), 'nueva_clave': request.session.pop('nueva_clave', None),
        })

    def post(self, request):
        from apps.crm.models import Embudo
        if request.POST.get('accion') == 'sms':
            from .models import ConfigSMS
            c = ConfigSMS.get()
            c.activo = bool(request.POST.get('activo'))
            c.account_sid = request.POST.get('account_sid', '').strip()
            if request.POST.get('auth_token', '').strip():
                c.auth_token = request.POST['auth_token'].strip()
            c.numero = request.POST.get('numero', '').strip()
            c.save()
            messages.success(request, 'Configuración de SMS guardada.')
            return redirect('integraciones:lista')
        if request.POST.get('revocar'):
            ApiKey.objects.filter(pk=request.POST['revocar']).update(activa=False)
            messages.success(request, 'Clave revocada.')
        else:
            nombre = request.POST.get('nombre', '').strip()
            if not nombre:
                messages.error(request, 'Poné un nombre a la clave.')
                return redirect('integraciones:lista')
            obj, clave = ApiKey.generar(
                nombre=nombre, fuente=request.POST.get('fuente', '')[:150], creada_por=request.user,
                embudo=Embudo.objects.filter(pk=request.POST.get('embudo') or 0).first(),
            )
            request.session['nueva_clave'] = clave
            messages.success(request, 'Clave creada. Copiala ahora: por seguridad no se vuelve a mostrar.')
        return redirect('integraciones:lista')


@method_decorator(csrf_exempt, name='dispatch')
class SMSWebhookView(View):
    """Twilio → respuestas por SMS. URL: /integraciones/sms/webhook/<token>/ (se configura en el número de Twilio)."""

    def post(self, request, token):
        import secrets
        from django.http import HttpResponse
        from core.phone import normalizar_telefono
        from .models import ConfigSMS
        from . import sms
        if not secrets.compare_digest(token, ConfigSMS.get().webhook_token):
            return HttpResponse(status=403)
        tel = normalizar_telefono(request.POST.get('From', ''))
        if tel and request.POST.get('Body'):
            sms.recibir(tel, request.POST['Body'])
        return HttpResponse('<Response/>', content_type='text/xml')
