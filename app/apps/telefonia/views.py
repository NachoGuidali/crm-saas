import json
import logging
import secrets

from django import forms
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from core import tenancy
from core.permisos import PermisoRequeridoMixin
from core.utils import error, json_body, ok, paginar, query_sin_page

from . import services
from .client import ErrorAnura, get_cliente
from .models import (AgenteDiscador, CampaniaContacto, CampaniaDiscado, ConfigAnura, EventoAnura, InternoAnura,
                     Llamada, RutaAnura)

logger = logging.getLogger('apps.telefonia')


# ═══════════════════════════════════════════════════════════════════════════
# Endpoints de la spec (Módulo 1 y 2)
# ═══════════════════════════════════════════════════════════════════════════

@method_decorator(csrf_exempt, name='dispatch')
class AnuraWebhookView(View):
    """
    POST /api/integrations/anura/webhook  (CallHook de Anura)
    El token va en la URL (?token=… o /telefonia/webhook/<token>/) o en el header X-Webhook-Token.
    Responde 200 enseguida y procesa en segundo plano (Anura reintenta si tardamos).
    """

    def post(self, request, token=None):
        config = ConfigAnura.get()
        bearer = request.headers.get('Authorization', '')
        bearer = bearer[7:].strip() if bearer.lower().startswith('bearer ') else ''
        recibido = token or request.GET.get('token') or request.headers.get('X-Webhook-Token', '') or bearer
        if not secrets.compare_digest(str(recibido), config.webhook_token):
            return HttpResponse('Token inválido', status=403)
        try:
            payload = json.loads(request.body) if request.body and request.content_type != 'application/x-www-form-urlencoded' \
                else request.POST.dict()
        except ValueError:
            payload = request.POST.dict()
        eventos = payload if isinstance(payload, list) else [payload]
        from .tasks import procesar_evento_llamada_task
        for ev in eventos:
            if isinstance(ev, dict) and ev:
                procesar_evento_llamada_task.delay(ev, 'webhook')
        return JsonResponse({'ok': True, 'recibidos': len(eventos)})


class DialView(LoginRequiredMixin, View):
    """POST /api/telephony/dial — Click2call sin cambiar de pantalla."""

    def post(self, request):
        from apps.crm.models import Contacto, Oportunidad
        data = json_body(request)
        op = contacto = None
        op_id = data.get('oportunidadId') or data.get('caseId')
        if op_id:
            op = get_object_or_404(Oportunidad.objects.visibles_para(request.user), pk=op_id)
            contacto = op.contacto
        elif data.get('contactoId'):
            contacto = get_object_or_404(Contacto.objects.visibles_para(request.user), pk=data['contactoId'])
        numero = data.get('phoneNumber') or (contacto.telefono if contacto else '')
        try:
            llamada = services.discar(request.user, numero, oportunidad=op, contacto=contacto)
        except services.ErrorTelefonia as e:
            return error(str(e))
        return ok(llamada=services.estado_llamada_json(llamada), callId=llamada.call_id or llamada.anura_uuid)


class SoftphoneView(LoginRequiredMixin, View):
    """Credenciales de la terminal CRM del agente logueado (solo las suyas, nunca escritas en el HTML)."""

    def post(self, request):
        datos = services.credenciales_softphone(request.user)
        if datos is None:
            return error('No tenés el teléfono del CRM configurado.', 404)
        resp = ok(**datos)
        resp['Cache-Control'] = 'no-store'
        return resp


class QuienLlamaView(LoginRequiredMixin, View):
    def get(self, request):
        return ok(**services.quien_llama(request.user, request.GET.get('numero', '')))


@method_decorator(csrf_exempt, name='dispatch')
class SoftphoneRegistroView(LoginRequiredMixin, View):
    """Respaldo: el teléfono (JsSIP) atendió una llamada; se registra por si no llega el webhook de Anura."""

    def post(self, request):
        reason = _chequear_csrf(request)
        if reason:
            return reason
        data = json_body(request)
        llamada = services.registrar_desde_softphone(
            request.user, numero=data.get('numero', ''), direccion=data.get('direccion', 'IN'))
        return ok(llamada=services.estado_llamada_json(llamada))


@method_decorator(csrf_exempt, name='dispatch')
class SoftphoneCortoView(LoginRequiredMixin, View):
    """Respaldo: el teléfono cortó; se cierra la llamada si Anura todavía no mandó el END."""

    def post(self, request):
        reason = _chequear_csrf(request)
        if reason:
            return reason
        services.cerrar_desde_softphone(request.user)
        return ok()


def _chequear_csrf(request):
    from django.middleware.csrf import CsrfViewMiddleware
    reason = CsrfViewMiddleware(lambda r: None).process_view(request, None, (), {})
    return error('CSRF inválido', 403) if reason is not None else None


class MarcoView(LoginRequiredMixin, View):
    """Marco fijo: el teléfono queda arriba y las pantallas del CRM cargan adentro sin cortar la llamada."""

    def get(self, request):
        destino = request.GET.get('ir') or '/'
        if not destino.startswith('/') or destino.startswith('//'):
            destino = '/'
        return render(request, 'telefonia/marco.html', {'destino': destino})


class CalificarView(LoginRequiredMixin, View):
    """POST /api/telephony/calificar/<id> — resultado de la gestión al terminar la llamada."""

    def post(self, request, pk):
        from datetime import datetime
        from apps.crm.models import ResultadoGestion
        qs = Llamada.objects.select_related('oportunidad__embudo', 'oportunidad__contacto', 'contacto', 'agente')
        if not request.user.ve_todo:
            qs = qs.filter(agente=request.user)
        llamada = get_object_or_404(qs, pk=pk)
        data = json_body(request)
        resultado = ResultadoGestion.objects.filter(pk=data.get('resultado') or 0, activo=True).first()
        if resultado is None:
            return error('Elegí el resultado de la llamada.')
        fecha = None
        if data.get('volver_a_llamar'):
            try:
                fecha = timezone.make_aware(datetime.fromisoformat(data['volver_a_llamar']))
            except (ValueError, TypeError):
                return error('Fecha inválida.')
        try:
            services.calificar_llamada(llamada, resultado, request.user, nota=data.get('nota', ''),
                                       volver_a_llamar=fecha)
        except services.ErrorTelefonia as e:
            return error(str(e))
        return ok(siguiente=services.pendiente_json(services.pendiente_de_calificar(request.user)))


@method_decorator(csrf_exempt, name='dispatch')
class HangupView(LoginRequiredMixin, View):
    """PUT /api/telephony/hangup/{id} — acepta el id interno de la llamada o el callId de Anura."""

    def put(self, request, call_id):
        from django.middleware.csrf import CsrfViewMiddleware
        # PUT también valida CSRF (csrf_exempt solo evita el chequeo automático que no soporta PUT con fetch).
        reason = CsrfViewMiddleware(lambda r: None).process_view(request, None, (), {})
        if reason is not None:
            return error('CSRF inválido', 403)
        filtro = Q(call_id=call_id) | Q(anura_uuid=call_id)
        if str(call_id).isdigit():
            filtro |= Q(pk=int(call_id))
        llamada = Llamada.objects.filter(filtro).order_by('-inicio_at').first()
        if llamada is None:
            return error('Llamada no encontrada', 404)
        try:
            services.colgar(request.user, llamada, descartar=bool(request.GET.get('descartar')))
        except services.ErrorTelefonia as e:
            return error(str(e))
        return ok()

    post = put


class EstadoView(LoginRequiredMixin, View):
    """Estado de la llamada activa del usuario (lo consulta el widget flotante, en cualquier pantalla)."""

    def get(self, request):
        llamada = services.llamada_activa(request.user)
        if llamada is None and request.GET.get('ultima'):
            llamada = Llamada.objects.filter(pk=request.GET['ultima'], agente=request.user).first()
        return JsonResponse({'llamada': services.estado_llamada_json(llamada)})


# ═══════════════════════════════════════════════════════════════════════════
# Configuración
# ═══════════════════════════════════════════════════════════════════════════

class ConfigForm(forms.ModelForm):
    class Meta:
        model = ConfigAnura
        fields = ['activo', 'modo_demo', 'webphone_url', 'click2dial_url', 'click2dial_token', 'api_url', 'auth_tipo', 'api_token',
                  'api_usuario', 'api_password',
                  'auth_header_nombre', 'tenant_id', 'account_id', 'embudo_entrantes', 'crear_tarjeta_salientes', 'solo_eventos_propios',
                  'tarea_llamada_perdida', 'descargar_grabaciones', 'polling_activo', 'softphone_activo',
                  'softphone_ws', 'sip_dominio', 'sip_puerto']
        widgets = {'api_token': forms.PasswordInput(render_value=True),
                   'click2dial_token': forms.PasswordInput(render_value=True),
                   'api_password': forms.PasswordInput(render_value=True)}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for f in self.fields.values():
            w = f.widget
            w.attrs['class'] = ('form-check-input' if isinstance(w, forms.CheckboxInput)
                                else 'form-select' if isinstance(w, forms.Select) else 'form-control')


class ConfigView(PermisoRequeridoMixin, View):
    permiso = 'telefonia'

    def get(self, request):
        from apps.crm.models import Embudo
        from apps.users.models import User
        config = ConfigAnura.objects.get_or_create(pk=1)[0]
        return render(request, 'telefonia/config.html', {
            'config': config, 'form': ConfigForm(instance=config),
            'internos': InternoAnura.objects.select_related('usuario'),
            'rutas': RutaAnura.objects.select_related('embudo', 'agente'),
            'usuarios': User.objects.filter(is_active=True), 'embudos': Embudo.objects.filter(activo=True),
            'webhook_spec_url': request.build_absolute_uri('/api/integrations/anura/webhook') + f'?token={config.webhook_token}',
            'plantilla_webhook': services.PLANTILLA_WEBHOOK,
            'webhook_host': tenancy.host_sitio().split('/')[0],
            'webhook_https': tenancy.usa_https(),
            'avisos': EventoAnura.objects.all()[:50],
        })

    def post(self, request):
        from apps.users.models import User
        config = ConfigAnura.objects.get_or_create(pk=1)[0]
        accion = request.POST.get('accion', 'guardar')
        if accion == 'guardar':
            form = ConfigForm(request.POST, instance=config)
            if form.is_valid():
                form.save()
                services.invalidar_softphone(InternoAnura.objects.values_list('usuario_id', flat=True))
                messages.success(request, 'Configuración de Anura guardada.')
            else:
                messages.error(request, form.errors.as_text())
        elif accion == 'interno':
            usuario = get_object_or_404(User, pk=request.POST.get('usuario'))
            interno = request.POST.get('interno', '').strip()
            alias = request.POST.get('alias', '')
            conflicto = InternoAnura.conflicto([interno] + InternoAnura.limpiar_alias(alias), usuario) if interno else None
            if not interno:
                messages.error(request, 'Indicá el interno.')
            elif conflicto:
                messages.error(request, f'"{conflicto[0]}" ya está asignado a {conflicto[1].usuario}.')
            else:
                obj, _ = InternoAnura.objects.update_or_create(usuario=usuario, defaults={
                    'interno': interno, 'alias': alias, 'activo': True})
                if request.POST.get('sip_usuario', '').strip():  # vacío = deja las credenciales SIP como estaban
                    obj.sip_usuario = request.POST['sip_usuario'].strip()
                    if request.POST.get('sip_password'):
                        obj.sip_password = request.POST['sip_password']
                    obj.save(update_fields=['sip_usuario', 'sip_password_cifrada'])
                cache.delete(f'tiene_interno_{usuario.pk}')
                services.invalidar_softphone([usuario.pk])
                messages.success(request, f'Interno {interno} asignado a {usuario}.')
        elif accion == 'borrar_interno':
            obj = InternoAnura.objects.filter(pk=request.POST.get('id')).first()
            if obj:
                cache.delete(f'tiene_interno_{obj.usuario_id}')
                services.invalidar_softphone([obj.usuario_id])
                obj.delete()
        elif accion == 'ruta':
            from apps.crm.models import Embudo
            embudo = get_object_or_404(Embudo, pk=request.POST.get('embudo'))
            RutaAnura.objects.update_or_create(
                tipo=request.POST.get('tipo', 'did'), valor=request.POST.get('valor', '').strip(),
                defaults={'embudo': embudo, 'descripcion': request.POST.get('descripcion', ''),
                          'agente_id': request.POST.get('agente') or None})
            messages.success(request, 'Ruta guardada.')
        elif accion == 'borrar_ruta':
            RutaAnura.objects.filter(pk=request.POST.get('id')).delete()
        elif accion == 'regenerar_token':
            config.webhook_token = secrets.token_urlsafe(24)
            config.save()
            messages.warning(request, 'Token regenerado: actualizá la URL del CallHook en Anura.')
        elif accion == 'probar':
            try:
                get_cliente(config).verificar_token()
                messages.success(request, 'Modo demo: OK.' if config.modo_demo
                                 else 'Token de Click2Dial válido: Anura lo aceptó.')
            except ErrorAnura as e:
                messages.error(request, f'Falló la verificación: {e}')
        elif accion == 'simular_entrante':
            numero = request.POST.get('numero', '').strip() or '+5491155550000'
            estado = request.POST.get('estado', 'NOANSWER')
            call_id = f'sim-{secrets.token_hex(6)}'
            base = {'callId': call_id, 'direction': 'IN', 'calling': numero, 'called': request.POST.get('did', ''),
                    'terminal': request.POST.get('interno', '')}
            services.procesar_evento_llamada({**base, 'event': 'START'}, origen='demo')
            services.procesar_evento_llamada({**base, 'event': 'END', 'status': estado,
                                              'billSeconds': 95 if estado == 'ANSWER' else 0}, origen='demo')
            messages.success(request, f'Llamada entrante simulada desde {numero} ({estado}). Revisá el tablero y las notificaciones.')
        return redirect('telefonia:config')


def _llamadas_csv(qs):
    import csv
    resp = HttpResponse(content_type='text/csv; charset=utf-8')
    resp['Content-Disposition'] = f'attachment; filename="llamadas_{timezone.localdate():%Y%m%d}.csv"'
    resp.write('\ufeff')
    w = csv.writer(resp, delimiter=';')
    w.writerow(['Fecha', 'Dirección', 'Número', 'Contacto', 'Oportunidad', 'Agente', 'Estado', 'Resultado', 'Nota',
                'Duración (s)', 'ID grabación'])
    for l in qs.iterator(chunk_size=2000):
        w.writerow([timezone.localtime(l.inicio_at).strftime('%d/%m/%Y %H:%M'), l.get_direccion_display(), l.numero,
                    l.contacto.nombre if l.contacto_id else '', l.oportunidad_id or '',
                    l.agente.display_name if l.agente_id else '', l.get_estado_display(),
                    l.resultado.nombre if l.resultado_id else ('Sin calificar' if l.estado == Llamada.ESTADO_ATENDIDA
                                                               and not l.calificada_at and l.oportunidad_id else ''),
                    l.resultado_nota, l.duracion_seg, l.id_grabacion])
    return resp


class LlamadasView(LoginRequiredMixin, View):
    def get(self, request):
        qs = Llamada.objects.select_related('agente', 'contacto', 'oportunidad', 'resultado')
        if not request.user.ve_todo:
            qs = qs.filter(agente=request.user)
        g = request.GET
        if g.get('agente') and request.user.ve_todo:
            qs = qs.filter(agente_id=g['agente'])
        if g.get('direccion'):
            qs = qs.filter(direccion=g['direccion'])
        if g.get('estado'):
            qs = qs.filter(estado=g['estado'])
        if g.get('resultado') == 'sin':
            qs = qs.filter(services.q_sin_calificar())
        elif g.get('resultado'):
            qs = qs.filter(resultado_id=g['resultado'])
        if g.get('desde'):
            qs = qs.filter(inicio_at__date__gte=g['desde'])
        if g.get('hasta'):
            qs = qs.filter(inicio_at__date__lte=g['hasta'])
        if g.get('q'):
            digitos = ''.join(c for c in g['q'] if c.isdigit())
            qs = qs.filter(Q(contacto__nombre__icontains=g['q']) | Q(numero__endswith=digitos[-8:] or '§')
                           | Q(call_id=g['q'].strip()) | Q(anura_uuid=g['q'].strip()))
        from apps.crm.models import ResultadoGestion
        from apps.users.models import User
        if g.get('csv'):
            return _llamadas_csv(qs.order_by('-inicio_at'))
        return render(request, 'telefonia/llamadas.html', {
            'page': paginar(request, qs.order_by('-inicio_at'), 50), 'filtros': g, 'query': query_sin_page(request),
            'agentes': User.objects.filter(is_active=True), 'estados': Llamada.ESTADO_CHOICES,
            'resultados': ResultadoGestion.objects.all(),
        })


# ═══════════════════════════════════════════════════════════════════════════
# Discador progresivo (Módulo 3)
# ═══════════════════════════════════════════════════════════════════════════

class CampaniaForm(forms.ModelForm):
    DIAS = [(0, 'Lun'), (1, 'Mar'), (2, 'Mié'), (3, 'Jue'), (4, 'Vie'), (5, 'Sáb'), (6, 'Dom')]
    dias = forms.TypedMultipleChoiceField(choices=DIAS, coerce=int, required=False, widget=forms.CheckboxSelectMultiple)

    class Meta:
        model = CampaniaDiscado
        fields = ['nombre', 'embudo', 'agentes', 'max_intentos', 'minutos_entre_intentos', 'hora_desde', 'hora_hasta',
                  'segundos_entre_llamadas']
        widgets = {'agentes': forms.CheckboxSelectMultiple,
                   'hora_desde': forms.TimeInput(attrs={'type': 'time'}, format='%H:%M'),
                   'hora_hasta': forms.TimeInput(attrs={'type': 'time'}, format='%H:%M')}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.users.models import User
        self.fields['agentes'].queryset = User.objects.filter(is_active=True, interno_anura__activo=True)
        self.fields['agentes'].help_text = 'Solo aparecen usuarios con interno de Anura.'
        self.fields['dias'].initial = self.instance.dias or [0, 1, 2, 3, 4]
        for f in self.fields.values():
            w = f.widget
            w.attrs['class'] = ('form-check-input' if isinstance(w, (forms.CheckboxInput, forms.CheckboxSelectMultiple))
                                else 'form-select' if isinstance(w, forms.Select) else 'form-control')

    def save(self, commit=True):
        self.instance.dias = self.cleaned_data.get('dias') or []
        return super().save(commit)


class CampaniaListView(LoginRequiredMixin, View):
    def get(self, request):
        qs = CampaniaDiscado.objects.select_related('embudo').prefetch_related('agentes')
        if not request.user.tiene_permiso('discador'):
            qs = qs.filter(agentes=request.user)
        sesion = AgenteDiscador.objects.filter(agente=request.user).select_related('campania').first()
        return render(request, 'telefonia/campanias.html', {'campanias': qs, 'sesion': sesion})


class CampaniaEditarView(PermisoRequeridoMixin, View):
    permiso = 'discador'

    def get(self, request, pk=None):
        c = get_object_or_404(CampaniaDiscado, pk=pk) if pk else None
        return render(request, 'telefonia/campania_form.html', {'c': c, 'form': CampaniaForm(instance=c)})

    def post(self, request, pk=None):
        c = get_object_or_404(CampaniaDiscado, pk=pk) if pk else None
        form = CampaniaForm(request.POST, instance=c)
        if not form.is_valid():
            return render(request, 'telefonia/campania_form.html', {'c': c, 'form': form})
        c = form.save(commit=False)
        if not c.pk:
            c.creada_por = request.user
        c.save()
        form.save_m2m()
        messages.success(request, 'Campaña guardada. Cargá los contactos desde el detalle o desde la lista de oportunidades.')
        return redirect('telefonia:campania', pk=c.pk)


class CampaniaDetalleView(LoginRequiredMixin, View):
    def get(self, request, pk):
        c = get_object_or_404(CampaniaDiscado.objects.select_related('embudo'), pk=pk)
        if not (request.user.tiene_permiso('discador') or c.agentes.filter(pk=request.user.pk).exists()):
            raise PermissionDenied
        contactos = c.contactos.select_related('contacto', 'agente', 'oportunidad')
        if request.GET.get('estado'):
            contactos = contactos.filter(estado=request.GET['estado'])
        if request.GET.get('json'):
            return JsonResponse({'resumen': c.resumen(), 'estado': c.estado, 'conectados': [
                {'agente': s.agente.display_name, 'pausa': s.en_pausa,
                 'en_llamada': Llamada.objects.filter(agente=s.agente, estado__in=Llamada.ESTADOS_VIVOS).exists()}
                for s in c.sesiones.select_related('agente')]})
        etapas = c.embudo.etapas.filter(tipo='normal').order_by('orden') if c.embudo else []
        return render(request, 'telefonia/campania_detalle.html', {
            'c': c, 'resumen': c.resumen(), 'page': paginar(request, contactos.order_by('estado', '-prioridad', 'pk'), 50),
            'sesiones': c.sesiones.select_related('agente'), 'estados': CampaniaContacto.ESTADO_CHOICES,
            'mi_sesion': AgenteDiscador.objects.filter(agente=request.user, campania=c).first(),
            'etapas': etapas, 'filtros': request.GET, 'query': query_sin_page(request),
            'en_horario': c.en_horario(), 'config': ConfigAnura.get(),
        })

    def post(self, request, pk):
        c = get_object_or_404(CampaniaDiscado, pk=pk)
        accion = request.POST.get('accion')
        es_gestor = request.user.tiene_permiso('discador')
        es_agente = c.agentes.filter(pk=request.user.pk).exists()
        if accion in ('conectar', 'desconectar', 'pausa') and not (es_agente or es_gestor):
            raise PermissionDenied
        if accion not in ('conectar', 'desconectar', 'pausa') and not es_gestor:
            raise PermissionDenied

        if accion == 'conectar':
            if not services.usuario_tiene_interno(request.user):
                messages.error(request, 'Necesitás un interno de Anura para usar el discador.')
            else:
                AgenteDiscador.objects.update_or_create(agente=request.user, defaults={
                    'campania': c, 'en_pausa': False, 'libre_desde': timezone.now(), 'conectado_at': timezone.now()})
                messages.success(request, 'Conectado al discador: te vamos a pasar la próxima llamada cuando estés libre.')
        elif accion == 'desconectar':
            AgenteDiscador.objects.filter(agente=request.user).delete()
        elif accion == 'pausa':
            s = AgenteDiscador.objects.filter(agente=request.user, campania=c).first()
            if s:
                s.en_pausa = not s.en_pausa
                s.libre_desde = timezone.now()
                s.save(update_fields=['en_pausa', 'libre_desde'])
        elif accion in ('activar', 'pausar', 'finalizar'):
            nuevo = {'activar': CampaniaDiscado.ESTADO_ACTIVA, 'pausar': CampaniaDiscado.ESTADO_PAUSADA,
                     'finalizar': CampaniaDiscado.ESTADO_FINALIZADA}[accion]
            c.estado = nuevo
            c.save(update_fields=['estado'])
            if nuevo == CampaniaDiscado.ESTADO_FINALIZADA:
                AgenteDiscador.objects.filter(campania=c).delete()
            messages.success(request, f'Campaña {c.get_estado_display().lower()}.')
        elif accion == 'cargar':
            from apps.crm.models import Oportunidad
            ops = Oportunidad.objects.filter(estado=Oportunidad.ESTADO_ABIERTA)
            if c.embudo_id:
                ops = ops.filter(embudo=c.embudo)
            if request.POST.get('etapa'):
                ops = ops.filter(etapa_id=request.POST['etapa'])
            if request.POST.get('sin_contacto'):
                ops = ops.filter(contacto_efectivo_at__isnull=True)
            n, omitidos = services.cargar_en_campania(c, ops, usuario=request.user)
            messages.success(request, f'{n} contactos cargados ({omitidos} omitidos: repetidos o "no contactar").')
        elif accion == 'reintentar_todos':
            n = c.contactos.filter(estado=CampaniaContacto.ESTADO_NO_CONTESTA).update(
                estado=CampaniaContacto.ESTADO_REINTENTAR, intentos=0, proximo_intento_at=timezone.now())
            messages.success(request, f'{n} contactos vuelven a la cola.')
        return redirect('telefonia:campania', pk=c.pk)
