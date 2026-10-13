import logging
import mimetypes
import os
import uuid

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.files.storage import default_storage
from django.db.models import Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from core import tenancy
from core.permisos import PermisoRequeridoMixin
from core.utils import error, json_body, ok

from . import services
from .models import Conversacion, LineaWhatsApp, Mensaje, Plantilla, RespuestaRapida
from .proveedores import ErrorProveedor, get_proveedor
from .proveedores.evolution import nombre_instancia, prefijo_instancia

logger = logging.getLogger('apps.whatsapp')
MAX_ADJUNTO = 16 * 1024 * 1024


# ═══════════════════════════════════════════════════════════════════════════
# Webhooks (uno por línea: la clave secreta de la URL identifica la línea)
# ═══════════════════════════════════════════════════════════════════════════

@method_decorator(csrf_exempt, name='dispatch')
class WebhookView(View):
    def _linea(self, proveedor, key):
        linea = LineaWhatsApp.objects.filter(webhook_key=key, proveedor=proveedor).first()
        if linea is None:
            raise Http404
        return linea

    def get(self, request, proveedor, key):
        linea = self._linea(proveedor, key)
        if linea.proveedor == LineaWhatsApp.PROV_META:
            challenge = get_proveedor(linea).verificar_handshake(request)
            if challenge is not None:
                return HttpResponse(challenge)
            return HttpResponse('Token de verificación inválido', status=403)
        return HttpResponse('OK')

    def post(self, request, proveedor, key):
        linea = self._linea(proveedor, key)
        prov = get_proveedor(linea)
        if not prov.validar_webhook(request):
            logger.warning('Webhook con firma inválida en línea %s', linea)
            return HttpResponse('Firma inválida', status=403)
        try:
            res = prov.parsear_webhook(request)
        except Exception as e:
            logger.exception('Webhook ilegible en línea %s: %s', linea, e)
            return HttpResponse('OK')
        from .tasks import procesar_mensaje_entrante_task
        for msg in res.mensajes:
            procesar_mensaje_entrante_task.delay(linea.pk, msg.to_dict())
        if res.estados:
            services.actualizar_estados(res.estados)
            services.marcar_cambio_inbox()
        if res.conexion:
            LineaWhatsApp.objects.filter(pk=linea.pk).update(estado=res.conexion, estado_actualizado_at=timezone.now())
        if linea.proveedor == LineaWhatsApp.PROV_TWILIO:
            return HttpResponse('<Response></Response>', content_type='text/xml')
        return HttpResponse('OK')


# ═══════════════════════════════════════════════════════════════════════════
# Inbox
# ═══════════════════════════════════════════════════════════════════════════

def _convs(request):
    qs = Conversacion.objects.visibles_para(request.user).select_related('linea', 'contacto', 'agente')
    g = request.GET
    bandeja = g.get('bandeja', 'mias' if not request.user.ve_todo else 'todas')
    if bandeja == 'mias':
        qs = qs.filter(agente=request.user)
    elif bandeja == 'sin_asignar':
        qs = qs.filter(agente__isnull=True)
    if g.get('archivadas') == '1':
        qs = qs.filter(archivada=True)
    else:
        qs = qs.filter(archivada=False)
    if g.get('linea'):
        qs = qs.filter(linea_id=g['linea'])
    if g.get('no_leidos') == '1':
        qs = qs.filter(no_leidos__gt=0)
    if g.get('estado'):
        qs = qs.filter(estado=g['estado'])
    q = (g.get('q') or '').strip()
    if q:
        from core.phone import variantes_telefono
        digitos = ''.join(c for c in q if c.isdigit())
        filtro = Q(nombre_perfil__icontains=q) | Q(contacto__nombre__icontains=q)
        if len(digitos) >= 4:
            filtro |= Q(telefono__endswith=digitos[-8:]) | Q(telefono__in=variantes_telefono(q))
        qs = qs.filter(filtro)
    return qs.order_by('-ultimo_mensaje_at', '-pk'), bandeja


def _conv_visible(request, pk):
    conv = (Conversacion.objects.visibles_para(request.user).select_related('linea', 'contacto', 'agente')
            .filter(pk=pk).first())
    if conv is None:
        raise Http404
    return conv


class GuardarAdjuntoView(LoginRequiredMixin, View):
    """Adjunto recibido por WhatsApp → campo personalizado de tipo archivo del contacto (ej. "Recibo de sueldo")."""

    def _mensaje(self, request, pk):
        msg = get_object_or_404(Mensaje.objects.select_related('conversacion__contacto'), pk=pk)
        _conv_visible(request, msg.conversacion_id)
        return msg

    def get(self, request, pk):
        from apps.crm import archivos
        from apps.crm.services import oportunidad_activa_de
        msg = self._mensaje(request, pk)
        contacto = msg.conversacion.contacto
        op = oportunidad_activa_de(contacto) if contacto else None
        return ok(campos=[{'slug': c.slug, 'nombre': c.nombre}
                          for c in archivos.campos_archivo(op.embudo if op else None)])

    def post(self, request, pk):
        from apps.crm import archivos
        from apps.crm.models import CampoPersonalizado
        from apps.crm.services import oportunidad_activa_de
        msg = self._mensaje(request, pk)
        contacto = msg.conversacion.contacto
        if contacto is None:
            return error('El chat no está vinculado a un contacto.')
        data = json_body(request)
        campo = CampoPersonalizado.objects.filter(slug=data.get('campo') or '', tipo=CampoPersonalizado.TIPO_ARCHIVO,
                                                  activo=True).first()
        if campo is None:
            return error('Elegí el campo.')
        try:
            contenido = archivos.leer_media_local(msg.media_url)
            ext = mimetypes.guess_extension(msg.media_mime or '') or ''
            nombre = msg.media_filename or f'{campo.slug}_{timezone.localtime(msg.timestamp):%Y%m%d_%H%M}{ext}'
            archivos.adjuntar(contacto, campo, contenido, nombre, request.user, oportunidad_activa_de(contacto),
                              origen='desde WhatsApp')
        except archivos.ErrorArchivo as e:
            return error(str(e))
        return ok(mensaje=f'Guardado en «{campo.nombre}»')


class InboxView(LoginRequiredMixin, View):
    def get(self, request):
        from apps.users.models import User
        qs, bandeja = _convs(request)
        conv = None
        if request.GET.get('conv'):
            conv = _conv_visible(request, request.GET['conv'])
        ctx = {
            'conversaciones': list(qs[:80]), 'bandeja': bandeja, 'filtros': request.GET, 'conv': conv,
            'lineas': LineaWhatsApp.para_usuario(request.user), 'version': services.version_inbox(),
            'agentes': User.objects.filter(is_active=True) if request.user.tiene_permiso('reasignar') else [],
            'plantillas': Plantilla.objects.filter(activa=True), 'respuestas': RespuestaRapida.objects.filter(activa=True),
        }
        if conv:
            ctx.update(self._contexto_conv(request, conv))
        return render(request, 'whatsapp/inbox.html', ctx)

    @staticmethod
    def _contexto_conv(request, conv):
        from apps.crm.services import oportunidad_activa_de
        _marcar_leida(request, conv)
        op = oportunidad_activa_de(conv.contacto) if conv.contacto_id else None
        from apps.crm.forms import tipificaciones_de
        return {
            'mensajes': list(conv.mensajes.select_related('enviado_por').order_by('-timestamp', '-pk')[:80])[::-1],
            'op': op, 'etapas': op.embudo.etapas.order_by('orden') if op else [],
            'tipificaciones': tipificaciones_de(op.embudo) if op else [],
        }


def _marcar_leida(request, conv):
    if conv.no_leidos and (conv.agente_id == request.user.pk or conv.agente_id is None or request.user.ve_todo):
        cambios = {'no_leidos': 0}
        if conv.estado == Conversacion.ESTADO_PENDIENTE and conv.agente_id == request.user.pk:
            cambios['estado'] = Conversacion.ESTADO_ABIERTA
        Conversacion.objects.filter(pk=conv.pk).update(**cambios)
        services.invalidar_no_leidos([request.user.pk, conv.agente_id])


class ListaView(LoginRequiredMixin, View):
    """Polling liviano: si no hubo novedades desde la versión que tiene el navegador, responde vacío."""

    def get(self, request):
        version = services.version_inbox()
        if request.GET.get('v') and str(version) == request.GET['v']:
            return JsonResponse({'cambios': False, 'v': version})
        qs, bandeja = _convs(request)
        html = render_to_string('whatsapp/_lista.html', {'conversaciones': list(qs[:80]),
                                                          'conv_id': request.GET.get('conv')}, request=request)
        return JsonResponse({'cambios': True, 'v': version, 'html': html,
                             'no_leidos': services.contar_no_leidos_usuario(request.user)})


class MensajesView(LoginRequiredMixin, View):
    def get(self, request, pk):
        conv = _conv_visible(request, pk)
        despues = int(request.GET.get('despues') or 0)
        qs = conv.mensajes.select_related('enviado_por').order_by('timestamp', 'pk')
        if despues:
            qs = qs.filter(pk__gt=despues)
        else:
            qs = qs.order_by('-timestamp', '-pk')[:80]
        mensajes = list(qs)
        if not despues:
            mensajes.reverse()
        if mensajes and request.GET.get('leer') == '1':
            _marcar_leida(request, conv)
        # estados actualizados de los últimos salientes (entregado / leído)
        estados = dict(conv.mensajes.filter(direccion=Mensaje.DIR_SALIENTE).order_by('-pk')
                       .values_list('pk', 'status')[:30])
        html = render_to_string('whatsapp/_mensajes.html', {'mensajes': mensajes}, request=request)
        return JsonResponse({'html': html, 'ultimo': mensajes[-1].pk if mensajes else despues,
                             'estados': estados, 'ventana': conv.ventana_abierta})


class EnviarView(LoginRequiredMixin, View):
    """Envía en una conversación existente, o abre una nueva hacia un contacto (desde la ficha)."""

    def post(self, request, pk=None):
        from apps.crm.models import Contacto
        if pk:
            conv = _conv_visible(request, pk)
        else:
            contacto = get_object_or_404(Contacto.objects.visibles_para(request.user), pk=request.POST.get('contacto'))
            if not contacto.telefono:
                return error('El contacto no tiene teléfono.')
            linea = get_object_or_404(LineaWhatsApp.para_usuario(request.user), pk=request.POST.get('linea'))
            from apps.crm.services import oportunidad_activa_de
            op = oportunidad_activa_de(contacto)
            nueva = not Conversacion.objects.filter(linea=linea, telefono=contacto.telefono).exists()
            conv = services.conversacion_para_contacto(contacto, linea, agente=op.agente if op else request.user)
        if not conv.linea.usable_por(request.user):
            return error('No tenés acceso a esta línea.', 403)
        if conv.agente_id is None:
            Conversacion.objects.filter(pk=conv.pk).update(agente=request.user)
            conv.agente = request.user
        try:
            msg = self._enviar(request, conv)
        except services.ErrorEnvio as e:
            return error(str(e))
        if not pk and nueva:
            from apps.crm.models import Actividad
            Actividad.objects.create(contacto=contacto, oportunidad=op, tipo=Actividad.TIPO_WHATSAPP, usuario=request.user,
                                     texto=f'Inició el chat de WhatsApp desde la línea {conv.linea}')
        html = render_to_string('whatsapp/_mensajes.html', {'mensajes': [msg]}, request=request)
        return ok(html=html, id=msg.pk, conv=conv.pk)

    def _enviar(self, request, conv):
        texto = request.POST.get('texto', '')
        if request.POST.get('plantilla'):
            plantilla = get_object_or_404(Plantilla, pk=request.POST['plantilla'], activa=True)
            valores = request.POST.getlist('valores') or None
            return services.enviar_mensaje(conv, request.user, plantilla=plantilla, valores=valores)
        archivo = request.FILES.get('archivo')
        if archivo:
            if archivo.size > MAX_ADJUNTO:
                raise services.ErrorEnvio('El archivo supera los 16 MB.')
            carpeta = f'whatsapp/salientes/{timezone.localdate():%Y/%m}/{uuid.uuid4().hex}'
            if request.POST.get('voz') == '1':
                # Nota de voz grabada en el navegador: se pasa a OGG/Opus para que llegue como audio de WhatsApp
                from django.core.files.base import ContentFile
                from .audio import ErrorAudio, a_ogg_opus
                try:
                    ogg = a_ogg_opus(archivo.read())
                except ErrorAudio as e:
                    raise services.ErrorEnvio(str(e))
                ruta = default_storage.save(f'{carpeta}.ogg', ContentFile(ogg))
                return services.enviar_mensaje(conv, request.user, archivo_url=settings.MEDIA_URL + ruta,
                                               archivo_mime='audio/ogg', archivo_nombre='nota-de-voz.ogg')
            ext = os.path.splitext(archivo.name)[1].lower()[:8]
            ruta = default_storage.save(f'{carpeta}{ext}', archivo)
            url = settings.MEDIA_URL + ruta
            return services.enviar_mensaje(conv, request.user, texto=texto, archivo_url=url,
                                           archivo_mime=archivo.content_type or 'application/octet-stream',
                                           archivo_nombre=archivo.name)
        return services.enviar_mensaje(conv, request.user, texto=texto)


class ConversacionAccionView(LoginRequiredMixin, View):
    def post(self, request, pk, accion):
        from apps.users.models import User
        conv = _conv_visible(request, pk)
        data = json_body(request)
        cambios = {}
        if accion == 'tomar':
            cambios = {'agente': request.user, 'estado': Conversacion.ESTADO_ABIERTA}
        elif accion == 'asignar':
            if not request.user.tiene_permiso('reasignar'):
                return error('Sin permiso para reasignar.', 403)
            agente = get_object_or_404(User, pk=data.get('agente'), is_active=True)
            cambios = {'agente': agente}
            from apps.users.services import notificar
            if agente != request.user:
                notificar(agente, 'whatsapp', f'Te asignaron el chat con {conv.nombre_mostrar}', '',
                          f'/whatsapp/?conv={conv.pk}')
        elif accion == 'cerrar':
            cambios = {'estado': Conversacion.ESTADO_CERRADA, 'no_leidos': 0}
        elif accion == 'reabrir':
            cambios = {'estado': Conversacion.ESTADO_ABIERTA}
        elif accion == 'archivar':
            cambios = {'archivada': True, 'no_leidos': 0}
        elif accion == 'desarchivar':
            cambios = {'archivada': False}
        elif accion == 'no_leido':
            cambios = {'no_leidos': max(conv.no_leidos, 1)}
        else:
            return error('Acción desconocida', 404)
        Conversacion.objects.filter(pk=conv.pk).update(**cambios)
        services.invalidar_no_leidos([request.user.pk, conv.agente_id, getattr(cambios.get('agente'), 'pk', None)])
        services.marcar_cambio_inbox()
        return ok()


# ═══════════════════════════════════════════════════════════════════════════
# Líneas
# ═══════════════════════════════════════════════════════════════════════════

class LineaForm(forms.ModelForm):
    class Meta:
        model = LineaWhatsApp
        fields = ['nombre', 'proveedor', 'telefono', 'activa', 'orden', 'embudo', 'agentes', 'min_segundos_entre_envios',
                  'evolution_instancia', 'evolution_api_url', 'evolution_api_key',
                  'meta_phone_number_id', 'meta_waba_id', 'meta_access_token', 'meta_app_secret', 'meta_verify_token',
                  'meta_api_version', 'twilio_account_sid', 'twilio_auth_token', 'twilio_from']
        widgets = {'agentes': forms.CheckboxSelectMultiple,
                   'meta_access_token': forms.PasswordInput(render_value=True),
                   'meta_app_secret': forms.PasswordInput(render_value=True),
                   'twilio_auth_token': forms.PasswordInput(render_value=True),
                   'evolution_api_key': forms.PasswordInput(render_value=True)}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.users.models import User
        self.fields['agentes'].queryset = User.objects.filter(is_active=True)
        for nombre, f in self.fields.items():
            w = f.widget
            if isinstance(w, forms.CheckboxInput) or isinstance(w, forms.CheckboxSelectMultiple):
                w.attrs['class'] = 'form-check-input'
            elif isinstance(w, forms.Select):
                w.attrs['class'] = 'form-select'
            else:
                w.attrs['class'] = 'form-control'

    def clean(self):
        d = super().clean()
        p = d.get('proveedor')
        requeridos = {
            LineaWhatsApp.PROV_META: ['meta_phone_number_id', 'meta_access_token', 'meta_verify_token'],
            LineaWhatsApp.PROV_TWILIO: ['twilio_account_sid', 'twilio_auth_token', 'twilio_from'],
        }.get(p, [])
        for campo in requeridos:
            if not d.get(campo):
                self.add_error(campo, 'Obligatorio para este proveedor.')
        if p == LineaWhatsApp.PROV_EVOLUTION and d.get('evolution_api_url') and not d.get('evolution_api_key'):
            self.add_error('evolution_api_key', 'Si usás tu propio servidor de Evolution, cargá su API key.')
        if p == LineaWhatsApp.PROV_EVOLUTION and d.get('evolution_instancia') and not d.get('evolution_api_url'):
            # En el servidor compartido cada cliente solo puede usar instancias con su prefijo.
            prefijo = prefijo_instancia()
            if not d['evolution_instancia'].startswith(prefijo):
                self.add_error('evolution_instancia', f'Tiene que empezar con "{prefijo}" (o dejalo vacío).')
        if p == LineaWhatsApp.PROV_EVOLUTION and d.get('evolution_instancia'):
            otra = LineaWhatsApp.objects.filter(evolution_instancia=d['evolution_instancia']).exclude(pk=self.instance.pk)
            if otra.exists():
                self.add_error('evolution_instancia', 'Ya hay otra línea con esa instancia.')
        return d


class LineaListView(PermisoRequeridoMixin, View):
    permiso = 'lineas_whatsapp'

    def get(self, request):
        from django.db.models import Count
        lineas = LineaWhatsApp.objects.annotate(n_convs=Count('conversaciones')).select_related('embudo')
        return render(request, 'whatsapp/lineas.html', {'lineas': lineas})


class LineaEditarView(PermisoRequeridoMixin, View):
    permiso = 'lineas_whatsapp'

    def get(self, request, pk=None):
        linea = get_object_or_404(LineaWhatsApp, pk=pk) if pk else None
        return render(request, 'whatsapp/linea_form.html', {'linea': linea, 'form': LineaForm(instance=linea)})

    def post(self, request, pk=None):
        linea = get_object_or_404(LineaWhatsApp, pk=pk) if pk else None
        form = LineaForm(request.POST, instance=linea)
        if not form.is_valid():
            return render(request, 'whatsapp/linea_form.html', {'linea': linea, 'form': form})
        if linea is None:
            alcanzado, limite = tenancy.limite_alcanzado('lineas_whatsapp')
            if alcanzado:
                form.add_error(None, f'Tu plan permite hasta {limite} líneas de WhatsApp. Pedinos ampliar el plan.')
                return render(request, 'whatsapp/linea_form.html', {'linea': linea, 'form': form})
        creando = linea is None
        linea = form.save()
        from core.auditoria import auditar
        from apps.users.models import RegistroAuditoria
        auditar(RegistroAuditoria.CREDENCIAL, request=request,
                objeto=f'Línea WhatsApp «{linea.nombre}» ({linea.proveedor})',
                detalle='alta' if creando else 'edición')
        if linea.proveedor == LineaWhatsApp.PROV_EVOLUTION and not linea.evolution_instancia:
            linea.evolution_instancia = nombre_instancia(linea)
            linea.save(update_fields=['evolution_instancia'])
        if linea.proveedor == LineaWhatsApp.PROV_META and linea.min_segundos_entre_envios > 2 and pk is None:
            LineaWhatsApp.objects.filter(pk=linea.pk).update(min_segundos_entre_envios=1)
        from .tasks import actualizar_estado_lineas
        try:
            estado, detalle = get_proveedor(linea).consultar_estado()
            LineaWhatsApp.objects.filter(pk=linea.pk).update(estado=estado, estado_detalle=detalle,
                                                             estado_actualizado_at=timezone.now())
        except Exception as e:  # pragma: no cover
            logger.warning('No se pudo consultar estado de %s: %s', linea, e)
        messages.success(request, 'Línea guardada.' + (' Ahora escaneá el QR para vincularla.'
                                                     if linea.proveedor == LineaWhatsApp.PROV_EVOLUTION else ''))
        return redirect('whatsapp:linea_editar', pk=linea.pk)


class LineaConexionView(PermisoRequeridoMixin, View):
    permiso = 'lineas_whatsapp'

    def get(self, request, pk):
        """Estado actual + QR (Evolution) para el polling del modal de vinculación."""
        linea = get_object_or_404(LineaWhatsApp, pk=pk)
        prov = get_proveedor(linea)
        estado, detalle = prov.consultar_estado()
        LineaWhatsApp.objects.filter(pk=linea.pk).update(estado=estado, estado_detalle=detalle,
                                                         estado_actualizado_at=timezone.now())
        qr = None
        if linea.proveedor == LineaWhatsApp.PROV_EVOLUTION and estado != LineaWhatsApp.ESTADO_CONECTADA \
                and request.GET.get('qr') == '1':
            try:
                qr = prov.obtener_qr()
            except ErrorProveedor as e:
                detalle = str(e)
        return JsonResponse({'estado': estado, 'estado_display': dict(LineaWhatsApp.ESTADO_CHOICES).get(estado),
                             'detalle': detalle, 'qr': qr})

    def post(self, request, pk):
        linea = get_object_or_404(LineaWhatsApp, pk=pk)
        accion = request.POST.get('accion')
        prov = get_proveedor(linea)
        try:
            if accion == 'desconectar' and linea.proveedor == LineaWhatsApp.PROV_EVOLUTION:
                prov.desconectar()
                LineaWhatsApp.objects.filter(pk=pk).update(estado=LineaWhatsApp.ESTADO_DESCONECTADA)
                messages.success(request, 'Línea desvinculada.')
            elif accion == 'webhook' and linea.proveedor == LineaWhatsApp.PROV_EVOLUTION:
                prov.asegurar_instancia()
                prov.configurar_webhook()
                messages.success(request, 'Webhook configurado en Evolution.')
            elif accion == 'sync_plantillas' and linea.proveedor == LineaWhatsApp.PROV_META:
                n = prov.sincronizar_plantillas()
                messages.success(request, f'{n} plantillas actualizadas desde Meta.')
        except ErrorProveedor as e:
            messages.error(request, str(e))
        return redirect('whatsapp:linea_editar', pk=pk)


# ═══════════════════════════════════════════════════════════════════════════
# Plantillas y respuestas rápidas
# ═══════════════════════════════════════════════════════════════════════════

class PlantillaForm(forms.ModelForm):
    class Meta:
        model = Plantilla
        fields = ['nombre', 'cuerpo', 'linea', 'categoria', 'idioma', 'meta_nombre', 'twilio_content_sid', 'activa']
        widgets = {'cuerpo': forms.Textarea(attrs={'rows': 5})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for f in self.fields.values():
            w = f.widget
            w.attrs['class'] = ('form-check-input' if isinstance(w, forms.CheckboxInput)
                                else 'form-select' if isinstance(w, forms.Select) else 'form-control')


class PlantillaListView(PermisoRequeridoMixin, View):
    permiso = 'plantillas'

    def get(self, request):
        return render(request, 'whatsapp/plantillas.html', {
            'plantillas': Plantilla.objects.select_related('linea'),
            'respuestas': RespuestaRapida.objects.all(),
            'lineas_meta': LineaWhatsApp.objects.filter(proveedor=LineaWhatsApp.PROV_META, activa=True),
        })


class PlantillaEditarView(PermisoRequeridoMixin, View):
    permiso = 'plantillas'

    def get(self, request, pk=None):
        p = get_object_or_404(Plantilla, pk=pk) if pk else None
        return self._render(request, p, PlantillaForm(instance=p))

    def post(self, request, pk=None):
        p = get_object_or_404(Plantilla, pk=pk) if pk else None
        if request.POST.get('eliminar') and p:
            p.delete()
            messages.success(request, 'Plantilla eliminada.')
            return redirect('whatsapp:plantillas')
        form = PlantillaForm(request.POST, instance=p)
        if not form.is_valid():
            return self._render(request, p, form)
        p = form.save(commit=False)
        validos = {k for k, _ in Plantilla.variables_disponibles()}
        p.variables = [v if v in validos else '' for v in request.POST.getlist('variables')]
        p.save()
        if request.POST.get('enviar_meta'):
            linea = LineaWhatsApp.objects.filter(pk=request.POST['enviar_meta'], proveedor=LineaWhatsApp.PROV_META).first()
            if linea:
                try:
                    get_proveedor(linea).crear_plantilla(p)
                    Plantilla.objects.filter(pk=p.pk).update(meta_estado=Plantilla.ESTADO_PENDIENTE)
                    messages.success(request, 'Plantilla enviada a revisión de Meta.')
                except ErrorProveedor as e:
                    messages.error(request, f'Meta rechazó el envío: {e}')
        messages.success(request, 'Plantilla guardada.')
        return redirect('whatsapp:plantillas')

    def _render(self, request, p, form):
        return render(request, 'whatsapp/plantilla_form.html', {
            'p': p, 'form': form, 'variables_disp': Plantilla.variables_disponibles(),
            'lineas_meta': LineaWhatsApp.objects.filter(proveedor=LineaWhatsApp.PROV_META, activa=True),
        })


class RespuestaRapidaView(PermisoRequeridoMixin, View):
    permiso = 'plantillas'

    def post(self, request):
        if request.POST.get('eliminar'):
            RespuestaRapida.objects.filter(pk=request.POST['eliminar']).delete()
        else:
            pk = request.POST.get('pk')
            obj = RespuestaRapida.objects.filter(pk=pk).first() if pk else RespuestaRapida()
            from django.utils.text import slugify
            obj.atajo = slugify(request.POST.get('atajo', ''))[:30]
            obj.titulo = request.POST.get('titulo', '')[:100]
            obj.texto = request.POST.get('texto', '')
            if obj.atajo and obj.texto:
                if RespuestaRapida.objects.filter(atajo=obj.atajo).exclude(pk=obj.pk).exists():
                    messages.error(request, f'Ya existe el atajo /{obj.atajo}.')
                else:
                    obj.save()
                    messages.success(request, f'Respuesta /{obj.atajo} guardada.')
        return redirect('whatsapp:plantillas')
