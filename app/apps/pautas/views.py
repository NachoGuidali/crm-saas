from datetime import timedelta
from decimal import Decimal

from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q
from django.db.models.functions import TruncDate
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views import View

from core.permisos import PermisoRequeridoMixin

from . import services
from .models import InversionPauta, Pauta, normalizar_mensaje


class PautaForm(forms.ModelForm):
    claves_texto = forms.CharField(
        required=False, label='También llega como', widget=forms.Textarea(attrs={'rows': 3}),
        help_text='Un texto por línea: otros nombres con los que llega el origen de esta pauta (utm_campaign, nombre '
                  'del anuncio…). No distingue mayúsculas, acentos, guiones ni guiones bajos.',
    )

    anuncios_texto = forms.CharField(
        required=False, label='IDs de anuncio de Meta', widget=forms.Textarea(attrs={'rows': 2}),
        help_text='Uno por línea. Es el ID del anuncio en el Administrador de anuncios. Opcional.',
    )
    palabras_texto = forms.CharField(
        required=False, label='Palabras clave en el primer mensaje', widget=forms.Textarea(attrs={'rows': 2}),
        help_text='Una por línea: un código (#IG-SEP) o una frase del mensaje precargado del link de WhatsApp. '
                  'Se usa si el chat no trae datos del anuncio. Opcional.',
    )

    class Meta:
        model = Pauta
        fields = ['nombre', 'plataforma', 'embudo', 'inicio', 'fin', 'activa', 'notas']
        widgets = {'inicio': forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'),
                   'fin': forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'),
                   'notas': forms.Textarea(attrs={'rows': 2})}
        help_texts = {'nombre': 'Conviene que sea igual al origen que manda el formulario (ej: "pauta instagram").'}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['claves_texto'].initial = '\n'.join(self.instance.claves or [])
        self.fields['anuncios_texto'].initial = '\n'.join(self.instance.anuncio_ids or [])
        self.fields['palabras_texto'].initial = '\n'.join(self.instance.palabras_clave or [])
        for f in self.fields.values():
            w = f.widget
            w.attrs['class'] = ('form-check-input' if isinstance(w, forms.CheckboxInput)
                                else 'form-select' if isinstance(w, forms.Select) else 'form-control')

    def clean(self):
        d = super().clean()
        claves = [c.strip() for c in (d.get('claves_texto') or '').splitlines() if c.strip()]
        self.instance.claves = claves
        # Un mismo texto de origen no puede apuntar a dos pautas
        propias = {services.normalizar_clave(c) for c in [d.get('nombre') or '', *claves]} - {''}
        lineas = lambda campo: [c.strip() for c in (d.get(campo) or '').splitlines() if c.strip()]
        self.instance.anuncio_ids = lineas('anuncios_texto')
        self.instance.palabras_clave = lineas('palabras_texto')
        cortas = [w for w in self.instance.palabras_clave if len(normalizar_mensaje(w)) < 4]
        if cortas:
            self.add_error('palabras_texto', f'"{cortas[0]}" es muy corta: coincidiría con mensajes cualquiera.')
        anuncios = set(self.instance.anuncio_ids)
        palabras = {normalizar_mensaje(w) for w in self.instance.palabras_clave} - {''}
        for otra in Pauta.objects.exclude(pk=self.instance.pk):
            choque = propias & otra.claves_normalizadas
            if choque:
                raise forms.ValidationError(f'"{sorted(choque)[0]}" ya corresponde a la pauta "{otra}".')
            if anuncios & set(otra.anuncio_ids or []):
                self.add_error('anuncios_texto', f'El anuncio {sorted(anuncios & set(otra.anuncio_ids))[0]} ya está en la pauta "{otra}".')
            if palabras & otra.palabras_normalizadas:
                self.add_error('palabras_texto', f'"{sorted(palabras & otra.palabras_normalizadas)[0]}" ya está en la pauta "{otra}".')
        return d


def _periodo(request):
    from apps.reportes.views import _periodo as periodo_reportes
    return periodo_reportes(request)


class AnalisisView(PermisoRequeridoMixin, View):
    permiso = ('pautas', 'reportes')

    def get(self, request):
        from apps.crm.models import Embudo
        p, desde, hasta, ini, fin = _periodo(request)
        embudo = Embudo.objects.filter(pk=request.GET.get('embudo') or 0).first()
        filas = services.metricas(ini, fin, embudo)
        filas.sort(key=lambda f: (f['pauta'] is None, -f['leads']))
        from apps.reportes.analisis import gestion_por
        gestion = gestion_por('pauta', ini, fin, embudo)
        for f in filas:
            g = gestion.get(f['pauta'].pk if f['pauta'] else None, {})
            f['gestion'] = g
            f['vueltas'] = round(g.get('llamadas', 0) / f['leads'], 1) if f['leads'] and g else None
        return render(request, 'pautas/analisis.html', {
            'filas': filas, 'totales': services.totales(filas), 'sin_pauta': services.origenes_sin_pauta(),
            'p': p, 'desde': desde, 'hasta': hasta, 'embudo': embudo, 'embudos': Embudo.objects.filter(activo=True),
            'puede_editar': request.user.tiene_permiso('pautas'),
            'max_cpa': max([f['cpa'] for f in filas if f['cpa']] or [0]),
            'max_leads': max([f['leads'] for f in filas] or [0]),
        })


class PautaEditarView(PermisoRequeridoMixin, View):
    permiso = 'pautas'

    def get(self, request, pk=None):
        pauta = get_object_or_404(Pauta, pk=pk) if pk else None
        inicial = {'nombre': request.GET.get('nombre', '')} if not pauta else None
        return render(request, 'pautas/form.html', {'pauta': pauta, 'form': PautaForm(instance=pauta, initial=inicial)})

    def post(self, request, pk=None):
        pauta = get_object_or_404(Pauta, pk=pk) if pk else None
        if pauta and request.POST.get('eliminar'):
            nombre = pauta.nombre
            pauta.delete()
            messages.success(request, f'Pauta "{nombre}" eliminada. Sus leads quedan sin pauta (conservan el origen).')
            return redirect('pautas:analisis')
        form = PautaForm(request.POST, instance=pauta)
        if not form.is_valid():
            return render(request, 'pautas/form.html', {'pauta': pauta, 'form': form})
        pauta = form.save(commit=False)
        if not pauta.pk:
            pauta.creada_por = request.user
        pauta.save()
        vinculadas = services.vincular_existentes(pauta)
        messages.success(request, f'Pauta "{pauta}" guardada.' + (
            f' Se vincularon {vinculadas} leads que ya habían entrado con ese origen.' if vinculadas else ''))
        return redirect(pauta.get_absolute_url())


class PautaDetalleView(PermisoRequeridoMixin, View):
    permiso = ('pautas', 'reportes')

    def get(self, request, pk):
        from apps.crm.models import Oportunidad
        pauta = get_object_or_404(Pauta, pk=pk)
        p, desde, hasta, ini, fin = _periodo(request)
        fila = services.metricas(ini, fin, pautas=[pauta])
        fila = fila[0] if fila else services._fila(pauta, {}, Decimal('0'), 0)
        ops = Oportunidad.objects.filter(pauta=pauta, created_at__range=(ini, fin))

        por_etapa = dict(ops.values_list('etapa').annotate(n=Count('pk')).values_list('etapa', 'n'))
        etapas = []
        from apps.crm.models import Embudo
        embudos = list(Embudo.objects.filter(pk__in=ops.values('embudo_id')).order_by('nombre')) or \
            ([pauta.embudo] if pauta.embudo else [])
        for emb in embudos:
            for e in emb.etapas.order_by('orden'):
                etapas.append({'etapa': e, 'n': por_etapa.get(e.pk, 0), 'embudo': emb})
        estados = [(Oportunidad(estado=k).get_estado_display(), n, k) for k, n in
                   ops.values_list('estado').annotate(n=Count('pk')).values_list('estado', 'n')]
        motivos = list(ops.filter(tipificacion__isnull=False).values('tipificacion__resultado', 'tipificacion__nombre',
                                                                      'tipificacion__categoria')
                       .annotate(n=Count('pk')).order_by('-n')[:12])
        agentes = list(ops.filter(agente__isnull=False).values('agente__first_name', 'agente__last_name', 'agente__username')
                       .annotate(leads=Count('pk'), ventas=Count('pk', filter=Q(estado=Oportunidad.ESTADO_GANADA)))
                       .order_by('-ventas', '-leads'))
        for a in agentes:
            a['nombre'] = f"{a['agente__first_name']} {a['agente__last_name']}".strip() or a['agente__username']
            a['conversion'] = round(a['ventas'] * 100 / a['leads'], 1) if a['leads'] else 0
        dias = [desde + timedelta(days=i) for i in range((hasta - desde).days + 1)]
        por_dia = dict(ops.annotate(d=TruncDate('created_at')).values_list('d').annotate(n=Count('pk')).values_list('d', 'n'))
        ventas_dia = dict(ops.filter(estado=Oportunidad.ESTADO_GANADA).annotate(d=TruncDate('created_at'))
                          .values_list('d').annotate(n=Count('pk')).values_list('d', 'n'))
        serie = {'labels': [d.strftime('%d/%m') for d in dias], 'leads': [por_dia.get(d, 0) for d in dias],
                 'ventas': [ventas_dia.get(d, 0) for d in dias]}
        return render(request, 'pautas/detalle.html', {
            'pauta': pauta, 'f': fila, 'p': p, 'desde': desde, 'hasta': hasta, 'etapas': etapas, 'estados': estados,
            'motivos': motivos, 'agentes': agentes, 'serie': serie,
            'inversiones': pauta.inversiones.select_related('cargada_por')[:60],
            'inversion_total': sum((i.monto for i in pauta.inversiones.all()), Decimal('0')),
            'puede_editar': request.user.tiene_permiso('pautas'),
            'filtro_url': f'?embudo=todos&pauta={pauta.pk}&fecha=ingreso&desde={desde:%Y-%m-%d}&hasta={hasta:%Y-%m-%d}',
        })

    def post(self, request, pk):
        if not request.user.tiene_permiso('pautas'):
            raise PermissionDenied
        pauta = get_object_or_404(Pauta, pk=pk)
        if request.POST.get('eliminar_inversion'):
            InversionPauta.objects.filter(pk=request.POST['eliminar_inversion'], pauta=pauta).delete()
            messages.success(request, 'Inversión eliminada.')
        else:
            from apps.crm.importacion import _decimal
            monto = _decimal(request.POST.get('monto', ''))
            fecha = request.POST.get('fecha') or timezone.localdate().isoformat()
            if monto is None or monto <= 0:
                messages.error(request, 'Monto inválido.')
                return redirect(request.get_full_path())
            InversionPauta.objects.create(pauta=pauta, fecha=fecha, monto=monto, nota=request.POST.get('nota', '')[:200],
                                          cargada_por=request.user)
            from django.contrib.humanize.templatetags.humanize import intcomma
            messages.success(request, f'Inversión de $ {intcomma(round(monto, 2))} cargada.')
        return redirect(request.get_full_path())
