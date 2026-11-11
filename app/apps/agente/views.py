import json
from datetime import timedelta
from decimal import Decimal

from django import forms
from django.contrib import messages
from django.db.models import Count, Q, Sum
from django.http import Http404, HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views import View

from core.permisos import PermisoRequeridoMixin
from core.utils import error, json_body, ok

from . import conocimiento, lineas, motor, services
from .models import CAMPOS_BASE_AGENTE, ConfigAgente, DocumentoAgente, EnlaceRotativo, TurnoAgente


class ModuloMixin(PermisoRequeridoMixin):
    permiso = 'agente_ia'

    def dispatch(self, request, *args, **kwargs):
        if not services.habilitado():
            raise Http404('El módulo Agente IA no está habilitado.')
        return super().dispatch(request, *args, **kwargs)


def _bootstrap(form):
    for f in form.fields.values():
        w = f.widget
        if isinstance(w, forms.CheckboxInput):
            w.attrs['class'] = 'form-check-input'
        elif isinstance(w, forms.CheckboxSelectMultiple):
            continue
        elif isinstance(w, forms.Select):
            w.attrs['class'] = 'form-select'
        else:
            w.attrs['class'] = 'form-control'
    return form


# ── Configuración general ──────────────────────────────────────────────────

class ConfigForm(forms.ModelForm):
    # Las keys no se muestran nunca: vacío = dejar la que está; "borrar" = quitarla.
    nueva_anthropic = forms.CharField(required=False, label='API key de Anthropic', widget=forms.PasswordInput,
                                      help_text='Dejala vacía para mantener la actual.')
    nueva_openai = forms.CharField(required=False, label='API key de OpenAI', widget=forms.PasswordInput,
                                   help_text='Dejala vacía para mantener la actual.')
    borrar_keys = forms.MultipleChoiceField(required=False, choices=[('anthropic', 'Anthropic'), ('openai', 'OpenAI')],
                                            widget=forms.CheckboxSelectMultiple, label='Borrar key propia')
    campos_base = forms.MultipleChoiceField(choices=[c for c in CAMPOS_BASE_AGENTE if c[0] != 'nombre'], required=False,
                                            widget=forms.CheckboxSelectMultiple, label='Datos del contacto a completar')

    class Meta:
        model = ConfigAgente
        fields = ['activo', 'nombre', 'empresa', 'modelo', 'esfuerzo', 'instrucciones', 'criterios', 'campos_base',
                  'campos', 'embudo', 'etapa_calificando', 'etapa_derivado', 'etapa_descartado',
                  'tipificacion_descartado', 'derivar_url', 'derivar_token', 'mensaje_derivacion', 'demora_segundos',
                  'max_turnos', 'presupuesto_diario']
        widgets = {'instrucciones': forms.Textarea(attrs={'rows': 9}), 'criterios': forms.Textarea(attrs={'rows': 5}),
                   'mensaje_derivacion': forms.Textarea(attrs={'rows': 2}), 'campos': forms.CheckboxSelectMultiple,
                   'derivar_token': forms.PasswordInput(render_value=True)}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.crm.models import CampoPersonalizado, Etapa, Tipificacion
        self.fields['campos'].queryset = CampoPersonalizado.objects.filter(activo=True).exclude(
            tipo=CampoPersonalizado.TIPO_ARCHIVO).order_by('nombre')
        etapas = Etapa.objects.select_related('embudo').order_by('embudo__nombre', 'orden')
        for k in ('etapa_calificando', 'etapa_derivado', 'etapa_descartado'):
            self.fields[k].queryset = etapas
            self.fields[k].label_from_instance = lambda e: f'{e.embudo} · {e}'
        self.fields['tipificacion_descartado'].queryset = Tipificacion.objects.all()
        _bootstrap(self)

    def clean(self):
        d = super().clean()
        emb = d.get('embudo')
        for k in ('etapa_calificando', 'etapa_derivado', 'etapa_descartado'):
            if d.get(k) and emb and d[k].embudo_id != emb.pk:
                self.add_error(k, 'Tiene que ser una etapa del embudo elegido.')
        return d


class ConfigView(ModuloMixin, View):
    def get(self, request):
        cfg = ConfigAgente.get()
        return self._render(request, ConfigForm(instance=cfg), cfg)

    def post(self, request):
        cfg = ConfigAgente.get()
        form = ConfigForm(request.POST, instance=cfg)
        if request.POST.get('probar') in ('anthropic', 'openai'):
            from .claves import SinClave, clave, probar
            prov = request.POST['probar']
            nueva = request.POST.get(f'nueva_{prov}', '').strip()
            try:
                key, origen = (nueva, 'nueva') if nueva else clave(prov, cfg)
            except SinClave as e:
                messages.error(request, str(e))
                return redirect('agente:config')
            ok_, msg = probar(prov, key)
            (messages.success if ok_ else messages.error)(request, f'{msg} (key {"de la plataforma" if origen == "plataforma" else "de la empresa" if origen == "cliente" else "nueva, sin guardar"})')
            return redirect('agente:config')
        if not form.is_valid():
            return self._render(request, form, cfg)
        cfg = form.save(commit=False)
        d = form.cleaned_data
        for prov in ('anthropic', 'openai'):
            if prov in d.get('borrar_keys', []):
                setattr(cfg, f'{prov}_api_key', '')
            elif d.get(f'nueva_{prov}', '').strip():
                setattr(cfg, f'{prov}_api_key', d[f'nueva_{prov}'].strip())
        cfg.save()
        form.save_m2m()
        messages.success(request, 'Agente guardado.' + ('' if cfg.activo else ' Está APAGADO: prendelo para que responda.'))
        return redirect('agente:config')

    def _render(self, request, form, cfg):
        from django.conf import settings
        from . import claves
        pol = claves.politica()
        keys = []
        for prov in ('anthropic', 'openai'):
            propia = getattr(cfg, f'{prov}_api_key')
            keys.append({'prov': prov, 'nombre': claves.NOMBRES[prov], 'campo': form[f'nueva_{prov}'],
                         'propia': f'…{propia[-4:]}' if propia else '', 'plataforma': bool(claves._de_plataforma(prov)),
                         'disponible': claves.disponible(prov, cfg)})
        return render(request, 'agente/config.html', {
            'form': form, 'cfg': cfg, 'seccion': 'agente', 'tab': 'config', 'keys': keys, 'politica': pol,
            'puede_propia': pol in ('propia', 'ambas'),
            'falta_key': not claves.disponible(cfg.proveedor, cfg), 'falta_openai': not claves.disponible('openai', cfg),
            'proveedor_nombre': claves.NOMBRES[cfg.proveedor],
        })


# ── Conocimiento y precios ─────────────────────────────────────────────────

class DocumentoForm(forms.ModelForm):
    class Meta:
        model = DocumentoAgente
        fields = ['titulo', 'texto', 'archivo', 'enviable', 'activo', 'orden']
        widgets = {'texto': forms.Textarea(attrs={'rows': 10})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _bootstrap(self)

    def clean(self):
        d = super().clean()
        archivo = d.get('archivo')
        if archivo and hasattr(archivo, 'read') and not (d.get('texto') or '').strip():
            if archivo.size > 15 * 1024 * 1024:
                self.add_error('archivo', 'Máximo 15 MB.')
                return d
            try:
                d['texto'] = conocimiento.extraer_texto(archivo.name, archivo.read())
            except conocimiento.ErrorConocimiento as e:
                if not d.get('enviable'):
                    self.add_error('archivo', str(e))
            archivo.seek(0)
        if not (d.get('texto') or '').strip() and not d.get('enviable'):
            self.add_error('texto', 'Escribí el texto o subí un archivo (PDF, Word o TXT).')
        return d


class ConocimientoView(ModuloMixin, View):
    def get(self, request):
        return self._render(request)

    def post(self, request):
        cfg = ConfigAgente.get()
        accion = request.POST.get('accion')
        try:
            if accion == 'precios_archivo' and request.FILES.get('planilla'):
                f = request.FILES['planilla']
                conocimiento.guardar_precios(cfg, *conocimiento.leer_planilla(f.name, f.read()))
                cfg.precios_url = ''
                cfg.save(update_fields=['precios_url'])
                messages.success(request, f'Precios cargados: {len(cfg.precios_filas)} filas.')
            elif accion == 'precios_google':
                cfg.precios_url = request.POST.get('precios_url', '').strip()
                conocimiento.url_csv_google(cfg.precios_url)
                cfg.save(update_fields=['precios_url'])
                conocimiento.sincronizar_google(cfg)
                messages.success(request, f'Planilla de Google enlazada: {len(cfg.precios_filas)} filas. Se actualiza cada 30 min.')
            elif accion == 'precios_borrar':
                cfg.precios_url, cfg.precios_columnas, cfg.precios_filas = '', [], []
                cfg.save(update_fields=['precios_url', 'precios_columnas', 'precios_filas'])
                messages.success(request, 'Tabla de precios borrada.')
            elif accion == 'doc_borrar':
                DocumentoAgente.objects.filter(pk=request.POST.get('doc')).delete()
                messages.success(request, 'Documento borrado.')
        except conocimiento.ErrorConocimiento as e:
            messages.error(request, str(e))
        return redirect('agente:conocimiento')

    def _render(self, request):
        cfg = ConfigAgente.get()
        docs = DocumentoAgente.objects.all()
        total = sum(len(d.texto) for d in docs if d.activo)
        return render(request, 'agente/conocimiento.html', {
            'cfg': cfg, 'docs': docs, 'seccion': 'agente', 'tab': 'conocimiento', 'total_chars': total,
            'tokens_aprox': total // 4, 'muestra': cfg.precios_filas[:15],
        })


class DocumentoView(ModuloMixin, View):
    def get(self, request, pk=None):
        doc = get_object_or_404(DocumentoAgente, pk=pk) if pk else None
        return render(request, 'agente/documento.html', {'form': DocumentoForm(instance=doc), 'doc': doc,
                                                         'seccion': 'agente', 'tab': 'conocimiento'})

    def post(self, request, pk=None):
        doc = get_object_or_404(DocumentoAgente, pk=pk) if pk else None
        form = DocumentoForm(request.POST, request.FILES, instance=doc)
        if not form.is_valid():
            return render(request, 'agente/documento.html', {'form': form, 'doc': doc, 'seccion': 'agente',
                                                             'tab': 'conocimiento'})
        doc = form.save(commit=False)
        doc.texto = form.cleaned_data.get('texto', doc.texto)
        doc.save()
        messages.success(request, f'"{doc.titulo}" guardado.')
        return redirect('agente:conocimiento')


# ── Líneas y links rotativos ───────────────────────────────────────────────

class LineasView(ModuloMixin, View):
    def get(self, request):
        from apps.whatsapp.models import LineaWhatsApp
        lista = list(LineaWhatsApp.objects.filter(activa=True).order_by('orden', 'nombre'))
        for l in lista:
            l.cupo_hoy = lineas.cupo_dia(l)
            l.iniciados = lineas.iniciados_hoy(l)
        from core import tenancy
        return render(request, 'agente/lineas.html', {
            'lineas': lista, 'enlaces': EnlaceRotativo.objects.prefetch_related('lineas'), 'seccion': 'agente',
            'tab': 'lineas', 'base': tenancy.url_sitio(),
        })

    def post(self, request):
        from apps.whatsapp.models import LineaWhatsApp
        accion = request.POST.get('accion')
        if accion == 'linea':
            l = get_object_or_404(LineaWhatsApp, pk=request.POST.get('linea'))
            l.agente_ia_activo = bool(request.POST.get('agente_ia_activo'))
            l.reserva = bool(request.POST.get('reserva'))
            l.max_salientes_dia = max(int(request.POST.get('max_salientes_dia') or 0), 0)
            cal = request.POST.get('calentamiento')
            if cal and not l.calentamiento_inicio:
                l.calentamiento_inicio = timezone.localdate()
            elif not cal:
                l.calentamiento_inicio = None
            l.save(update_fields=['agente_ia_activo', 'reserva', 'max_salientes_dia', 'calentamiento_inicio'])
            messages.success(request, f'Línea "{l}" actualizada.')
        elif accion == 'enlace':
            e = get_object_or_404(EnlaceRotativo, pk=request.POST['enlace']) if request.POST.get('enlace') else EnlaceRotativo()
            e.nombre = request.POST.get('nombre', '').strip()[:100] or 'Link'
            e.mensaje = request.POST.get('mensaje', '').strip()[:300]
            e.activo = bool(request.POST.get('activo', '1'))
            e.save()
            e.lineas.set(LineaWhatsApp.objects.filter(pk__in=request.POST.getlist('lineas')))
            messages.success(request, f'Link "{e.nombre}" guardado.')
        elif accion == 'enlace_borrar':
            EnlaceRotativo.objects.filter(pk=request.POST.get('enlace')).delete()
            messages.success(request, 'Link borrado.')
        return redirect('agente:lineas')


class EnlacePublicoView(View):
    """/w/<slug>/ → wa.me de la línea que toca (sin login: va en los anuncios)."""

    def get(self, request, slug):
        from urllib.parse import quote
        if not services.habilitado():
            raise Http404
        enlace = get_object_or_404(EnlaceRotativo, slug=slug, activo=True)
        linea = lineas.elegir_para_enlace(enlace)
        if linea is None:
            return HttpResponse('<p style="font-family:sans-serif;text-align:center;margin-top:20vh">En este momento no '
                                'hay asesores disponibles. Probá de nuevo en unos minutos 🙏</p>', status=503)
        EnlaceRotativo.objects.filter(pk=enlace.pk).update(clics=enlace.clics + 1)
        url = f'https://wa.me/{lineas.numero_wa(linea)}' + (f'?text={quote(enlace.mensaje)}' if enlace.mensaje else '')
        return HttpResponseRedirect(url)


# ── Simulador ──────────────────────────────────────────────────────────────

class SimuladorView(ModuloMixin, View):
    """Chatear con el agente con la configuración actual. No manda WhatsApp ni toca leads."""

    def get(self, request):
        from . import claves
        cfg = ConfigAgente.get()
        return render(request, 'agente/simulador.html', {'seccion': 'agente', 'tab': 'simulador', 'cfg': cfg,
                                                         'falta_key': not claves.disponible(cfg.proveedor, cfg)})

    def post(self, request):
        data = json_body(request)
        historial = data.get('historial') or []
        datos = data.get('datos') or {}
        modo = data.get('modo') or TurnoAgente.MODO_RESPUESTA
        cfg = ConfigAgente.get()
        ctx = motor.Contexto(config=cfg, simulado=True, datos=dict(datos))
        msgs = [{'role': 'user' if h.get('de') == 'lead' else 'assistant',
                 'content': [{'type': 'text', 'text': str(h.get('texto', ''))[:4000] or '(vacío)'}]}
                for h in historial[-40:]]
        mensajes = motor.armar_mensajes(msgs, motor.contexto_lead(ctx, modo, data.get('motivo', '')))
        try:
            res = motor.pensar(ctx, mensajes)
        except motor.ErrorAgente as e:
            return error(str(e))
        except Exception as e:
            return error(f'Error del modelo: {e}')
        partes = motor.partir(res.textos)
        TurnoAgente.objects.create(modo=modo, modelo=cfg.modelo, respuesta='\n---\n'.join(partes),
                                   herramientas=res.herramientas, tokens_entrada=res.usage['entrada'],
                                   tokens_salida=res.usage['salida'], tokens_cache_lectura=res.usage['cache_lectura'],
                                   tokens_cache_escritura=res.usage['cache_escritura'], costo=res.costo,
                                   request_id=res.request_id, simulado=True)
        return ok(mensajes=partes, datos=ctx.datos, acciones=ctx.acciones, resultado=ctx.resultado,
                  herramientas=res.herramientas, costo=str(res.costo), rechazo=res.rechazo)


# ── Registro y métricas ────────────────────────────────────────────────────

class RegistroView(ModuloMixin, View):
    def get(self, request):
        dias = int(request.GET.get('dias') or 7)
        desde = timezone.now() - timedelta(days=dias)
        reales = TurnoAgente.objects.filter(simulado=False, created_at__gte=desde)
        resumen = reales.aggregate(costo=Sum('costo'), turnos=Count('id'), chats=Count('conversacion', distinct=True),
                                   errores=Count('id', filter=~Q(error='')))
        from apps.crm.models import Actividad
        acts = Actividad.objects.filter(created_at__gte=desde)
        derivados = acts.filter(texto__startswith='Agente IA derivó').count()
        descartados = acts.filter(texto__startswith='Agente IA descartó').count()
        humano = acts.filter(texto__startswith='Agente IA pidió').count()
        costo = resumen['costo'] or Decimal(0)
        turnos = (TurnoAgente.objects.filter(created_at__gte=desde, simulado=request.GET.get('sim') == '1')
                  .select_related('conversacion__contacto')[:200])
        return render(request, 'agente/registro.html', {
            'seccion': 'agente', 'tab': 'registro', 'dias': dias, 'resumen': resumen, 'costo': costo,
            'derivados': derivados, 'descartados': descartados, 'humano': humano, 'turnos': turnos,
            'costo_derivado': (costo / derivados) if derivados else None, 'gasto_hoy': motor.gasto_hoy(),
            'cfg': ConfigAgente.get(), 'sim': request.GET.get('sim') == '1',
        })
