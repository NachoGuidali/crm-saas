import json

from django import forms
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View

from core.permisos import PermisoRequeridoMixin
from core.utils import paginar, query_sin_page

from .models import AccionEtapa, EjecucionAccion


class AccionForm(forms.ModelForm):
    demora_valor = forms.IntegerField(min_value=0, initial=0, label='Demora')
    demora_unidad = forms.ChoiceField(choices=[('min', 'minutos'), ('h', 'horas'), ('d', 'días')], initial='min')

    class Meta:
        model = AccionEtapa
        fields = ['nombre', 'etapa', 'otras_etapas', 'todas_las_etapas', 'disparador', 'accion_previa', 'tipo', 'mover_a',
                  'tipificacion', 'plantilla_email', 'activa', 'solo_si_sigue_en_etapa', 'solo_en_horario', 'plantilla',
                  'texto', 'linea', 'email_asunto', 'tarea_titulo', 'tarea_vence_horas', 'modo_embudo', 'embudo_destino',
                  'etapa_destino', 'volver_a', 'asignar_destino', 'usuario_destino', 'reparto', 'volver_a_etapa',
                  'etiqueta', 'solo_pautas', 'solo_etiquetas', 'excluir_etiquetas']
        widgets = {'texto': forms.Textarea(attrs={'rows': 4}), 'otras_etapas': forms.CheckboxSelectMultiple,
                   'solo_pautas': forms.CheckboxSelectMultiple, 'solo_etiquetas': forms.CheckboxSelectMultiple,
                   'excluir_etiquetas': forms.CheckboxSelectMultiple}

    def __init__(self, *args, embudo=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.embudo = embudo
        self.fields['etapa'].required = False
        self.fields['reparto'].required = False
        from apps.pautas.models import Pauta
        self.fields['solo_pautas'].queryset = Pauta.objects.filter(activa=True).order_by('nombre')
        from .models import PlantillaEmail
        self.fields['plantilla_email'].queryset = PlantillaEmail.objects.filter(activa=True)
        self.fields['accion_previa'].queryset = AccionEtapa.objects.none()
        if embudo is not None:
            self.fields['etapa'].queryset = embudo.etapas.order_by('orden')
            self.fields['mover_a'].queryset = embudo.etapas.order_by('orden')
            from apps.crm.models import Etapa as _Etapa
            abiertas = embudo.etapas.filter(tipo=_Etapa.TIPO_NORMAL).order_by('orden')
            self.fields['otras_etapas'].queryset = embudo.etapas.order_by('orden')
            self.fields['volver_a_etapa'].queryset = abiertas
            self.fields['accion_previa'].queryset = (AccionEtapa.objects.filter(embudo=embudo)
                                                     .exclude(pk=self.instance.pk).select_related('etapa'))
            self.fields['accion_previa'].label_from_instance = lambda a: f'{a.etapa} · {a.nombre}'
            from django.db.models import Q as _Q
            from apps.crm.models import Tipificacion
            self.fields['tipificacion'].queryset = Tipificacion.objects.filter(
                _Q(embudo=embudo) | _Q(embudo__isnull=True), activa=True).order_by('resultado', 'nombre')
        from apps.crm.models import Embudo, Etapa
        from apps.users.models import User
        otros = Embudo.objects.filter(activo=True).exclude(pk=getattr(embudo, 'pk', None))
        self.fields['embudo_destino'].queryset = otros
        self.fields['etapa_destino'].queryset = (Etapa.objects.filter(embudo__in=otros, tipo=Etapa.TIPO_NORMAL)
                                                 .select_related('embudo').order_by('embudo__nombre', 'orden'))
        self.fields['etapa_destino'].label_from_instance = lambda e: f'{e.embudo} · {e.nombre}'
        self.fields['usuario_destino'].queryset = User.objects.filter(is_active=True).order_by('first_name', 'username')
        m = self.instance.demora_minutos or 0
        if m and m % 1440 == 0:
            self.fields['demora_valor'].initial, self.fields['demora_unidad'].initial = m // 1440, 'd'
        elif m and m % 60 == 0:
            self.fields['demora_valor'].initial, self.fields['demora_unidad'].initial = m // 60, 'h'
        else:
            self.fields['demora_valor'].initial = m
        for f in self.fields.values():
            w = f.widget
            if isinstance(w, forms.CheckboxSelectMultiple):
                w.attrs['class'] = 'form-check-input'
                continue
            w.attrs['class'] = ('form-check-input' if isinstance(w, forms.CheckboxInput)
                                else 'form-select' if isinstance(w, forms.Select) else 'form-control')

    def clean(self):
        d = super().clean()
        tipo = d.get('tipo')
        d['reparto'] = d.get('reparto') or AccionEtapa.REPARTO_MENOR_CARGA
        if tipo == AccionEtapa.TIPO_WHATSAPP and not (d.get('plantilla') or d.get('texto')):
            self.add_error('plantilla', 'Elegí una plantilla o escribí un texto.')
        if tipo == AccionEtapa.TIPO_SMS and not d.get('texto'):
            self.add_error('texto', 'Escribí el texto del SMS.')
        if tipo == AccionEtapa.TIPO_EMAIL and not d.get('texto') and not d.get('plantilla_email'):
            self.add_error('plantilla_email', 'Elegí una plantilla de email o escribí asunto y texto.')
        disp = d.get('disparador')
        if disp == AccionEtapa.DISP_DESPUES_DE:
            if not d.get('accion_previa'):
                self.add_error('accion_previa', 'Elegí después de qué automatización.')
            else:
                d['etapa'] = d['accion_previa'].etapa  # la secuencia vive en la misma etapa
        elif d.get('todas_las_etapas') and self.embudo is not None:
            d['etapa'] = d.get('etapa') or self.embudo.etapa_inicial
        elif not d.get('etapa'):
            self.add_error('etapa', 'Elegí la etapa.')
        if disp == AccionEtapa.DISP_DESPUES_DE:
            d['todas_las_etapas'], d['otras_etapas'] = False, []
        if tipo == AccionEtapa.TIPO_ETIQUETA and not d.get('etiqueta'):
            self.add_error('etiqueta', 'Elegí la etiqueta.')
        if tipo == AccionEtapa.TIPO_EMAIL and d.get('plantilla_email'):
            self._errors.pop('texto', None)
        if disp in (AccionEtapa.DISP_SIN_RESPUESTA, AccionEtapa.DISP_SIN_ACTIVIDAD) and not d.get('demora_valor'):
            self.add_error('demora_valor', 'Indicá cuánto tiempo esperar.')
        if tipo == AccionEtapa.TIPO_ETAPA:
            destino, tip = d.get('mover_a'), d.get('tipificacion')
            if destino is None:
                self.add_error('mover_a', 'Elegí la etapa.')
            elif destino.es_cierre:
                esperado = 'venta' if destino.es_ganado else 'no_venta'
                if tip is None or tip.resultado != esperado or tip.es_postergacion:
                    self.add_error('tipificacion', f'Para cerrar como {destino} elegí una tipificación de ese tipo (no de postergación).')
        if tipo == AccionEtapa.TIPO_EMBUDO:
            modo, etapa = d.get('modo_embudo'), d.get('etapa')
            if modo != AccionEtapa.MODO_VOLVER and not d.get('embudo_destino'):
                self.add_error('embudo_destino', 'Elegí el embudo de destino.')
            if d.get('etapa_destino') and d.get('embudo_destino') and d['etapa_destino'].embudo_id != d['embudo_destino'].pk:
                self.add_error('etapa_destino', 'La etapa no es de ese embudo.')
            if etapa is not None and etapa.es_cierre and modo != AccionEtapa.MODO_CREAR:
                self.add_error('modo_embudo', 'En Venta / No venta la tarjeta ya está cerrada: usá "Crear una oportunidad nueva".')
            if d.get('asignar_destino') == 'usuario' and not d.get('usuario_destino'):
                self.add_error('usuario_destino', 'Elegí el usuario.')
        return d

    def save(self, commit=True):
        if self.cleaned_data.get('etapa') is not None:
            self.instance.etapa = self.cleaned_data['etapa']
        factor = {'min': 1, 'h': 60, 'd': 1440}[self.cleaned_data.get('demora_unidad') or 'min']
        self.instance.demora_minutos = (self.cleaned_data.get('demora_valor') or 0) * factor
        if self.embudo is not None:
            self.instance.embudo = self.embudo
        return super().save(commit)


class AccionListView(PermisoRequeridoMixin, View):
    permiso = 'automatizaciones'

    def get(self, request):
        from apps.crm.views import embudo_actual
        embudo, embudos = embudo_actual(request)
        etapas = []
        if embudo:
            acciones = list(AccionEtapa.objects.filter(embudo=embudo).select_related('plantilla', 'linea', 'etapa', 'etiqueta',
                                                                                   'volver_a_etapa')
                            .prefetch_related('otras_etapas', 'solo_pautas', 'solo_etiquetas', 'excluir_etiquetas'))
            for etapa in embudo.etapas.order_by('orden'):
                etapas.append({'etapa': etapa, 'acciones': [a for a in acciones if a.etapa_id == etapa.pk]})
        return render(request, 'automatizaciones/lista.html', {'embudo': embudo, 'embudos': embudos, 'etapas': etapas})


class AccionEditarView(PermisoRequeridoMixin, View):
    permiso = 'automatizaciones'

    def get(self, request, pk=None):
        from apps.crm.views import embudo_actual
        accion = get_object_or_404(AccionEtapa, pk=pk) if pk else None
        embudo = accion.embudo if accion else embudo_actual(request)[0]
        inicial = {'etapa': request.GET.get('etapa')} if not accion else None
        return self._render(request, accion, embudo, AccionForm(instance=accion, embudo=embudo, initial=inicial))

    def post(self, request, pk=None):
        from apps.crm.views import embudo_actual
        accion = get_object_or_404(AccionEtapa, pk=pk) if pk else None
        embudo = accion.embudo if accion else embudo_actual(request)[0]
        if request.POST.get('eliminar') and accion:
            accion.delete()
            messages.success(request, 'Automatización eliminada.')
            return redirect('automatizaciones:lista')
        if request.POST.get('toggle') and accion:
            accion.activa = not accion.activa
            accion.save(update_fields=['activa'])
            messages.success(request, f'"{accion}" {"activada" if accion.activa else "desactivada"}.')
            return redirect('automatizaciones:lista')
        form = AccionForm(request.POST, instance=accion, embudo=embudo)
        if not form.is_valid():
            return self._render(request, accion, embudo, form)
        form.save()
        messages.success(request, 'Automatización guardada.')
        return redirect('automatizaciones:lista')

    def _render(self, request, accion, embudo, form):
        from apps.whatsapp.models import Plantilla
        from .models import PlantillaEmail
        return render(request, 'automatizaciones/form.html', {
            'accion': accion, 'embudo': embudo, 'form': form,
            'plantillas_texto': json.dumps({str(p.pk): p.cuerpo for p in Plantilla.objects.filter(activa=True)}).replace('</', '<\\/'),
            'plantillas_email_texto': json.dumps({str(p.pk): f'{p.asunto}\n\n{p.cuerpo}' for p in PlantillaEmail.objects.filter(activa=True)}).replace('</', '<\\/'),
        })


class EjecucionesView(PermisoRequeridoMixin, View):
    permiso = ('automatizaciones', 'supervision')

    def get(self, request):
        qs = EjecucionAccion.objects.select_related('accion', 'oportunidad__contacto')
        if request.GET.get('estado'):
            qs = qs.filter(estado=request.GET['estado'])
        return render(request, 'automatizaciones/ejecuciones.html', {
            'page': paginar(request, qs, 50), 'estados': EjecucionAccion.ESTADO_CHOICES, 'filtros': request.GET,
            'query': query_sin_page(request),
        })


_GIF = (b'GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,'
        b'\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;')


def email_abierto(request, token):
    """Píxel de apertura (público). Los clientes de correo que bloquean imágenes no cuentan."""
    from django.db.models import F
    from django.http import HttpResponse
    from django.utils import timezone
    from .models import EmailEnviado
    EmailEnviado.objects.filter(token=token).update(aperturas=F('aperturas') + 1)
    EmailEnviado.objects.filter(token=token, abierto_at__isnull=True).update(abierto_at=timezone.now())
    resp = HttpResponse(_GIF, content_type='image/gif')
    resp['Cache-Control'] = 'no-store'
    return resp


def email_clic(request, token):
    """Link redirigido (público): registra el clic y lleva a la URL original (firmada, no es un redirect abierto)."""
    from django.core import signing
    from django.db.models import F
    from django.http import Http404, HttpResponseRedirect
    from django.utils import timezone
    from .models import EmailEnviado
    try:
        url = signing.loads(request.GET.get('u', ''), salt='email-clic')
    except signing.BadSignature:
        raise Http404
    if not str(url).startswith(('http://', 'https://')):
        raise Http404
    ahora = timezone.now()
    EmailEnviado.objects.filter(token=token).update(clics=F('clics') + 1)
    EmailEnviado.objects.filter(token=token, clic_at__isnull=True).update(clic_at=ahora)
    EmailEnviado.objects.filter(token=token, abierto_at__isnull=True).update(abierto_at=ahora)  # si hizo clic, lo abrió
    return HttpResponseRedirect(url)
