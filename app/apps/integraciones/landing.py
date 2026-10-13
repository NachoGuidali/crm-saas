"""
Landing de prueba pública (/demo/formulario/): un formulario como el de una campaña, que entra al CRM por el mismo
camino que cualquier lead (deduplicación, asignación, automatizaciones, pauta) y avisa a la vendedora.

Se habilita con DEMO_LANDING=1 en el .env. El origen por defecto es "Landing Meta"; se puede cambiar con
?origen=…  o ?utm_campaign=…  (así se prueba la vinculación con Análisis de pautas).
"""
import logging

from django import forms
from django.conf import settings
from django.core.cache import cache
from django.http import Http404
from django.shortcuts import redirect, render
from django.views import View

from core.phone import normalizar_telefono

logger = logging.getLogger('apps.integraciones')

ORIGEN_POR_DEFECTO = 'Landing Meta'
LIMITE_POR_HORA = 20


class LandingForm(forms.Form):
    nombre = forms.CharField(max_length=120, label='Nombre y apellido')
    telefono = forms.CharField(max_length=40, label='Celular (WhatsApp)')
    email = forms.EmailField(required=False, label='Email (opcional)')
    consulta = forms.CharField(required=False, max_length=600, label='¿Qué te interesa? (opcional)',
                               widget=forms.Textarea(attrs={'rows': 3}))
    sitio = forms.CharField(required=False)  # trampa para bots: los humanos no la ven

    def clean_telefono(self):
        tel = normalizar_telefono(self.cleaned_data['telefono'])
        if not tel:
            raise forms.ValidationError('Revisá el número: ej. 11 2345 6789.')
        return tel


def _ip(request):
    fwd = request.META.get('HTTP_X_FORWARDED_FOR', '')
    return fwd.split(',')[0].strip() if fwd else request.META.get('REMOTE_ADDR', '')


class LandingView(View):
    def dispatch(self, request, *args, **kwargs):
        if not getattr(settings, 'DEMO_LANDING', False):
            raise Http404
        return super().dispatch(request, *args, **kwargs)

    def _origen(self, request):
        return (request.GET.get('origen') or request.GET.get('utm_campaign') or ORIGEN_POR_DEFECTO)[:200]

    def get(self, request):
        return render(request, 'integraciones/landing.html', {'form': LandingForm(), 'origen': self._origen(request)})

    def post(self, request):
        from apps.crm import services as crm
        from apps.crm.models import Actividad, Embudo, Oportunidad
        from apps.users.services import notificar_varios, supervisores_de
        origen = (request.POST.get('origen') or ORIGEN_POR_DEFECTO)[:200]
        form = LandingForm(request.POST)
        if not form.is_valid():
            return render(request, 'integraciones/landing.html', {'form': form, 'origen': origen})
        d = form.cleaned_data
        if d.get('sitio'):  # bot
            return redirect('demo_gracias')
        clave = f'landing_ip_{_ip(request)}'
        cache.add(clave, 0, 3600)
        if cache.incr(clave) > LIMITE_POR_HORA:
            form.add_error(None, 'Recibimos demasiados envíos desde esta conexión. Probá más tarde.')
            return render(request, 'integraciones/landing.html', {'form': form, 'origen': origen})
        embudo = (Embudo.objects.filter(activo=True, slug=request.GET.get('embudo') or '').first()
                  or Embudo.objects.filter(activo=True).order_by('orden', 'pk').first())
        if embudo is None:
            raise Http404
        res = crm.ingresar_prospecto({'nombre': d['nombre'], 'telefono': d['telefono'], 'email': d.get('email', '')},
                                     embudo, Oportunidad.ORIGEN_WEB, fuente='Landing de prueba', origen_pauta=origen)
        op = res.oportunidad
        if op is not None and d.get('consulta'):
            Actividad.objects.create(contacto=res.contacto, oportunidad=op, tipo=Actividad.TIPO_NOTA,
                                     texto=f'Consulta desde la landing: {d["consulta"]}')
        if op is not None and res.oportunidad_nueva:
            op.refresh_from_db()
            if op.agente_id is None:  # quedó en espera (fuera de horario / sin conectadas / reparto manual)
                notificar_varios(supervisores_de(embudo), 'asignacion', f'Nuevo lead sin asignar: {res.contacto.nombre}',
                                 f'{origen} · {res.contacto.telefono}', op.get_absolute_url())
        logger.info('Landing de prueba: %s (%s) → %s', res.contacto.telefono, origen, res.motivo or 'nuevo')
        return redirect('demo_gracias')


def gracias(request):
    if not getattr(settings, 'DEMO_LANDING', False):
        raise Http404
    return render(request, 'integraciones/landing_gracias.html')
