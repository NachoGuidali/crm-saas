"""
Panel de la plataforma (schema público, dominio principal): alta y gestión de clientes y planes,
y la pantalla "Ingresar" que lleva a cada usuario al subdominio de su empresa.

Solo entran los superusuarios del schema público (los usuarios de cada cliente viven en su propio schema
y no existen acá).
"""
import json
import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.mixins import UserPassesTestMixin
from django.core.exceptions import ValidationError
from django.db.models import Sum
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django_tenants.utils import get_public_schema_name, tenant_context

from core.auditoria import auditar
from core.tenancy import dominio_para

from . import cobros, mercadopago, services
from .forms import ClienteForm, IngresarForm, LinkPagoMPForm, NuevoClienteForm, PagoForm, PlanForm, SuscripcionMPForm
from apps.users.models import RegistroAuditoria
from .models import Cliente, Dominio, Pago, Plan

logger = logging.getLogger('apps.clientes')

FILTROS = [
    ('', 'Todos'), ('vencidos', 'Vencidos'), ('por_vencer', 'Por vencer'), ('prueba', 'En prueba'),
    ('suspendidos', 'Suspendidos'), ('baja', 'De baja'),
]


def _url_cliente(request, dominio, ruta='/usuarios/login/'):
    # El esquema y el puerto salen de SITE_URL, no del request: detrás de nginx, request.get_port() devuelve
    # el puerto interno de gunicorn (8000) y agregaría ":8000" a la URL pública.
    from urllib.parse import urlsplit
    partes = urlsplit(settings.SITE_URL)
    puerto = f':{partes.port}' if partes.port else ''
    return f'{partes.scheme}://{dominio}{puerto}{ruta}'


class PanelMixin(UserPassesTestMixin):
    login_url = reverse_lazy('panel:login')

    def test_func(self):
        return self.request.user.is_authenticated and self.request.user.is_superuser


class PanelLoginView(auth_views.LoginView):
    template_name = 'clientes/login.html'
    redirect_authenticated_user = True

    def get_success_url(self):
        return self.get_redirect_url() or reverse_lazy('panel:clientes')

    def post(self, request, *args, **kwargs):
        from apps.users import services as user_services
        self._ip = user_services.ip_de(request)
        self._username = request.POST.get('username', '').strip()
        if user_services.login_bloqueado(self._username, self._ip):
            messages.error(request, 'Demasiados intentos fallidos. Esperá unos minutos.')
            return self.render_to_response(self.get_context_data(form=self.get_form()))
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        from apps.users import services as user_services
        from apps.users.models import RegistroAuditoria
        user_services.limpiar_login_fallido(self._username, self._ip)
        auditar(RegistroAuditoria.LOGIN, usuario=form.get_user(), ip=self._ip, detalle='panel de plataforma')
        return super().form_valid(form)

    def form_invalid(self, form):
        from apps.users import services as user_services
        from apps.users.models import RegistroAuditoria
        user_services.registrar_login_fallido(self._username, self._ip)
        auditar(RegistroAuditoria.LOGIN_FALLIDO, username=self._username, ip=self._ip, detalle='panel de plataforma')
        return super().form_invalid(form)


class PanelLogoutView(View):
    def post(self, request):
        from django.contrib.auth import logout
        logout(request)
        return redirect('panel:login')


class _PanelPasswordForm(PasswordChangeForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for campo in self.fields.values():
            campo.widget.attrs.setdefault('class', 'form-control')


class PanelPasswordView(PanelMixin, auth_views.PasswordChangeView):
    """Cambio de contraseña del superusuario del panel (plataforma)."""
    template_name = 'clientes/password.html'
    form_class = _PanelPasswordForm
    success_url = reverse_lazy('panel:clientes')

    def form_valid(self, form):
        messages.success(self.request, 'Contraseña actualizada.')
        return super().form_valid(form)


class ClienteListView(PanelMixin, View):
    def get(self, request):
        todos = list(Cliente.objects.exclude(schema_name=get_public_schema_name())
                     .select_related('plan').prefetch_related('domains'))
        hoy = timezone.localdate()
        con_acceso = [c for c in todos if c.estado in Cliente.ESTADOS_CON_ACCESO]
        resumen = {
            'activos': sum(c.estado == Cliente.ESTADO_ACTIVO for c in todos),
            'prueba': sum(c.estado == Cliente.ESTADO_PRUEBA for c in todos),
            'mensual': sum((c.precio for c in todos if c.estado == Cliente.ESTADO_ACTIVO), 0),
            'vencidos': sum(c.estado_cobro in ('en_gracia', 'vencido') for c in con_acceso),
            'por_vencer': sum(c.estado_cobro == 'por_vencer' for c in con_acceso),
            'suspendidos': sum(c.estado == Cliente.ESTADO_SUSPENDIDO for c in todos),
            'cobrado_mes': Pago.objects.filter(anulado=False, fecha__year=hoy.year, fecha__month=hoy.month)
                                       .aggregate(t=Sum('monto'))['t'] or 0,
        }
        filtro = request.GET.get('filtro', '')
        clientes = {
            'vencidos': [c for c in todos if c.estado_cobro in ('en_gracia', 'vencido')],
            'por_vencer': [c for c in todos if c.estado_cobro == 'por_vencer'],
            'prueba': [c for c in todos if c.estado == Cliente.ESTADO_PRUEBA],
            'suspendidos': [c for c in todos if c.estado == Cliente.ESTADO_SUSPENDIDO],
            'baja': [c for c in todos if c.estado == Cliente.ESTADO_BAJA],
        }.get(filtro, [c for c in todos if c.estado != Cliente.ESTADO_BAJA])
        # Primero lo que hay que mirar: vencidos, por vencer, después el resto por vencimiento.
        orden = {'vencido': 0, 'en_gracia': 1, 'por_vencer': 2, 'sin_fecha': 3, 'al_dia': 4, '': 5}
        clientes.sort(key=lambda c: (orden[c.estado_cobro], c.vencimiento or hoy.replace(year=hoy.year + 50), c.nombre))
        for c in clientes:
            c.dominio = next((d.domain for d in c.domains.all() if d.is_primary), '')
            c.url = _url_cliente(request, c.dominio) if c.dominio else ''
            c.uso = _uso(c)
        return render(request, 'clientes/lista.html', {
            'clientes': clientes, 'resumen': resumen, 'filtro': filtro, 'filtros': FILTROS,
            'moneda': settings.SAAS_MONEDA, 'dias_gracia': settings.SAAS_DIAS_GRACIA,
        })


def _uso(cliente):
    from apps.users.models import User
    from apps.whatsapp.models import LineaWhatsApp
    try:
        with tenant_context(cliente):
            return {'usuarios': User.objects.filter(is_active=True).count(), 'lineas': LineaWhatsApp.objects.count()}
    except Exception:  # schema roto o a medio crear: que no tumbe el panel
        return None


class ClienteNuevoView(PanelMixin, View):
    def get(self, request):
        return render(request, 'clientes/nuevo.html', {'form': NuevoClienteForm()})

    def post(self, request):
        form = NuevoClienteForm(request.POST)
        if not form.is_valid():
            return render(request, 'clientes/nuevo.html', {'form': form})
        d = form.cleaned_data
        try:
            cliente, password = services.crear_cliente(
                nombre=d['nombre'], subdominio=d['subdominio'], plan=d['plan'], estado=d['estado'],
                admin_nombre=d['admin_nombre'], admin_email=d['admin_email'], admin_usuario=d['admin_usuario'],
                admin_password=d['admin_password'] or None, contacto_nombre=d['admin_nombre'],
                contacto_telefono=d['contacto_telefono'],
            )
        except ValidationError as e:
            form.add_error('subdominio', e.messages)
            return render(request, 'clientes/nuevo.html', {'form': form})
        # Las credenciales se muestran una sola vez (no se guardan en ningún lado).
        request.session['panel_credenciales'] = {
            'usuario': d['admin_usuario'], 'password': password, 'temporal': not d['admin_password'],
        }
        messages.success(request, f'Cliente "{cliente}" creado.')
        return redirect('panel:cliente', pk=cliente.pk)


class ClienteDetalleView(PanelMixin, View):
    def _cliente(self, pk):
        return get_object_or_404(Cliente.objects.exclude(schema_name=get_public_schema_name()), pk=pk)

    def get(self, request, pk):
        cliente = self._cliente(pk)
        return self._render(request, cliente, ClienteForm(instance=cliente))

    def post(self, request, pk):
        cliente = self._cliente(pk)
        accion = request.POST.get('accion', '')
        volver = redirect('panel:cliente', pk=cliente.pk)
        if accion == 'suspender':
            cobros.suspender(cliente, Cliente.SUSPENSION_MANUAL)
            auditar(RegistroAuditoria.CLIENTE_SUSPENSION, request=request, objeto=cliente.nombre, detalle='manual')
            messages.success(request, 'Cliente suspendido.')
            return volver
        if accion == 'reactivar':
            cobros.reactivar(cliente)
            auditar(RegistroAuditoria.CLIENTE_REACTIVACION, request=request, objeto=cliente.nombre)
            messages.success(request, 'Cliente reactivado.')
            if cliente.estado_cobro in ('en_gracia', 'vencido') and cliente.suspension_automatica:
                messages.warning(request, 'Ojo: sigue vencido. Si no registrás un pago (o corrés "Pagado hasta"), '
                                          'la revisión diaria lo vuelve a suspender.')
            return volver
        if accion == 'pago':
            form_pago = PagoForm(request.POST, prefix='pago')
            if not form_pago.is_valid():
                return self._render(request, cliente, form_pago=form_pago)
            d = form_pago.cleaned_data
            pago = cobros.registrar_pago(
                cliente, monto=d['monto'], medio=d['medio'], meses=d['meses'], desde=d['periodo_desde'],
                fecha=d['fecha'], referencia=d['referencia'], notas=d['notas'], reactivar=d['reactivar'],
                registrado_por=request.user.get_username(),
            )
            auditar(RegistroAuditoria.PAGO, request=request, objeto=cliente.nombre,
                    detalle=f'{d["medio"]} {d["monto"]} x{d["meses"]}m → hasta {pago.periodo_hasta:%d/%m/%Y}')
            messages.success(request, f'Pago registrado. Tiene el servicio pago hasta el {pago.periodo_hasta:%d/%m/%Y}.')
            return volver
        if accion == 'anular_pago':
            pago = get_object_or_404(Pago, pk=request.POST.get('pago'), cliente=cliente, anulado=False)
            cliente = cobros.anular_pago(pago)
            messages.success(request, 'Pago anulado. Pagado hasta: '
                             + (f'{cliente.pagado_hasta:%d/%m/%Y}' if cliente.pagado_hasta else 'sin pagos') + '.')
            return volver
        if accion.startswith('mp_'):
            return self._accion_mp(request, cliente, accion) or volver
        form = ClienteForm(request.POST, instance=cliente)
        if not form.is_valid():
            return self._render(request, cliente, form)
        form.save()
        messages.success(request, 'Cliente guardado.')
        return volver

    def _accion_mp(self, request, cliente, accion):
        try:
            if accion == 'mp_suscripcion':
                form_mp = SuscripcionMPForm(request.POST, prefix='mp')
                if not form_mp.is_valid():
                    return self._render(request, cliente, form_mp=form_mp)
                mercadopago.crear_suscripcion(cliente, form_mp.cleaned_data['email_pagador'])
                messages.success(request, 'Suscripción creada. Mandale el link al cliente para que autorice el débito.')
            elif accion == 'mp_actualizar':
                estado = mercadopago.actualizar_suscripcion(cliente)
                messages.info(request, f'Estado de la suscripción: {ESTADOS_MP.get(estado, estado)}.')
            elif accion == 'mp_cancelar':
                mercadopago.cancelar_suscripcion(cliente)
                messages.success(request, 'Suscripción cancelada: Mercado Pago no le cobra más.')
            elif accion == 'mp_link':
                form_link = LinkPagoMPForm(request.POST, prefix='link')
                if not form_link.is_valid():
                    return self._render(request, cliente, form_link=form_link)
                meses = form_link.cleaned_data['meses']
                request.session['panel_link_mp'] = {'url': mercadopago.crear_link_pago(cliente, meses), 'meses': meses}
        except mercadopago.ErrorMP as e:
            messages.error(request, str(e))
        return None

    def _render(self, request, cliente, form=None, form_pago=None, form_mp=None, form_link=None):
        dominio = cliente.dominio_principal
        return render(request, 'clientes/detalle.html', {
            'cliente': cliente, 'form': form or ClienteForm(instance=cliente), 'dominio': dominio,
            'url': _url_cliente(request, dominio) if dominio else '', 'uso': _uso(cliente),
            'credenciales': request.session.pop('panel_credenciales', None),
            'form_pago': form_pago or PagoForm(prefix='pago', initial={
                'monto': cliente.precio, 'periodo_desde': cobros.desde_sugerido(cliente),
                'fecha': timezone.localdate(), 'reactivar': True}),
            'pagos': cliente.pagos.all()[:50],
            'mp_configurado': mercadopago.configurado(),
            'form_mp': form_mp or SuscripcionMPForm(prefix='mp', initial={
                'email_pagador': cliente.mp_email_pagador or cliente.contacto_email}),
            'form_link': form_link or LinkPagoMPForm(prefix='link'),
            'link_mp': request.session.pop('panel_link_mp', None),
            'estado_mp': ESTADOS_MP.get(cliente.mp_suscripcion_estado, cliente.mp_suscripcion_estado),
            'moneda': settings.SAAS_MONEDA, 'dias_gracia': settings.SAAS_DIAS_GRACIA,
        })


ESTADOS_MP = {'pending': 'esperando que el cliente autorice', 'authorized': 'activa (debita todos los meses)',
              'paused': 'pausada', 'cancelled': 'cancelada'}


class PagoListView(PanelMixin, View):
    """Todos los pagos de un mes (para la contabilidad)."""

    def get(self, request):
        hoy = timezone.localdate()
        try:
            anio, mes = (int(x) for x in request.GET.get('mes', f'{hoy:%Y-%m}').split('-'))
            desde = hoy.replace(year=anio, month=mes, day=1)
        except ValueError:
            desde = hoy.replace(day=1)
        pagos = Pago.objects.filter(fecha__year=desde.year, fecha__month=desde.month).select_related('cliente')
        validos = pagos.filter(anulado=False)
        return render(request, 'clientes/pagos.html', {
            'pagos': pagos, 'mes': desde, 'anterior': cobros.sumar_meses(desde, -1),
            'siguiente': cobros.sumar_meses(desde, 1) if desde < hoy.replace(day=1) else None,
            'total': validos.aggregate(t=Sum('monto'))['t'] or 0,
            'por_medio': [(dict(Pago.MEDIO_CHOICES).get(m['medio'], m['medio']), m['t'])
                          for m in validos.values('medio').annotate(t=Sum('monto')).order_by('-t')],
            'moneda': settings.SAAS_MONEDA,
        })


class PlanListView(PanelMixin, View):
    def get(self, request):
        return render(request, 'clientes/planes.html', {'planes': Plan.objects.all(), 'form': PlanForm()})


class PlanEditarView(PanelMixin, View):
    def get(self, request, pk=None):
        plan = get_object_or_404(Plan, pk=pk) if pk else None
        return render(request, 'clientes/plan_form.html', {'plan': plan, 'form': PlanForm(instance=plan)})

    def post(self, request, pk=None):
        plan = get_object_or_404(Plan, pk=pk) if pk else None
        form = PlanForm(request.POST, instance=plan)
        if not form.is_valid():
            return render(request, 'clientes/plan_form.html', {'plan': plan, 'form': form})
        form.save()
        messages.success(request, 'Plan guardado.')
        return redirect('panel:planes')


class AuditoriaPanelView(PanelMixin, View):
    """Bitácora de acciones del panel de la plataforma (schema público)."""

    def get(self, request):
        from django.core.paginator import Paginator
        from django.db.models import Q
        qs = RegistroAuditoria.objects.select_related('usuario').all()
        accion = request.GET.get('accion', '')
        if accion:
            qs = qs.filter(accion=accion)
        q = request.GET.get('q', '').strip()
        if q:
            qs = qs.filter(Q(usuario_desc__icontains=q) | Q(objeto__icontains=q) | Q(ip__icontains=q))
        pagina = Paginator(qs, 50).get_page(request.GET.get('page'))
        return render(request, 'clientes/auditoria.html', {
            'pagina': pagina, 'accion': accion, 'q': q, 'acciones': RegistroAuditoria.ACCION_CHOICES})


class HomeView(View):
    """Home pública de supregcrm.com: presentación del producto para indexar y publicitar."""

    def get(self, request):
        numero = (settings.SAAS_WHATSAPP_VENTAS or '').strip()
        wa_url = ''
        if numero:
            from urllib.parse import quote
            msg = quote(f'Hola, quiero más info sobre {settings.CRM_NOMBRE}.')
            wa_url = f'https://wa.me/{numero}?text={msg}'
        return render(request, 'clientes/home.html', {
            'wa_url': wa_url,
            'SAAS_DESCRIPCION': settings.SAAS_DESCRIPCION,
        })


class IngresarView(View):
    """Login central: el usuario indica su empresa y lo mandamos al login de su subdominio."""

    def get(self, request):
        return render(request, 'clientes/ingresar.html', {'form': IngresarForm()})

    def post(self, request):
        form = IngresarForm(request.POST)
        if form.is_valid():
            sub = form.cleaned_data['empresa'].strip().lower()
            sub = sub.split('.', 1)[0]  # por si pegan "acme.tucrm.com"
            dom = Dominio.objects.filter(domain=dominio_para(sub)).select_related('tenant').first()
            if dom and not dom.tenant.es_publico:
                return HttpResponseRedirect(_url_cliente(request, dom.domain))
            form.add_error('empresa', 'No encontramos esa empresa. Revisá el subdominio que te enviamos.')
        return render(request, 'clientes/ingresar.html', {'form': form})


@method_decorator(csrf_exempt, name='dispatch')
class MercadoPagoWebhookView(View):
    """
    Notificaciones de Mercado Pago (dominio principal). Formato nuevo: ?type=...&data.id=... + JSON
    {"type": ..., "data": {"id": ...}}; formato IPN viejo: ?topic=...&id=...
    Responde 500 si la API de MP falla, así MP reintenta más tarde.
    """

    def post(self, request):
        if not mercadopago.firma_valida(request):
            logger.warning('Webhook de Mercado Pago con firma inválida')
            return HttpResponse(status=401)
        try:
            cuerpo = json.loads(request.body or b'{}')
        except ValueError:
            cuerpo = {}
        tipo = request.GET.get('type') or request.GET.get('topic') or cuerpo.get('type') or cuerpo.get('topic') or ''
        recurso = (request.GET.get('data.id') or request.GET.get('id')
                   or (cuerpo.get('data') or {}).get('id') or '')
        try:
            mercadopago.procesar_notificacion(tipo, str(recurso))
        except mercadopago.ErrorMP:
            logger.exception('Webhook MP %s %s', tipo, recurso)
            return HttpResponse(status=500)
        return HttpResponse('ok')

    def get(self, request):
        return HttpResponse('ok')
