"""Pantallas de email: plantillas, configuración del servidor de correo, difusiones y baja de suscripción."""
from datetime import datetime

from django import forms
from django.contrib import messages
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views import View

from core.permisos import PermisoRequeridoMixin
from core.utils import paginar

from . import difusiones
from .models import ConfigEmail, Difusion, DifusionDestinatario, EmailEnviado, PlantillaEmail


def _estilizar(form):
    for f in form.fields.values():
        w = f.widget
        w.attrs['class'] = ('form-check-input' if isinstance(w, forms.CheckboxInput)
                            else 'form-select' if isinstance(w, forms.Select) else 'form-control')
    return form


class PlantillaEmailForm(forms.ModelForm):
    class Meta:
        model = PlantillaEmail
        fields = ['nombre', 'asunto', 'cuerpo', 'activa']
        widgets = {'cuerpo': forms.Textarea(attrs={'rows': 12})}

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        _estilizar(self)


class PlantillasEmailView(PermisoRequeridoMixin, View):
    permiso = ('automatizaciones', 'difusiones', 'plantillas')

    def get(self, request, pk=None):
        p = get_object_or_404(PlantillaEmail, pk=pk) if pk else None
        return self._render(request, p, PlantillaEmailForm(instance=p))

    def post(self, request, pk=None):
        p = get_object_or_404(PlantillaEmail, pk=pk) if pk else None
        if p and request.POST.get('eliminar'):
            p.delete()
            messages.success(request, 'Plantilla eliminada.')
            return redirect('automatizaciones:plantillas_email')
        form = PlantillaEmailForm(request.POST, instance=p)
        if not form.is_valid():
            return self._render(request, p, form)
        p = form.save()
        messages.success(request, f'Plantilla "{p}" guardada.')
        return redirect('automatizaciones:plantilla_email', pk=p.pk)

    def _render(self, request, p, form):
        return render(request, 'automatizaciones/plantillas_email.html',
                      {'plantilla': p, 'form': form, 'plantillas': PlantillaEmail.objects.all()})


class ConfigEmailForm(forms.ModelForm):
    password = forms.CharField(required=False, widget=forms.PasswordInput(render_value=False), label='Contraseña',
                               help_text='Vacío = deja la que estaba.')

    class Meta:
        model = ConfigEmail
        fields = ['activo', 'host', 'puerto', 'seguridad', 'usuario', 'password', 'remitente', 'por_minuto']

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        _estilizar(self)

    def save(self, commit=True):
        nueva = self.cleaned_data.get('password')
        if not nueva:
            self.instance.password = ConfigEmail.get().password
        return super().save(commit)


class ConfigEmailView(PermisoRequeridoMixin, View):
    permiso = 'integraciones'

    def get(self, request):
        return render(request, 'automatizaciones/config_email.html', {'form': ConfigEmailForm(instance=ConfigEmail.get())})

    def post(self, request):
        if request.POST.get('accion') == 'probar':
            from django.core.mail import EmailMessage
            from .services import conexion_email
            destino = request.POST.get('destino') or request.user.email
            if not destino:
                messages.error(request, 'Indicá a qué email mandar la prueba.')
                return redirect('automatizaciones:config_email')
            try:
                con, remitente = conexion_email()
                EmailMessage('Prueba del CRM', 'Si te llegó este email, la configuración funciona.', remitente,
                             [destino], connection=con).send(fail_silently=False)
                messages.success(request, f'Email de prueba enviado a {destino}. Revisá también la carpeta de spam.')
            except Exception as e:
                messages.error(request, f'No se pudo enviar: {e}')
            return redirect('automatizaciones:config_email')
        form = ConfigEmailForm(request.POST, instance=ConfigEmail.get())
        if not form.is_valid():
            return render(request, 'automatizaciones/config_email.html', {'form': form})
        form.save()
        messages.success(request, 'Configuración de email guardada. Probala con "Enviar email de prueba".')
        return redirect('automatizaciones:config_email')


class DifusionForm(forms.ModelForm):
    class Meta:
        model = Difusion
        fields = ['nombre', 'canal', 'plantilla_email', 'plantilla_wa', 'texto_wa', 'linea']
        widgets = {'texto_wa': forms.Textarea(attrs={'rows': 4})}

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        from apps.whatsapp.models import LineaWhatsApp, Plantilla
        self.fields['plantilla_email'].queryset = PlantillaEmail.objects.filter(activa=True)
        self.fields['plantilla_wa'].queryset = Plantilla.objects.filter(activa=True)
        self.fields['linea'].queryset = LineaWhatsApp.objects.filter(activa=True)
        _estilizar(self)


class DifusionListView(PermisoRequeridoMixin, View):
    permiso = 'difusiones'

    def get(self, request):
        lista = list(Difusion.objects.select_related('creada_por', 'plantilla_email', 'plantilla_wa')[:100])
        for d in lista:
            d.r = difusiones.resultados(d)
        return render(request, 'automatizaciones/difusiones.html', {'difusiones': lista})


class DifusionDetalleView(PermisoRequeridoMixin, View):
    permiso = 'difusiones'

    def get(self, request, pk):
        dif = get_object_or_404(Difusion, pk=pk)
        return self._render(request, dif, DifusionForm(instance=dif))

    def post(self, request, pk):
        dif = get_object_or_404(Difusion, pk=pk)
        accion = request.POST.get('accion', 'guardar')
        if accion == 'cancelar' and dif.estado in (Difusion.BORRADOR, Difusion.PROGRAMADA, Difusion.ENVIANDO):
            dif.estado = Difusion.CANCELADA
            dif.save(update_fields=['estado'])
            dif.destinatarios.filter(estado=DifusionDestinatario.PENDIENTE).update(
                estado=DifusionDestinatario.OMITIDO, detalle='Difusión cancelada')
            messages.success(request, 'Difusión cancelada: no se envía nada más.')
            return redirect('automatizaciones:difusion', pk=dif.pk)
        if dif.estado != Difusion.BORRADOR:
            messages.error(request, 'La difusión ya se programó o envió: no se puede editar.')
            return redirect('automatizaciones:difusion', pk=dif.pk)
        form = DifusionForm(request.POST, instance=dif)
        if not form.is_valid():
            return self._render(request, dif, form)
        dif = form.save()
        if accion in ('enviar', 'programar'):
            cuando = None
            if accion == 'programar':
                try:
                    cuando = timezone.make_aware(datetime.strptime(request.POST.get('cuando', ''), '%Y-%m-%dT%H:%M'))
                except ValueError:
                    messages.error(request, 'Indicá fecha y hora para programar.')
                    return self._render(request, dif, form)
            try:
                difusiones.iniciar(dif, cuando)
            except ValueError as e:
                messages.error(request, str(e))
                return self._render(request, dif, form)
            messages.success(request, 'Difusión en marcha: se envía de a tandas, podés seguir el avance acá.'
                             if accion == 'enviar' else f'Difusión programada para el {timezone.localtime(cuando):%d/%m %H:%M}.')
        else:
            messages.success(request, 'Borrador guardado.')
        return redirect('automatizaciones:difusion', pk=dif.pk)

    def _render(self, request, dif, form):
        dest = dif.destinatarios.select_related('contacto', 'oportunidad__etapa', 'email', 'mensaje').order_by('pk')
        if request.GET.get('estado'):
            dest = dest.filter(estado=request.GET['estado'])
        return render(request, 'automatizaciones/difusion.html', {
            'dif': dif, 'form': form, 'r': difusiones.resultados(dif), 'page': paginar(request, dest, 50),
            'omitidos_previstos': difusiones.omitidos_previstos(dif) if dif.estado == Difusion.BORRADOR else 0,
            'estados': DifusionDestinatario.ESTADOS,
        })


def baja_email(request, token):
    """Página pública "No quiero recibir más emails" (confirmar con un botón: los antivirus abren los links solos)."""
    envio = EmailEnviado.objects.filter(token=token).select_related('contacto').first()
    if envio is None or envio.contacto is None:
        raise Http404
    hecho = False
    if request.method == 'POST':
        c = envio.contacto
        if not c.no_email:
            c.no_email, c.no_email_at = True, timezone.now()
            c.save(update_fields=['no_email', 'no_email_at'])
            from apps.crm.models import Actividad
            Actividad.objects.create(contacto=c, oportunidad=envio.oportunidad, tipo=Actividad.TIPO_CAMBIO,
                                     texto='Se dio de baja de los emails (link del email).',
                                     datos={'cambios': [{'campo': 'no_email', 'label': 'Recibir emails',
                                                         'antes': 'Sí', 'despues': 'No'}]})
        EmailEnviado.objects.filter(pk=envio.pk, baja_at__isnull=True).update(baja_at=timezone.now())
        hecho = True
    return render(request, 'automatizaciones/baja.html', {'hecho': hecho, 'email': envio.para})

