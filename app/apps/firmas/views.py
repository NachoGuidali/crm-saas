import json

from django import forms
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.files.storage import default_storage
from django.db.models import Count, Q
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views import View

from core.permisos import PermisoRequeridoMixin

from . import services
from .models import FormularioFirma, SolicitudFirma


class ModuloMixin:
    """El cliente tiene que tener habilitada la función extra 'firmas'."""

    def dispatch(self, request, *args, **kwargs):
        if not services.habilitado():
            raise Http404('El módulo de firmas electrónicas no está habilitado.')
        return super().dispatch(request, *args, **kwargs)


class ConfigMixin(ModuloMixin, PermisoRequeridoMixin):
    permiso = 'firmas'


# ── Configuración: formularios ─────────────────────────────────────────────

class FormularioForm(forms.ModelForm):
    class Meta:
        model = FormularioFirma
        fields = ['nombre', 'titulo', 'documento', 'logo', 'color_fondo', 'color_principal', 'texto_final',
                  'vence_dias', 'enviar_copia', 'asunto_email', 'texto_email', 'texto_whatsapp', 'activo']
        widgets = {'documento': forms.Textarea(attrs={'rows': 7}), 'texto_email': forms.Textarea(attrs={'rows': 4}),
                   'texto_whatsapp': forms.Textarea(attrs={'rows': 2}),
                   'color_fondo': forms.TextInput(attrs={'type': 'color'}),
                   'color_principal': forms.TextInput(attrs={'type': 'color'})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for f in self.fields.values():
            w = f.widget
            if isinstance(w, forms.CheckboxInput):
                w.attrs['class'] = 'form-check-input'
            elif w.attrs.get('type') == 'color':
                w.attrs['class'] = 'form-control form-control-color'
            else:
                w.attrs['class'] = 'form-control'

    def clean_logo(self):
        logo = self.cleaned_data.get('logo')
        if logo and hasattr(logo, 'size') and logo.size > 2 * 1024 * 1024:
            raise forms.ValidationError('El logo no puede pesar más de 2 MB.')
        return logo


class FormularioListView(ConfigMixin, View):
    def get(self, request):
        formularios = FormularioFirma.objects.annotate(
            n_enviadas=Count('solicitudes'),
            n_firmadas=Count('solicitudes', filter=Q(solicitudes__estado=SolicitudFirma.ESTADO_FIRMADA)))
        recientes = SolicitudFirma.objects.select_related('formulario', 'contacto', 'oportunidad')[:30]
        return render(request, 'firmas/lista.html', {'formularios': formularios, 'recientes': recientes})


class FormularioEditarView(ConfigMixin, View):
    def _render(self, request, form, campos, obj=None):
        return render(request, 'firmas/editar.html', {
            'form': form, 'obj': obj, 'campos_json': json.dumps(campos, ensure_ascii=False).replace('</', '<\\/'),
            'tipos_json': json.dumps(FormularioFirma.TIPOS_CAMPO, ensure_ascii=False),
            'prellenar_json': json.dumps(services.opciones_prellenar(), ensure_ascii=False).replace('</', '<\\/'),
        })

    def get(self, request, pk=None):
        obj = get_object_or_404(FormularioFirma, pk=pk) if pk else None
        campos = obj.campos if obj else [
            {'id': 'c1', 'tipo': 'texto', 'etiqueta': 'Nombre y apellido', 'requerido': True, 'prellenar': 'nombre'},
            {'id': 'c2', 'tipo': 'texto', 'etiqueta': 'DNI', 'requerido': True, 'prellenar': 'dni'},
            {'id': 'c3', 'tipo': 'acepto', 'etiqueta': 'Declaro que los datos son correctos', 'requerido': True},
            {'id': 'c4', 'tipo': 'firma', 'etiqueta': 'Firma', 'requerido': True},
        ]
        return self._render(request, FormularioForm(instance=obj), campos, obj)

    def post(self, request, pk=None):
        obj = get_object_or_404(FormularioFirma, pk=pk) if pk else None
        if obj and request.POST.get('eliminar'):
            if obj.solicitudes.exists():
                obj.activo = False
                obj.save(update_fields=['activo'])
                messages.info(request, 'El formulario ya se usó: quedó desactivado (los firmados se conservan).')
            else:
                obj.delete()
                messages.success(request, 'Formulario eliminado.')
            return redirect('firmas:lista')
        form = FormularioForm(request.POST, request.FILES, instance=obj)
        try:
            campos = services.normalizar_campos(json.loads(request.POST.get('campos_json') or '[]'))
        except ValueError:
            campos = []
        if form.is_valid() and not any(c['tipo'] == FormularioFirma.T_FIRMA for c in campos):
            form.add_error(None, 'Agregá al menos un campo de tipo "Firma".')
        if not form.is_valid():
            return self._render(request, form, campos, obj)
        f = form.save(commit=False)
        f.campos = campos
        f.save()
        messages.success(request, 'Formulario guardado.')
        if request.POST.get('preview'):
            return redirect('firmas:editar', pk=f.pk)
        return redirect('firmas:lista')


class VistaPreviaView(ConfigMixin, View):
    """Cómo lo ve el lead (con datos de ejemplo). No se puede enviar."""

    def get(self, request, pk):
        f = get_object_or_404(FormularioFirma, pk=pk)
        snap = {'titulo': f.titulo, 'documento': f.documento, 'campos': f.campos, 'logo': f.logo.name if f.logo else '',
                'color_fondo': f.color_fondo, 'color_principal': f.color_principal, 'texto_final': f.texto_final,
                'iniciales': {}}
        return render(request, 'firmas/publico.html', {'snap': snap, 'vista_previa': True, 'errores': {}, 'datos': {}})


# ── Desde la ficha del lead ────────────────────────────────────────────────

def _op_visible(request, pk):
    from apps.crm.models import Oportunidad
    return get_object_or_404(Oportunidad.objects.visibles_para(request.user).select_related('contacto'), pk=pk)


class EnviarView(ModuloMixin, LoginRequiredMixin, View):
    def post(self, request, pk):
        op = _op_visible(request, pk)
        formulario = get_object_or_404(FormularioFirma, pk=request.POST.get('formulario'), activo=True)
        canales = request.POST.getlist('canal')
        try:
            sol = services.crear_solicitud(formulario, op, usuario=request.user)
        except services.ErrorFirma as e:
            messages.error(request, str(e))
            return redirect(op.get_absolute_url())
        enviados, errores = services.enviar(sol, canales, usuario=request.user)
        for e in errores:
            messages.warning(request, e)
        if enviados:
            messages.success(request, 'Formulario enviado: ' + ' · '.join(enviados))
        else:
            messages.info(request, f'Link generado (copialo desde la ficha): {sol.url}')
        return redirect(op.get_absolute_url() + '#firmas')


class AnularView(ModuloMixin, LoginRequiredMixin, View):
    def post(self, request, pk):
        sol = get_object_or_404(SolicitudFirma, pk=pk)
        _op_visible(request, sol.oportunidad_id)
        try:
            services.anular(sol, request.user)
            messages.success(request, 'Link anulado.')
        except services.ErrorFirma as e:
            messages.error(request, str(e))
        return redirect(sol.oportunidad.get_absolute_url() + '#firmas')


def _pdf_response(sol, descarga=True):
    if not sol.pdf or not default_storage.exists(sol.pdf):
        raise Http404
    return FileResponse(default_storage.open(sol.pdf, 'rb'), as_attachment=descarga,
                        filename=sol.pdf.rsplit('/', 1)[-1], content_type='application/pdf')


class PdfView(ModuloMixin, LoginRequiredMixin, View):
    def get(self, request, pk):
        sol = get_object_or_404(SolicitudFirma, pk=pk, estado=SolicitudFirma.ESTADO_FIRMADA)
        _op_visible(request, sol.oportunidad_id)
        return _pdf_response(sol, descarga=request.GET.get('ver') != '1')


# ── Página pública (la abre el lead, sin login) ────────────────────────────

def _ip(request):
    fwd = request.META.get('HTTP_X_FORWARDED_FOR', '')
    return fwd.split(',')[0].strip() if fwd else request.META.get('REMOTE_ADDR')


class PublicoView(View):
    def _sol(self, token):
        if not services.habilitado():
            raise Http404
        return get_object_or_404(SolicitudFirma.objects.select_related('formulario', 'contacto', 'oportunidad'),
                                 token=token)

    def _render(self, request, sol, errores=None, datos=None, status=200):
        datos = datos if datos is not None else {f'f_{k}': v for k, v in (sol.snapshot.get('iniciales') or {}).items()}
        return render(request, 'firmas/publico.html', {
            'sol': sol, 'snap': sol.snapshot, 'errores': errores or {}, 'datos': datos,
        }, status=status)

    def get(self, request, token):
        sol = self._sol(token)
        if sol.firmable:
            services.marcar_abierta(sol)
        return self._render(request, sol)

    def post(self, request, token):
        sol = self._sol(token)
        if not sol.firmable:
            return self._render(request, sol, status=409)
        try:
            sol = services.completar(sol, request.POST, ip=_ip(request),
                                     dispositivo=request.META.get('HTTP_USER_AGENT', ''))
        except services.ErrorFirma as e:
            errores = e.args[0] if isinstance(e.args[0], dict) else {'_': str(e)}
            return self._render(request, sol, errores=errores, datos=request.POST, status=400)
        return redirect('firmas_publico:formulario', token=sol.token)


class PdfPublicoView(View):
    def get(self, request, token):
        if not services.habilitado():
            raise Http404
        sol = get_object_or_404(SolicitudFirma, token=token, estado=SolicitudFirma.ESTADO_FIRMADA)
        return _pdf_response(sol)


# ── API para el bot / integraciones ────────────────────────────────────────

def _api_base():
    from apps.integraciones.views import ApiBase
    return ApiBase


class FirmaApiView(_api_base()):
    """
    POST /api/v1/firmas/   {telefono|email|dni|oportunidad_id, formulario: id o nombre, enviar: "email"|"whatsapp"|"ambos"|""}
        → {ok, id, url, estado}: crea el link para ese lead (y opcionalmente lo envía). El bot puede mandar `url` en el chat.
    GET  /api/v1/firmas/?id=… (o token=…) → estado de la solicitud.
    """

    def get(self, request):
        if not services.habilitado():
            return self._resp(request, {'error': 'El módulo de firmas no está habilitado.'}, 403)
        filtro = {'pk': self.data['id']} if str(self.data.get('id', '')).isdigit() else {'token': self.data.get('token', '')}
        sol = SolicitudFirma.objects.filter(**filtro).select_related('formulario').first()
        if sol is None:
            return self._resp(request, {'error': 'No existe.'}, 404)
        return self._resp(request, self._datos(sol))

    def post(self, request):
        from apps.crm.models import Oportunidad
        from apps.crm.services import buscar_contacto, oportunidad_activa_de
        if not services.habilitado():
            return self._resp(request, {'error': 'El módulo de firmas no está habilitado.'}, 403)
        d = self.data
        op = None
        if str(d.get('oportunidad_id', '')).isdigit():
            op = Oportunidad.objects.filter(pk=d['oportunidad_id']).select_related('contacto').first()
        else:
            c = buscar_contacto(d.get('telefono') or d.get('phone') or '', d.get('email', ''), d.get('dni', ''))
            op = oportunidad_activa_de(c) if c else None
        if op is None:
            return self._resp(request, {'error': 'No se encontró una oportunidad activa para ese lead.'}, 404)
        ref = str(d.get('formulario') or '').strip()
        qs = FormularioFirma.objects.filter(activo=True)
        formulario = (qs.filter(pk=ref).first() if ref.isdigit() else None) or qs.filter(nombre__iexact=ref).first()
        if formulario is None:
            return self._resp(request, {'error': 'Formulario inexistente o inactivo.'}, 404)
        try:
            sol = services.crear_solicitud(formulario, op, origen='api')
        except services.ErrorFirma as e:
            return self._resp(request, {'error': str(e)}, 409)
        enviar = str(d.get('enviar') or '').lower()
        canales = ['email', 'whatsapp'] if enviar == 'ambos' else [enviar] if enviar in ('email', 'whatsapp') else []
        enviados, errores = services.enviar(sol, canales) if canales else ([], [])
        return self._resp(request, {**self._datos(sol), 'enviados': enviados, 'errores': errores}, 201)

    @staticmethod
    def _datos(sol):
        return {'ok': True, 'id': sol.pk, 'url': sol.url, 'estado': sol.estado_display.lower(),
                'formulario': sol.formulario.nombre, 'oportunidad_id': sol.oportunidad_id,
                'firmada_at': sol.firmada_at.isoformat() if sol.firmada_at else None}
