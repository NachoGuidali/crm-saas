import csv
from datetime import datetime, timedelta

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Exists, F, OuterRef, Prefetch, Q
from django.http import Http404, HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views import View

from apps.pautas.models import Pauta
from core import presencia
from core.permisos import PermisoRequeridoMixin
from core.utils import error, json_body, ok, paginar, query_sin_page

from . import auditoria, services as crm
from .forms import (CampoPersonalizadoForm, ReglaAsignacionForm, ContactoForm, EmbudoForm, EtapaForm, EtiquetaForm, ImportacionForm,
                    NuevaOportunidadForm, TareaForm, TipificacionForm, etapas_de, tipificaciones_de)
from .models import (CAMPOS_FIJOS_REQUERIBLES, Actividad, CampoPersonalizado, Contacto, Embudo, Etapa, Etiqueta, ImportacionLote, Oportunidad,
                     Tarea, Tipificacion)

POR_COLUMNA = 25


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def embudos_visibles(user):
    qs = Embudo.objects.filter(activo=True)
    if not user.ve_todo:
        qs = qs.filter(agentes=user)
    return qs.distinct()


def embudo_actual(request):
    embudos = list(embudos_visibles(request.user))
    pk = request.GET.get('embudo') or request.session.get('embudo_actual')
    elegido = next((e for e in embudos if str(e.pk) == str(pk)), None) or (embudos[0] if embudos else None)
    if elegido:
        request.session['embudo_actual'] = elegido.pk
    return elegido, embudos


def oportunidad_visible(request, pk, lock=False):
    qs = Oportunidad.objects.visibles_para(request.user).select_related('contacto', 'embudo', 'etapa', 'agente',
                                                                         'tipificacion')
    op = qs.filter(pk=pk).first()
    if op is None:
        raise Http404('Oportunidad no encontrada')
    return op


def _fecha_local(valor):
    if not valor:
        return None
    dt = parse_datetime(valor) or (datetime.fromisoformat(valor) if 'T' in valor else None)
    if dt is None:
        try:
            dt = datetime.strptime(valor, '%Y-%m-%d').replace(hour=9)
        except ValueError:
            return None
    return timezone.make_aware(dt) if timezone.is_naive(dt) else dt


def _con_marcas(qs):
    ahora = timezone.now()
    vencidas = Tarea.objects.filter(oportunidad=OuterRef('pk'), estado=Tarea.ESTADO_PENDIENTE, vence_at__lt=ahora)
    return qs.annotate(tiene_vencidas=Exists(vencidas))


def datos_extra_display(contacto, embudo=None):
    """[(etiqueta, valor)] para la ficha: primero los campos configurados, después datos sueltos de importaciones.
    Los de tipo archivo se muestran aparte (sección Documentos)."""
    extra = contacto.datos_extra or {}
    campos = [c for c in CampoPersonalizado.activos(embudo) if not c.es_archivo]
    filas = [(c.nombre, c.valor_display(extra.get(c.slug)), c) for c in campos]
    conocidos = {c.slug for c in CampoPersonalizado.objects.all()} | {'demo', 'masivo'}
    filas += [(k, v, None) for k, v in extra.items() if k not in conocidos and v not in (None, '')]
    return filas


def anotar_campos(objetos, campos, contacto_attr='contacto'):
    """Agrega a cada objeto `.campos_vista` = [(campo, valor_display)] para tarjetas y listas."""
    for o in objetos:
        c = getattr(o, contacto_attr) if contacto_attr else o
        extra = c.datos_extra or {}
        o.campos_vista = [(cp, cp.valor_display(extra.get(cp.slug))) for cp in campos]
    return objetos


def agentes_activos():
    from apps.users.models import User
    return User.objects.filter(is_active=True).order_by('first_name', 'username')


# ═══════════════════════════════════════════════════════════════════════════
# Tablero (kanban)
# ═══════════════════════════════════════════════════════════════════════════

class TableroView(LoginRequiredMixin, View):
    def get(self, request):
        embudo, embudos = embudo_actual(request)
        if embudo is None:
            return render(request, 'crm/sin_embudo.html')
        etapas = etapas_de(embudo)
        base = self._base(request, embudo)
        campos_tarjeta = [c for c in CampoPersonalizado.activos(embudo) if c.mostrar_en_tarjeta]
        columnas = []
        hace_30 = timezone.now() - timedelta(days=30)
        conteos = dict(base.filter(estado__in=Oportunidad.ESTADOS_ACTIVOS).values_list('etapa')
                       .annotate(n=Count('pk')).values_list('etapa', 'n'))
        for etapa in etapas:
            # Venta / No venta: las cerradas en los últimos 30 días, la más reciente arriba
            total = base.filter(etapa=etapa, cerrada_at__gte=hace_30).count() if etapa.es_cierre \
                else conteos.get(etapa.pk, 0)
            cards = anotar_campos(list(self._cards(base, etapa)[:POR_COLUMNA]), campos_tarjeta)
            columnas.append({'etapa': etapa, 'total': total, 'cards': cards, 'cierre': etapa.es_cierre,
                             'hay_mas': total > POR_COLUMNA})
        return render(request, 'crm/tablero.html', {
            'embudo': embudo, 'embudos': embudos, 'columnas': columnas, 'agentes': agentes_activos(),
            'tipificaciones': tipificaciones_de(embudo), 'filtros': request.GET,
            'origenes': Oportunidad.ORIGEN_CHOICES, 'pautas': Pauta.objects.filter(activa=True).order_by('nombre'),
            'lotes': ImportacionLote.objects.filter(embudo=embudo).order_by('-created_at')[:50],
        })

    @staticmethod
    def _base(request, embudo):
        qs = Oportunidad.objects.visibles_para(request.user).filter(embudo=embudo)
        qs = crm.filtrar_oportunidades(qs, request.GET, request.user)
        if request.GET.get('pausadas') == '0':
            qs = qs.exclude(estado=Oportunidad.ESTADO_PAUSADA)
        return qs

    @staticmethod
    def _cards(base, etapa):
        if etapa.es_cierre:
            qs = (base.filter(etapa=etapa, cerrada_at__gte=timezone.now() - timedelta(days=30))
                  .select_related('contacto', 'agente', 'tipificacion').order_by('-cerrada_at'))
        else:
            qs = (base.filter(etapa=etapa, estado__in=Oportunidad.ESTADOS_ACTIVOS)
                  .select_related('contacto', 'agente').order_by('-prioritaria', 'estado', '-ultima_actividad_at'))
        return _con_marcas(qs)


class TableroColumnaView(LoginRequiredMixin, View):
    """'Ver más' de una columna del tablero (paginado de a 25)."""

    def get(self, request):
        embudo, _ = embudo_actual(request)
        etapa = get_object_or_404(Etapa, pk=request.GET.get('etapa'), embudo=embudo)
        offset = int(request.GET.get('offset', 0))
        base = TableroView._base(request, embudo)
        cards = list(TableroView._cards(base, etapa)[offset:offset + POR_COLUMNA + 1])
        anotar_campos(cards, [c for c in CampoPersonalizado.activos(embudo) if c.mostrar_en_tarjeta])
        html = render_to_string('crm/_tarjetas.html', {'cards': cards[:POR_COLUMNA]}, request=request)
        return JsonResponse({'html': html, 'hay_mas': len(cards) > POR_COLUMNA})


# ═══════════════════════════════════════════════════════════════════════════
# Lista, alta, exportación y acciones masivas
# ═══════════════════════════════════════════════════════════════════════════

class OportunidadListView(LoginRequiredMixin, View):
    def get(self, request):
        embudo, embudos = embudo_actual(request)
        qs = Oportunidad.objects.visibles_para(request.user)
        if embudo and request.GET.get('embudo') != 'todos':
            qs = qs.filter(embudo=embudo)
        qs = crm.filtrar_oportunidades(qs, request.GET, request.user)
        if request.GET.get('estancados') and embudo:
            qs = qs.filter(estado=Oportunidad.ESTADO_ABIERTA, etapa__tipo=Etapa.TIPO_NORMAL,
                           etapa_desde__lt=timezone.now() - timedelta(days=embudo.dias_estancado_alerta or 7))
        orden = request.GET.get('orden', '-created_at')
        if orden.lstrip('-') not in ('created_at', 'ultima_actividad_at', 'etapa_desde', 'intentos_contacto',
                                     'asignada_at', 'cerrada_at', 'proximo_contacto_at', 'puntaje'):
            orden = '-created_at'
        qs = _con_marcas(qs.select_related('contacto', 'agente', 'etapa', 'embudo', 'tipificacion', 'pauta').order_by(orden))
        page = paginar(request, qs, 50)
        campos = CampoPersonalizado.activos(embudo)
        anotar_campos(page.object_list, [c for c in campos if c.mostrar_en_lista])
        from apps.telefonia.models import CampaniaDiscado
        etapas = etapas_de(embudo) if embudo else []
        tipificaciones = tipificaciones_de(embudo) if embudo else []
        sel = {k: set(request.GET.getlist(k)) for k in ('etapa', 'agente', 'estado', 'origen', 'tipificacion', 'lote',
                                                         'etiqueta', 'pauta')}
        return render(request, 'crm/oportunidades.html', {
            'sel': sel, 'chips': _chips_filtros(request, etapas, tipificaciones),
            'opciones': {
                'etapa': [(str(e.pk), e.nombre) for e in etapas],
                'agente': [('ninguno', 'Sin asignar')] + [(str(a.pk), a.display_name) for a in agentes_activos()],
                'estado': list(Oportunidad.ESTADO_CHOICES), 'origen': list(Oportunidad.ORIGEN_CHOICES),
                'tipificacion': [(str(t.pk), f'{t.get_resultado_display()} · {t.nombre}') for t in tipificaciones],
                'etiqueta': [(str(e.pk), e.nombre) for e in Etiqueta.objects.all()],
                'pauta': [('ninguna', 'Sin pauta')] + [(str(p.pk), p.nombre) for p in
                                                        Pauta.objects.all()],
                'lote': [(str(l.pk), (l.fuente or l.nombre_archivo)[:40])
                         for l in (ImportacionLote.objects.filter(embudo=embudo)[:30] if embudo else [])],
            },
            'campos_fecha': [(k, label) for k, label, _ in crm.CAMPOS_FECHA_FILTRO],
            'total_filtrado': page.paginator.count,
            'campos_columna': [c for c in campos if c.mostrar_en_lista],
            'campos_filtro': [c for c in campos if c.filtrable],
            'page': page, 'embudo': embudo, 'embudos': embudos, 'filtros': request.GET,
            'query': query_sin_page(request), 'agentes': agentes_activos(),
            'etapas': etapas, 'estados': Oportunidad.ESTADO_CHOICES,
            'origenes': Oportunidad.ORIGEN_CHOICES, 'etiquetas': Etiqueta.objects.all(),
            'tipificaciones': tipificaciones,
            'lotes': ImportacionLote.objects.filter(embudo=embudo)[:30] if embudo else [],
            'campanias': CampaniaDiscado.objects.exclude(estado=CampaniaDiscado.ESTADO_FINALIZADA),
        })


def _chips_filtros(request, etapas, tipificaciones):
    """Filtros activos como chips [(texto, url sin ese valor)] para quitarlos de a uno."""
    from apps.users.models import User
    nombres = {
        'etapa': {str(e.pk): e.nombre for e in etapas},
        'estado': dict(Oportunidad.ESTADO_CHOICES), 'origen': dict(Oportunidad.ORIGEN_CHOICES),
        'tipificacion': {str(t.pk): t.nombre for t in tipificaciones},
        'etiqueta': {str(e.pk): e.nombre for e in Etiqueta.objects.all()},
        'lote': {str(l.pk): (l.fuente or l.nombre_archivo) for l in ImportacionLote.objects.all()[:200]},
        'pauta': {'ninguna': 'Sin pauta', **{str(p.pk): p.nombre for p in
                                              Pauta.objects.all()}},
    }
    agentes = request.GET.getlist('agente')
    if agentes:
        nombres['agente'] = {str(u.pk): u.display_name for u in User.objects.filter(pk__in=[a for a in agentes if a.isdigit()])}
        nombres['agente']['ninguno'] = 'Sin asignar'
    titulos = {'etapa': 'Etapa', 'agente': 'Agente', 'estado': 'Estado', 'origen': 'Canal', 'tipificacion': 'Tipificación',
               'etiqueta': 'Etiqueta', 'lote': 'Base', 'pauta': 'Pauta'}
    chips = []
    for clave, titulo in titulos.items():
        for valor in request.GET.getlist(clave):
            if not valor:
                continue
            params = request.GET.copy()
            params.setlist(clave, [v for v in params.getlist(clave) if v != valor])
            params.pop('page', None)
            chips.append((f'{titulo}: {nombres.get(clave, {}).get(valor, valor)}', params.urlencode()))
    campo_fecha = dict((k, label) for k, label, _ in crm.CAMPOS_FECHA_FILTRO).get(request.GET.get('fecha') or 'ingreso')
    if request.GET.get('desde') or request.GET.get('hasta'):
        params = request.GET.copy()
        for k in ('desde', 'hasta', 'fecha', 'page'):
            params.pop(k, None)
        rango = f'{request.GET.get("desde") or "…"} → {request.GET.get("hasta") or "…"}'
        chips.append((f'{campo_fecha}: {rango}', params.urlencode()))
    for clave, texto in (('q', 'Búsqueda'), ('origen_pauta', 'Origen de pauta:'), ('intentos_min', 'Intentos ≥'), ('intentos_max', 'Intentos ≤'), ('ingresos_min', 'Ingresó ≥ veces:'), ('puntaje_min', 'Puntaje ≥'),
                         ('sin_actividad', 'Sin actividad (días) ≥'), ('estancados', 'Estancados'), ('sla', 'SLA:')):
        if request.GET.get(clave):
            params = request.GET.copy()
            params.pop(clave, None)
            params.pop('page', None)
            chips.append((f'{texto} {request.GET[clave] if clave != "estancados" else ""}'.strip(), params.urlencode()))
    return chips


class OportunidadCrearView(LoginRequiredMixin, View):
    def get(self, request):
        embudo, _ = embudo_actual(request)
        form = NuevaOportunidadForm(user=request.user, initial={'embudo': embudo, 'telefono': request.GET.get('tel', '')})
        return render(request, 'crm/oportunidad_form.html', {'form': form})

    def post(self, request):
        form = NuevaOportunidadForm(request.POST, user=request.user)
        if not form.is_valid():
            return render(request, 'crm/oportunidad_form.html', {'form': form})
        d = form.cleaned_data
        datos = {k: d.get(k) for k in ('nombre', 'telefono', 'email', 'dni', 'fecha_atencion', 'especialidad_atencion')}
        datos['datos_extra'] = {k: v for k, v in form.valores_personalizados().items() if v is not None}
        try:
            res = crm.ingresar_prospecto(datos, d['embudo'], Oportunidad.ORIGEN_MANUAL, usuario=request.user,
                                         agente=d.get('agente'), valor=d.get('valor'), pauta=d.get('pauta'))
        except crm.ErrorNegocio as e:
            form.add_error(None, str(e))
            return render(request, 'crm/oportunidad_form.html', {'form': form})
        if d.get('nota') and res.oportunidad:
            crm.agregar_nota(res.oportunidad, request.user, d['nota'])
        if res.oportunidad_nueva:
            messages.success(request, 'Prospecto creado.')
            return redirect(res.oportunidad.get_absolute_url())
        motivos = {'ya_activa': 'ya tenía una oportunidad en curso en este embudo',
                   'ya_cliente': 'ya es cliente (venta cerrada)', 'perdida_previa': 'ya se había cerrado como No venta',
                   'no_contactar': 'está marcado como "no contactar"'}
        messages.warning(request, f'No se duplicó: {res.contacto.nombre} {motivos.get(res.motivo, "ya existía")}. '
                                  'Te mostramos su ficha.')
        destino = res.oportunidad or res.contacto
        visible = Oportunidad.objects.visibles_para(request.user).filter(pk=getattr(res.oportunidad, 'pk', None)).exists()
        return redirect(destino.get_absolute_url() if visible or not res.oportunidad else reverse('crm:oportunidades'))


class ExportarView(PermisoRequeridoMixin, View):
    permiso = 'exportar'

    def get(self, request):
        embudo, _ = embudo_actual(request)
        qs = Oportunidad.objects.visibles_para(request.user)
        if embudo and request.GET.get('embudo') != 'todos':
            qs = qs.filter(embudo=embudo)
        qs = (crm.filtrar_oportunidades(qs, request.GET, request.user)
              .select_related('contacto', 'agente', 'etapa', 'embudo', 'tipificacion', 'pauta').order_by('-created_at'))

        class Eco:
            def write(self, v):
                return v

        writer = csv.writer(Eco(), delimiter=';')
        campos = CampoPersonalizado.activos(embudo)
        encabezado = ['ID', 'Nombre', 'Teléfono', 'Email', 'DNI', 'Fecha atención', 'Embudo', 'Etapa', 'Estado',
                      'Agente', 'Canal', 'Pauta', 'Origen de pauta', 'Fuente', 'Tipificación', 'Categoría', 'Intentos', 'Valor', 'Creada',
                      'Última actividad', 'Cerrada'] + [c.nombre for c in campos]

        def filas():
            yield '﻿' + writer.writerow(encabezado)
            for op in qs.iterator(chunk_size=1000):
                c = op.contacto
                yield writer.writerow([
                    op.pk, c.nombre, c.telefono, c.email, c.dni, c.fecha_atencion or '', op.embudo, op.etapa,
                    op.get_estado_display(), op.agente.display_name if op.agente else '', op.get_origen_display(),
                    op.pauta.nombre if op.pauta_id else '', op.origen_pauta, op.fuente, op.tipificacion or '', op.tipificacion.categoria if op.tipificacion else '',
                    op.intentos_contacto, op.valor or '', timezone.localtime(op.created_at).strftime('%d/%m/%Y %H:%M'),
                    timezone.localtime(op.ultima_actividad_at).strftime('%d/%m/%Y %H:%M'),
                    timezone.localtime(op.cerrada_at).strftime('%d/%m/%Y') if op.cerrada_at else '',
                ] + [c.valor_display((op.contacto.datos_extra or {}).get(c.slug)) for c in campos])

        from core.auditoria import auditar
        from apps.users.models import RegistroAuditoria
        auditar(RegistroAuditoria.EXPORT, request=request, objeto='oportunidades (CSV)',
                detalle=f'{qs.count()} filas; filtros={dict(request.GET)}')
        resp = StreamingHttpResponse(filas(), content_type='text/csv; charset=utf-8')
        resp['Content-Disposition'] = f'attachment; filename="oportunidades_{timezone.localdate():%Y%m%d}.csv"'
        return resp


class AccionesMasivasView(LoginRequiredMixin, View):
    """Aplica una acción a las oportunidades marcadas o a TODAS las que cumplen los filtros de la pantalla."""

    def post(self, request):
        from django.http import QueryDict
        from . import masivas
        from .tasks import accion_masiva
        user, accion = request.user, request.POST.get('accion', '')
        volver = request.POST.get('volver') or request.META.get('HTTP_REFERER') or reverse('crm:oportunidades')
        datos = {k: request.POST.get(k, '') for k in ('destino_agente', 'destino_etapa', 'tipificacion', 'nota',
                                                      'fecha', 'motivo', 'hasta', 'destino_etiqueta', 'campania',
                                                      'titulo', 'vence', 'tipo_tarea', 'para')}
        datos['destino_agentes'] = [a for a in request.POST.getlist('destino_agentes') if a.isdigit()]

        qs = Oportunidad.objects.visibles_para(user)
        if request.POST.get('todos_filtrados') == '1':
            filtros = QueryDict(request.POST.get('filtros_qs', ''))
            if filtros.get('embudo') != 'todos':
                embudo = embudos_visibles(user).filter(pk=filtros.get('embudo') or request.session.get('embudo_actual')).first()
                if embudo:
                    qs = qs.filter(embudo=embudo)
            else:
                embudo = None
            qs = crm.filtrar_oportunidades(qs, filtros, user)
            if filtros.get('estancados'):
                dias = (embudo.dias_estancado_alerta if embudo else 0) or 7
                qs = qs.filter(estado=Oportunidad.ESTADO_ABIERTA, etapa__tipo=Etapa.TIPO_NORMAL,
                               etapa_desde__lt=timezone.now() - timedelta(days=dias))
        else:
            qs = qs.filter(pk__in=[int(i) for i in request.POST.getlist('ids') if i.isdigit()])
        ids = list(qs.order_by('pk').values_list('pk', flat=True).distinct()[:masivas.LIMITE_TOTAL + 1])
        if not ids:
            messages.warning(request, 'No hay oportunidades seleccionadas.')
            return redirect(volver)
        if len(ids) > masivas.LIMITE_TOTAL:
            messages.error(request, f'Son más de {masivas.LIMITE_TOTAL:,} oportunidades: acotá los filtros.'.replace(',', '.'))
            return redirect(volver)
        if accion == 'difusion':
            if not user.tiene_permiso('difusiones'):
                raise PermissionDenied
            from apps.automatizaciones import difusiones
            desc = request.POST.get('filtros_qs', '') if request.POST.get('todos_filtrados') == '1' else f'{len(ids)} seleccionadas'
            dif = difusiones.crear(user, Oportunidad.objects.filter(pk__in=ids), descripcion=desc)
            messages.success(request, f'Difusión creada con {dif.destinatarios.count()} personas. Elegí el mensaje y enviala.')
            return redirect('automatizaciones:difusion', pk=dif.pk)
        try:
            masivas.validar(user, accion, datos)
        except crm.ErrorNegocio as e:
            messages.error(request, str(e))
            return redirect(volver)

        if accion in ('eliminar', 'reasignar', 'repartir'):
            from core.auditoria import auditar
            from apps.users.models import RegistroAuditoria
            auditar(RegistroAuditoria.BORRADO_MASIVO, request=request,
                    objeto=f'{accion} · {len(ids)} oportunidades', detalle=str(datos)[:500])

        if len(ids) > masivas.LIMITE_SINCRONICO:
            accion_masiva.delay(user.pk, accion, ids, datos)
            messages.info(request, f'Procesando {len(ids)} oportunidades en segundo plano. Te avisamos con una '
                                   'notificación cuando termine.')
            return redirect(volver)
        resultado = masivas.ejecutar(user, accion, ids, datos)
        messages.success(request, masivas.resumen(accion, resultado) + '.')
        for e in resultado['errores'][:8]:
            messages.warning(request, e)
        if len(resultado['errores']) > 8:
            messages.warning(request, f'… y {len(resultado["errores"]) - 8} más.')
        return redirect(volver)


# ═══════════════════════════════════════════════════════════════════════════
# Ficha de la oportunidad
# ═══════════════════════════════════════════════════════════════════════════

def contexto_chat(request, contacto, oportunidad=None):
    """
    Panel de WhatsApp de las fichas. El agente elige desde qué línea escribir: las que tiene habilitadas
    (con o sin chat previo con esa persona) + las que ya tienen chat (si no la tiene habilitada, solo lectura).
    """
    from apps.whatsapp.models import Conversacion, LineaWhatsApp, Plantilla, RespuestaRapida
    from apps.whatsapp.services import linea_para
    convs = {c.linea_id: c for c in Conversacion.objects.filter(contacto=contacto).select_related('linea')
             .order_by('ultimo_mensaje_at')}  # si hubiera dos en la misma línea, queda la más reciente
    usables = list(LineaWhatsApp.para_usuario(request.user))
    ids_usables = {l.pk for l in usables}
    opciones = [{'linea': c.linea, 'conv': c, 'usable': c.linea_id in ids_usables and c.linea.activa}
                for c in sorted(convs.values(), key=lambda c: c.ultimo_mensaje_at or c.created_at, reverse=True)]
    opciones += [{'linea': l, 'conv': None, 'usable': True} for l in usables if l.pk not in convs]

    pedida = request.GET.get('linea')
    elegida = next((o for o in opciones if str(o['linea'].pk) == pedida), None)
    if elegida is None:
        sugerida = linea_para(contacto, oportunidad, request.user) if usables else None
        elegida = (next((o for o in opciones if o['conv'] and o['usable']), None)
                   or next((o for o in opciones if sugerida and o['linea'].pk == sugerida.pk), None)
                   or (opciones[0] if opciones else None))
    linea = elegida['linea'] if elegida else None
    conv = elegida['conv'] if elegida else None
    plantillas = [p for p in Plantilla.objects.filter(activa=True) if linea is None or p.disponible_en(linea)]
    return {
        'opciones_linea': opciones, 'linea_actual': linea, 'conv': conv,
        'puede_escribir': bool(elegida and elegida['usable']),
        'ventana_cerrada': bool(linea and linea.es_oficial and (conv is None or not conv.ventana_abierta)),
        'mensajes': list(conv.mensajes.select_related('enviado_por').order_by('-timestamp')[:60])[::-1] if conv else [],
        'lineas': usables, 'plantillas': plantillas, 'respuestas': RespuestaRapida.objects.filter(activa=True),
    }


def _plantillas_email():
    from apps.automatizaciones.models import PlantillaEmail
    return list(PlantillaEmail.objects.filter(activa=True).values('pk', 'nombre', 'asunto', 'cuerpo'))


def _sms_operativo():
    from apps.integraciones.models import ConfigSMS
    return bool(ConfigSMS.get().operativo)


FILTROS_ACTIVIDAD = [
    ('', 'Todo'), ('nota', 'Notas'), ('llamada,intento', 'Llamadas'), ('whatsapp,email,sms', 'Mensajes'),
    ('etapa,cierre,pausa', 'Etapas'), ('cambio', 'Cambios de datos'), ('asignacion', 'Asignaciones'), ('tarea', 'Tareas'),
    ('sistema,reingreso', 'Sistema'),
]


class OportunidadDetalleView(LoginRequiredMixin, View):
    def get(self, request, pk):
        op = oportunidad_visible(request, pk)
        contacto = op.contacto
        actividades = (Actividad.objects.filter(contacto=contacto).select_related('usuario', 'llamada', 'oportunidad__embudo')
                       .order_by('-created_at')[:150])
        ctx = {
            'op': op, 'contacto': contacto, 'etapas': etapas_de(op.embudo),
            'tipificaciones': tipificaciones_de(op.embudo), 'actividades': actividades,
            'filtros_actividad': FILTROS_ACTIVIDAD, 'sms_activo': _sms_operativo(),
            'plantillas_email': _plantillas_email(),
            'tareas': op.tareas.filter(estado=Tarea.ESTADO_PENDIENTE).select_related('asignado_a').order_by('vence_at'),
            'otras': contacto.oportunidades.exclude(pk=op.pk).select_related('embudo', 'etapa', 'agente'),
            'llamadas': contacto.llamadas.select_related('agente').order_by('-inicio_at')[:30],
            'agentes': agentes_activos(), 'tarea_form': TareaForm(user=request.user),
            'contacto_form': ContactoForm(instance=contacto),
            'datos_extra': datos_extra_display(contacto, op.embudo),
            'documentos': [(c, (contacto.datos_extra or {}).get(c.slug) or [])
                           for c in CampoPersonalizado.activos(op.embudo) if c.es_archivo],
            'pautas': Pauta.objects.all(),
            'puede_reabrir': request.user.tiene_permiso('reabrir'),
            'historial': op.historial.select_related('etapa_anterior', 'etapa_nueva', 'usuario')[:30],
            'ejecuciones': op.ejecuciones.select_related('accion').order_by('-created_at')[:15],
        }
        ctx.update(contexto_chat(request, contacto, op))
        return render(request, 'crm/oportunidad_detalle.html', ctx)


class AccionOportunidadView(LoginRequiredMixin, View):
    """Endpoints AJAX de la ficha y del tablero: nunca recargan la página."""

    def post(self, request, pk, accion):
        op = oportunidad_visible(request, pk)
        data = json_body(request)
        user = request.user
        try:
            handler = getattr(self, f'_{accion}', None)
            if handler is None:
                return error('Acción desconocida', 404)
            return handler(request, op, data, user)
        except crm.FaltanCampos as e:
            return error(str(e), faltan=e.faltan, etapa_nombre=str(e.etapa))
        except crm.ErrorNegocio as e:
            return error(str(e))

    def _mover(self, request, op, data, user):
        etapa = get_object_or_404(Etapa, pk=data.get('etapa'), embudo=op.embudo)
        tip_id = data.get('tipificacion')
        tipificacion = get_object_or_404(Tipificacion, pk=tip_id) if tip_id else None
        if tipificacion and tipificacion.es_postergacion:
            fecha = _fecha_local(data.get('fecha'))
            crm.postergar(op, user, fecha, tipificacion, data.get('nota', ''))
            return ok(mensaje=f'Postergado hasta el {timezone.localtime(fecha):%d/%m/%Y}', recargar=True)
        valor = data.get('valor') or None
        crm.mover_etapa(op, etapa, user, tipificacion=tipificacion, nota=data.get('nota', ''), valor=valor,
                        puede_reabrir=user.tiene_permiso('reabrir'))
        op.refresh_from_db()
        return ok(mensaje=f'Movido a {etapa}', estado=op.estado, etapa=etapa.pk, recargar=etapa.es_cierre)

    def _completar(self, request, op, data, user):
        """Guarda los datos obligatorios que faltaban para pasar de etapa (ventanita del tablero / ficha)."""
        from django import forms as djforms
        from .forms import campo_formulario, valor_para_guardar
        from .models import CampoPersonalizado
        claves = [c for c in data if c != 'csrfmiddlewaretoken']
        # Archivos obligatorios (campos de tipo archivo): se guardan primero, directo en el contacto
        for clave, f in request.FILES.items():
            cp = CampoPersonalizado.objects.filter(slug=clave[3:], tipo=CampoPersonalizado.TIPO_ARCHIVO).first() \
                if clave.startswith('cp:') else None
            if cp is not None:
                from . import archivos
                try:
                    archivos.adjuntar(op.contacto, cp, f.read(), f.name, user, op)
                except archivos.ErrorArchivo as e:
                    return error(f'{cp.nombre}: {e}')
        campos, tipos = {}, {}
        for clave in claves:
            desc = crm.descripcion_campo(clave)
            if desc is None:
                continue
            if clave.startswith('cp:'):
                cp = CampoPersonalizado.objects.get(slug=clave[3:])
                if cp.es_archivo:
                    continue  # ya se guardó arriba (si vino)
                campos[clave], tipos[clave] = campo_formulario(cp), cp
            else:
                campos[clave] = {'email': djforms.EmailField, 'fecha': djforms.DateField,
                                 'numero': lambda **kw: djforms.DecimalField(max_digits=12, decimal_places=2, **kw)
                                 }.get(desc['tipo'], lambda **kw: djforms.CharField(max_length=200, **kw))(label=desc['nombre'])
            campos[clave].required = True
        form = type('CompletarForm', (djforms.Form,), dict(campos))(data)
        if not form.is_valid():
            return error(' · '.join(f'{campos[k].label}: {v[0]}' for k, v in form.errors.items()))
        contacto, cambios_c, extra = op.contacto, [], dict(op.contacto.datos_extra or {})
        antes = {**auditoria.foto_contacto(contacto), **auditoria.foto_oportunidad(op)}
        for clave, valor in form.cleaned_data.items():
            if clave.startswith('cp:'):
                extra[clave[3:]] = valor_para_guardar(tipos[clave], valor)
            elif clave == 'valor':
                op.valor = valor
                op.save(update_fields=['valor', 'updated_at'])
            else:
                if clave == 'telefono_alternativo':
                    from core.phone import normalizar_telefono
                    valor = normalizar_telefono(valor)
                setattr(contacto, clave, valor)
                cambios_c.append(clave)
        if extra != (contacto.datos_extra or {}):
            contacto.datos_extra = extra
            cambios_c.append('datos_extra')
        if cambios_c:
            contacto.save(update_fields=cambios_c + ['updated_at'])
        auditoria.registrar_cambios(antes, {**auditoria.foto_contacto(contacto), **auditoria.foto_oportunidad(op)},
                                    user, contacto, op, contexto='Datos completados para cambiar de etapa')
        return ok(mensaje='Datos guardados')

    def _pausar(self, request, op, data, user):
        crm.pausar(op, user, motivo=data.get('motivo', ''), hasta=_fecha_local(data.get('hasta')))
        return ok(mensaje='Prospecto pausado', recargar=True)

    def _reanudar(self, request, op, data, user):
        crm.reanudar(op, user)
        return ok(mensaje='Prospecto reactivado', recargar=True)

    def _intento(self, request, op, data, user):
        maximo = crm.registrar_intento(op, user, canal=data.get('canal', 'llamada'),
                                       resultado=data.get('resultado', 'sin_respuesta'), nota=data.get('nota', ''))
        msg = f'Intento #{op.intentos_contacto} registrado'
        return ok(mensaje=msg, max_intentos=maximo, intentos=op.intentos_contacto, recargar=not maximo,
                  sugerencia='Llegó al máximo de intentos: ¿lo cerrás como "Sin respuesta"?' if maximo else '')

    def _email(self, request, op, data, user):
        from apps.automatizaciones.services import enviar_email
        from apps.whatsapp.models import reemplazar_variables_texto
        contacto = op.contacto
        if not contacto.email:
            return error('El contacto no tiene email.')
        if contacto.no_contactar:
            return error('El contacto pidió no ser contactado.')
        asunto, cuerpo = (data.get('asunto') or '').strip(), (data.get('cuerpo') or '').strip()
        if not asunto or not cuerpo:
            return error('Completá el asunto y el mensaje.')
        from apps.automatizaciones.models import PlantillaEmail
        plantilla = PlantillaEmail.objects.filter(pk=data.get('plantilla') or 0).first()
        try:
            enviar_email(contacto, op, reemplazar_variables_texto(asunto, contacto, op),
                         reemplazar_variables_texto(cuerpo, contacto, op), usuario=user, plantilla=plantilla)
        except Exception as e:  # SMTP caído, dirección rechazada…
            return error(f'No se pudo enviar el email: {e}')
        crm.tocar(op)
        crm.marcar_primer_contacto(op)
        crm.avanzar_desde_inicial(op, user)
        return ok(mensaje=f'Email enviado a {contacto.email}', recargar=True)

    def _sms(self, request, op, data, user):
        from apps.integraciones import sms
        from apps.whatsapp.models import reemplazar_variables_texto
        try:
            sms.enviar(op.contacto, reemplazar_variables_texto(data.get('texto', ''), op.contacto, op), usuario=user,
                       oportunidad=op)
        except sms.ErrorSMS as e:
            return error(str(e))
        crm.avanzar_desde_inicial(op, user)
        return ok(mensaje='SMS enviado', recargar=True)

    def _prioridad(self, request, op, data, user):
        poner = not op.prioritaria
        crm.marcar_prioridad(op, data.get('motivo') or f'Marcada por {user.display_name}', usuario=user,
                             prioritaria=poner)
        return ok(mensaje='Marcada como prioridad' if poner else 'Se quitó la prioridad', recargar=True)

    def _nota(self, request, op, data, user):
        texto = (data.get('texto') or '').strip()
        if not texto:
            return error('La nota está vacía.')
        act = crm.agregar_nota(op, user, texto)
        html = render_to_string('crm/_actividad.html', {'a': act}, request=request)
        return ok(html=html)

    def _reasignar(self, request, op, data, user):
        from apps.users.models import User
        if not user.tiene_permiso('reasignar'):
            return error('No tenés permiso para reasignar.', 403)
        agente = get_object_or_404(User, pk=data.get('agente'), is_active=True)
        crm.reasignar(op, agente, user, nota=data.get('nota', ''))
        return ok(mensaje=f'Reasignado a {agente.display_name}', recargar=True)

    def _tarea(self, request, op, data, user):
        form = TareaForm(data, user=user)
        if not form.is_valid():
            return error('; '.join(f'{k}: {v[0]}' for k, v in form.errors.items()))
        d = form.cleaned_data
        crm.crear_tarea(user, d.get('asignado_a') or op.agente or user, d['titulo'], d['vence_at'], oportunidad=op,
                        tipo=d['tipo'], descripcion=d.get('descripcion', ''), prioridad=d['prioridad'])
        return ok(mensaje='Tarea agendada', recargar=True)

    def _pauta(self, request, op, data, user):
        from apps.pautas.models import Pauta
        if not user.tiene_permiso('pautas'):
            return error('No tenés permiso para cambiar la pauta.', 403)
        pauta = Pauta.objects.filter(pk=data.get('pauta') or 0).first()
        antes = auditoria.foto_oportunidad(op)
        op.pauta = pauta
        if pauta and not op.origen_pauta:
            op.origen_pauta = pauta.nombre
        op.save(update_fields=['pauta', 'origen_pauta', 'updated_at'])
        auditoria.registrar_cambios(antes, auditoria.foto_oportunidad(op), user, op.contacto, op)
        return ok(mensaje='Pauta actualizada')

    def _valor(self, request, op, data, user):
        from decimal import Decimal, InvalidOperation
        antes = auditoria.foto_oportunidad(op)
        try:
            op.valor = Decimal(str(data.get('valor'))) if data.get('valor') not in (None, '') else None
        except InvalidOperation:
            return error('Valor inválido')
        op.save(update_fields=['valor', 'updated_at'])
        auditoria.registrar_cambios(antes, auditoria.foto_oportunidad(op), user, op.contacto, op)
        return ok(mensaje='Valor actualizado')


class ContactoArchivoView(LoginRequiredMixin, View):
    """Subir / quitar archivos de los campos personalizados de tipo archivo (ficha)."""

    def post(self, request, pk):
        from . import archivos
        contacto = get_object_or_404(Contacto.objects.visibles_para(request.user), pk=pk)
        campo = get_object_or_404(CampoPersonalizado, slug=request.POST.get('campo'), tipo=CampoPersonalizado.TIPO_ARCHIVO)
        op = Oportunidad.objects.filter(pk=request.POST.get('oportunidad') or 0, contacto=contacto).first()
        destino = request.POST.get('next') or contacto.get_absolute_url()
        try:
            if request.POST.get('quitar', '').isdigit():
                item = archivos.quitar(contacto, campo, int(request.POST['quitar']), request.user, op)
                messages.success(request, f'Se quitó "{item.get("nombre")}".')
            else:
                subidos = request.FILES.getlist('archivo')
                if not subidos:
                    raise archivos.ErrorArchivo('Elegí un archivo.')
                for f in subidos:
                    archivos.adjuntar(contacto, campo, f.read(), f.name, request.user, op)
                messages.success(request, f'Archivo{"s" if len(subidos) > 1 else ""} guardado{"s" if len(subidos) > 1 else ""} en «{campo.nombre}».')
        except archivos.ErrorArchivo as e:
            messages.error(request, str(e))
        return redirect(destino)


class ContactoEditarView(LoginRequiredMixin, View):
    def post(self, request, pk):
        contacto = get_object_or_404(Contacto.objects.visibles_para(request.user), pk=pk)
        antes = auditoria.foto_contacto(contacto)
        form = ContactoForm(request.POST, instance=contacto)
        destino = request.POST.get('next') or contacto.get_absolute_url()
        if form.is_valid():
            form.save()
            contacto.refresh_from_db()
            op = Oportunidad.objects.filter(pk=request.POST.get('oportunidad') or 0, contacto=contacto).first()
            auditoria.registrar_cambios(antes, auditoria.foto_contacto(contacto), request.user, contacto, op)
            messages.success(request, 'Datos del contacto actualizados.')
        else:
            for campo, errs in form.errors.items():
                messages.error(request, f'{campo}: {errs[0]}' if campo != '__all__' else errs[0])
        return redirect(destino)


# ═══════════════════════════════════════════════════════════════════════════
# Contactos
# ═══════════════════════════════════════════════════════════════════════════

class ContactoListView(LoginRequiredMixin, View):
    def get(self, request):
        qs = Contacto.objects.visibles_para(request.user)
        q = request.GET.get('q', '').strip()
        if q:
            qs = qs.filter(crm.filtro_busqueda_contacto(q))
        if request.GET.get('etiqueta'):
            qs = qs.filter(etiquetas__id=request.GET['etiqueta'])
        if request.GET.get('no_contactar'):
            qs = qs.filter(no_contactar=True)
        activas = Oportunidad.objects.filter(estado__in=Oportunidad.ESTADOS_ACTIVOS).select_related('embudo', 'etapa', 'agente')
        qs = qs.prefetch_related(Prefetch('oportunidades', queryset=activas, to_attr='activas'), 'etiquetas')
        return render(request, 'crm/contactos.html', {
            'page': paginar(request, qs.order_by('-created_at'), 50), 'q': q, 'query': query_sin_page(request),
            'etiquetas': Etiqueta.objects.all(), 'filtros': request.GET,
        })


class ContactoDetalleView(LoginRequiredMixin, View):
    def get(self, request, pk):
        contacto = get_object_or_404(Contacto.objects.visibles_para(request.user), pk=pk)
        ops = contacto.oportunidades.select_related('embudo', 'etapa', 'agente', 'tipificacion').order_by('-created_at')
        activa = next((o for o in ops if o.activa), None)
        if activa and request.GET.get('ficha') != 'contacto' and \
                Oportunidad.objects.visibles_para(request.user).filter(pk=activa.pk).exists():
            return redirect(activa.get_absolute_url())
        ctx = {
            'contacto': contacto, 'oportunidades': ops, 'contacto_form': ContactoForm(instance=contacto),
            'datos_extra': datos_extra_display(contacto),
            'actividades': contacto.actividades.select_related('usuario', 'llamada', 'oportunidad__embudo')[:150],
            'llamadas': contacto.llamadas.select_related('agente').order_by('-inicio_at')[:30],
            'embudos': embudos_visibles(request.user),
        }
        ctx.update(contexto_chat(request, contacto))
        return render(request, 'crm/contacto_detalle.html', ctx)


class ContactoNuevaOportunidadView(LoginRequiredMixin, View):
    def post(self, request, pk):
        contacto = get_object_or_404(Contacto.objects.visibles_para(request.user), pk=pk)
        embudo = get_object_or_404(embudos_visibles(request.user), pk=request.POST.get('embudo'))
        res = crm.ingresar_prospecto({'telefono': contacto.telefono, 'email': contacto.email, 'dni': contacto.dni},
                                     embudo, Oportunidad.ORIGEN_MANUAL, usuario=request.user,
                                     agente=None if request.user.tiene_permiso('reasignar') else request.user)
        if res.oportunidad_nueva:
            messages.success(request, 'Oportunidad creada.')
            return redirect(res.oportunidad.get_absolute_url())
        messages.warning(request, 'No se creó: el contacto ya tiene una oportunidad en curso, ya es cliente o '
                                  'está marcado como "no contactar".')
        return redirect(contacto.get_absolute_url() + '?ficha=contacto')


class ContactoEliminarView(PermisoRequeridoMixin, View):
    permiso = 'eliminar'

    def post(self, request, pk):
        contacto = get_object_or_404(Contacto, pk=pk)
        nombre = contacto.nombre
        contacto.delete()
        messages.success(request, f'Contacto "{nombre}" eliminado con todo su historial.')
        return redirect('crm:contactos')


class BuscarView(LoginRequiredMixin, View):
    """Búsqueda global (barra superior) y detección de duplicados en el alta."""

    def get(self, request):
        q = request.GET.get('q', '').strip()
        if len(q) < 2:
            return JsonResponse({'resultados': []})
        contactos = (Contacto.objects.visibles_para(request.user).filter(crm.filtro_busqueda_contacto(q))
                     .prefetch_related(Prefetch('oportunidades', queryset=Oportunidad.objects.select_related('etapa', 'agente')
                                                .order_by('-created_at'), to_attr='ops'))[:10])
        resultados = []
        for c in contactos:
            op = c.ops[0] if c.ops else None
            resultados.append({
                'id': c.pk, 'nombre': c.nombre, 'telefono': c.telefono, 'email': c.email,
                'url': (op.get_absolute_url() if op else c.get_absolute_url()),
                'detalle': f'{op.etapa} · {op.get_estado_display()} · {op.agente.display_name if op.agente else "sin agente"}'
                if op else 'Sin oportunidades',
            })
        return JsonResponse({'resultados': resultados})


# ═══════════════════════════════════════════════════════════════════════════
# Mi día (cola de trabajo del agente) y tareas
# ═══════════════════════════════════════════════════════════════════════════

class MiDiaView(LoginRequiredMixin, View):
    def get(self, request):
        user = request.user
        hoy = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
        cola = crm.cola_de_trabajo(user)
        from apps.telefonia.models import Llamada
        stats = {
            'abiertas': Oportunidad.objects.filter(agente=user, estado=Oportunidad.ESTADO_ABIERTA).count(),
            'pausadas': Oportunidad.objects.filter(agente=user, estado=Oportunidad.ESTADO_PAUSADA).count(),
            'intentos_hoy': Actividad.objects.filter(usuario=user, created_at__gte=hoy,
                                                     tipo__in=[Actividad.TIPO_INTENTO, Actividad.TIPO_LLAMADA]).count(),
            'llamadas_hoy': Llamada.objects.filter(agente=user, inicio_at__gte=hoy).count(),
            'ventas_mes': Oportunidad.objects.filter(agente=user, estado=Oportunidad.ESTADO_GANADA,
                                                     cerrada_at__gte=hoy.replace(day=1)).count(),
            'ventas_hoy': Oportunidad.objects.filter(agente=user, estado=Oportunidad.ESTADO_GANADA,
                                                     cerrada_at__gte=hoy).count(),
        }
        return render(request, 'crm/mi_dia.html', {'cola': cola, 'stats': stats})


class SiguienteView(LoginRequiredMixin, View):
    def get(self, request):
        op = crm.siguiente_prospecto(request.user)
        if op is None:
            messages.info(request, '¡No tenés prospectos pendientes! 🎉')
            return redirect('crm:mi_dia')
        return redirect(op.get_absolute_url() + '?desde=cola')


class TareaListView(LoginRequiredMixin, View):
    def get(self, request):
        user = request.user
        qs = Tarea.objects.select_related('oportunidad__contacto', 'contacto', 'asignado_a')
        de = request.GET.get('de', 'mias')
        if de == 'todas' and user.ve_todo:
            if request.GET.get('agente'):
                qs = qs.filter(asignado_a_id=request.GET['agente'])
        else:
            qs = qs.filter(asignado_a=user)
        filtro = request.GET.get('f', 'pendientes')
        ahora = timezone.now()
        fin_dia = timezone.localtime().replace(hour=23, minute=59, second=59)
        if filtro == 'vencidas':
            qs = qs.filter(estado=Tarea.ESTADO_PENDIENTE, vence_at__lt=ahora)
        elif filtro == 'hoy':
            qs = qs.filter(estado=Tarea.ESTADO_PENDIENTE, vence_at__lte=fin_dia)
        elif filtro == 'completadas':
            qs = qs.filter(estado=Tarea.ESTADO_COMPLETADA).order_by('-completada_at')
        else:
            qs = qs.filter(estado=Tarea.ESTADO_PENDIENTE)
        return render(request, 'crm/tareas.html', {
            'page': paginar(request, qs, 50), 'f': filtro, 'de': de, 'agentes': agentes_activos(),
            'query': query_sin_page(request), 'filtros': request.GET,
        })


class TareaAccionView(LoginRequiredMixin, View):
    def post(self, request, pk, accion):
        tarea = get_object_or_404(Tarea, pk=pk)
        if tarea.asignado_a_id != request.user.pk and not request.user.ve_todo:
            return error('No es tu tarea.', 403)
        data = json_body(request)
        if accion == 'completar':
            crm.completar_tarea(tarea, request.user, data.get('resultado', ''))
            return ok(mensaje='Tarea completada')
        if accion == 'cancelar':
            tarea.estado = Tarea.ESTADO_CANCELADA
            tarea.save(update_fields=['estado'])
            crm.registrar_tarea(tarea, request.user, f'Tarea cancelada: {tarea.titulo}')
            crm.invalidar_tareas(tarea.asignado_a)
            return ok(mensaje='Tarea cancelada')
        if accion == 'posponer':
            horas = int(data.get('horas', 24))
            tarea.vence_at = max(tarea.vence_at, timezone.now()) + timedelta(hours=horas)
            tarea.notificada_vencida = False
            tarea.save(update_fields=['vence_at', 'notificada_vencida'])
            crm.registrar_tarea(tarea, request.user, f'Tarea pospuesta: {tarea.titulo} → vence '
                                                     f'{timezone.localtime(tarea.vence_at):%d/%m %H:%M}')
            crm.invalidar_tareas(tarea.asignado_a)
            return ok(mensaje='Tarea pospuesta')
        return error('Acción desconocida', 404)


# ═══════════════════════════════════════════════════════════════════════════
# Importaciones
# ═══════════════════════════════════════════════════════════════════════════

class ImportacionListView(PermisoRequeridoMixin, View):
    permiso = 'importar'

    def get(self, request):
        return render(request, 'crm/importaciones.html', {
            'lotes': ImportacionLote.objects.select_related('embudo', 'creado_por')[:100],
            'form': ImportacionForm(initial={'embudo': embudo_actual(request)[0]}),
        })

    def post(self, request):
        from .importacion import leer_archivo, sugerir_mapeo
        form = ImportacionForm(request.POST, request.FILES)
        if not form.is_valid():
            return render(request, 'crm/importaciones.html', {
                'lotes': ImportacionLote.objects.select_related('embudo', 'creado_por')[:100], 'form': form})
        archivo = form.cleaned_data['archivo']
        try:
            columnas, _ = leer_archivo(archivo, archivo.name, limite=1)
        except Exception as e:
            form.add_error('archivo', f'No se pudo leer el archivo: {e}')
            return render(request, 'crm/importaciones.html', {'lotes': ImportacionLote.objects.all()[:100], 'form': form})
        archivo.seek(0)
        lote = ImportacionLote.objects.create(
            archivo=archivo, nombre_archivo=archivo.name, embudo=form.cleaned_data['embudo'],
            fuente=form.cleaned_data.get('fuente') or archivo.name, mapeo=sugerir_mapeo(columnas),
            creado_por=request.user,
        )
        return redirect('crm:importacion_mapeo', pk=lote.pk)


class ImportacionMapeoView(PermisoRequeridoMixin, View):
    permiso = 'importar'

    def get(self, request, pk):
        from .importacion import campos_importacion, leer_archivo
        lote = get_object_or_404(ImportacionLote, pk=pk, estado=ImportacionLote.ESTADO_PENDIENTE)
        with lote.archivo.open('rb') as f:
            columnas, filas = leer_archivo(f, lote.nombre_archivo, limite=5)
        return render(request, 'crm/importacion_mapeo.html', {
            'lote': lote, 'columnas': columnas, 'filas': filas, 'campos': campos_importacion(),
            'etapas': [e for e in etapas_de(lote.embudo) if not e.es_cierre], 'agentes': lote.embudo.agentes.filter(is_active=True),
            'pautas': Pauta.objects.filter(activa=True),
        })

    def post(self, request, pk):
        from .importacion import campos_importacion
        from .tasks import procesar_importacion
        lote = get_object_or_404(ImportacionLote, pk=pk, estado=ImportacionLote.ESTADO_PENDIENTE)
        validos = {c for c, _ in campos_importacion()}
        mapeo = {}
        for col in lote.mapeo:
            campo = request.POST.get(f'col__{col}', '')
            mapeo[col] = campo if campo in validos else ''
        if not {'telefono', 'email', 'dni'} & set(mapeo.values()):
            messages.error(request, 'Tenés que indicar al menos la columna de teléfono, email o DNI.')
            return redirect('crm:importacion_mapeo', pk=lote.pk)
        lote.mapeo = mapeo
        etapa_id = request.POST.get('etapa')
        lote.etapa = Etapa.objects.filter(pk=etapa_id, embudo=lote.embudo).first() if etapa_id else None
        lote.asignar_automaticamente = request.POST.get('asignar') == '1'
        lote.agente_fijo_id = request.POST.get('agente_fijo') or None
        lote.disparar_automatizaciones = request.POST.get('automatizaciones') == '1'
        from apps.pautas.models import Pauta
        lote.pauta = Pauta.objects.filter(pk=request.POST.get('pauta') or 0).first()
        lote.save()
        procesar_importacion.delay(lote.pk)
        messages.success(request, 'Importación en proceso. Podés seguir trabajando: te avisamos cuando termine.')
        return redirect('crm:importacion_detalle', pk=lote.pk)


class ImportacionDetalleView(PermisoRequeridoMixin, View):
    permiso = 'importar'

    def get(self, request, pk):
        lote = get_object_or_404(ImportacionLote.objects.select_related('embudo', 'creado_por'), pk=pk)
        if request.GET.get('json'):
            return JsonResponse({'estado': lote.estado, 'porcentaje': lote.porcentaje, 'total': lote.total,
                                 'procesados': lote.procesados, 'creados': lote.creados,
                                 'ya_existentes': lote.ya_existentes, 'errores': lote.errores})
        from apps.reportes.analisis import gestion_por
        from django.utils import timezone as tz
        ops = lote.oportunidades.all()
        resumen = ops.aggregate(leads=Count('pk'), ventas=Count('pk', filter=Q(estado=Oportunidad.ESTADO_GANADA)),
                                perdidas=Count('pk', filter=Q(estado=Oportunidad.ESTADO_PERDIDA)),
                                efectivos=Count('pk', filter=Q(contacto_efectivo_at__isnull=False)),
                                en_curso=Count('pk', filter=Q(estado__in=Oportunidad.ESTADOS_ACTIVOS)))
        gestion = gestion_por('lote', lote.created_at, tz.now()).get(lote.pk, {}) if resumen['leads'] else {}
        return render(request, 'crm/importacion_detalle.html', {'lote': lote, 'resumen': resumen, 'gestion': gestion})


class ImportacionPlantillaView(LoginRequiredMixin, View):
    """Excel de ejemplo con las columnas sugeridas."""

    def get(self, request):
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = 'Prospectos'
        ws.append(['Nombre y apellido', 'Teléfono', 'Email', 'DNI', 'Fecha de atención', 'Especialidad', 'Localidad'])
        ws.append(['María Gómez', '11 5555-1234', 'maria@ejemplo.com', '30123456', '15/09/2026', 'Clínica médica', 'CABA'])
        resp = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        resp['Content-Disposition'] = 'attachment; filename="plantilla_prospectos.xlsx"'
        wb.save(resp)
        return resp


# ═══════════════════════════════════════════════════════════════════════════
# Configuración de embudos, etapas, tipificaciones y etiquetas
# ═══════════════════════════════════════════════════════════════════════════

class EmbudoListView(PermisoRequeridoMixin, View):
    permiso = 'embudos'

    def get(self, request):
        embudos = Embudo.objects.annotate(
            n_activas=Count('oportunidades', filter=Q(oportunidades__estado__in=Oportunidad.ESTADOS_ACTIVOS)),
        ).prefetch_related('etapas', 'agentes')
        return render(request, 'crm/config/embudos.html', {'embudos': embudos})


class EmbudoEditarView(PermisoRequeridoMixin, View):
    permiso = 'embudos'

    def get(self, request, pk=None):
        embudo = get_object_or_404(Embudo, pk=pk) if pk else None
        return self._render(request, embudo, EmbudoForm(instance=embudo))

    def post(self, request, pk=None):
        embudo = get_object_or_404(Embudo, pk=pk) if pk else None
        form = EmbudoForm(request.POST, instance=embudo)
        if form.is_valid():
            nuevo = embudo is None
            embudo = form.save()
            if nuevo:
                for orden, (nombre, color, tipo) in enumerate([
                    ('Nuevo', '#3B7EF6', Etapa.TIPO_NORMAL), ('En gestión', '#06B6D4', Etapa.TIPO_NORMAL),
                    ('Contacto efectivo', '#7C5CFC', Etapa.TIPO_NORMAL), ('Venta', '#22C97A', Etapa.TIPO_GANADO),
                    ('No venta', '#EF4444', Etapa.TIPO_PERDIDO)], start=1):
                    Etapa.objects.create(embudo=embudo, nombre=nombre, color=color, tipo=tipo, orden=orden,
                                         marca_contacto_efectivo=orden == 3)
                messages.success(request, 'Embudo creado con etapas base: ajustalas abajo.')
            else:
                messages.success(request, 'Embudo guardado.')
            return redirect('crm:embudo_editar', pk=embudo.pk)
        return self._render(request, embudo, form)

    def _render(self, request, embudo, form):
        ctx = {'embudo': embudo, 'form': form}
        from .forms import ResultadoGestionForm
        from .models import ResultadoGestion
        if embudo:
            ctx.update({
                'etapas': embudo.etapas.annotate(n=Count('oportunidades')).order_by('orden', 'pk'),
                'tipificaciones': Tipificacion.objects.filter(Q(embudo=embudo) | Q(embudo__isnull=True))
                .order_by('resultado', 'orden', 'categoria'),
                'etapa_form': EtapaForm(), 'tip_form': TipificacionForm(),
                'resultados': ResultadoGestion.objects.filter(Q(embudo=embudo) | Q(embudo__isnull=True))
                .select_related('mover_a'),
                'res_form': ResultadoGestionForm(embudo=embudo, initial={'activo': True}),
                'reglas': embudo.reglas_asignacion.prefetch_related('agentes', 'pautas'),
                'conectados': presencia.conectados(embudo.agentes.values_list('pk', flat=True)),
                'campos_requeribles': [(c, label) for c, label, *_ in CAMPOS_FIJOS_REQUERIBLES] + [
                    (f'cp:{c.slug}', f'★ {c.nombre}') for c in CampoPersonalizado.activos(embudo)],
            })
        return render(request, 'crm/config/embudo_form.html', ctx)


class ReglaAsignacionView(PermisoRequeridoMixin, View):
    permiso = 'embudos'

    def _obtener(self, embudo_pk, pk):
        embudo = get_object_or_404(Embudo, pk=embudo_pk)
        return embudo, (get_object_or_404(embudo.reglas_asignacion, pk=pk) if pk else None)

    def get(self, request, embudo_pk, pk=None):
        embudo, regla = self._obtener(embudo_pk, pk)
        inicial = None if regla else {'orden': embudo.reglas_asignacion.count() + 1}
        form = ReglaAsignacionForm(instance=regla, embudo=embudo, initial=inicial)
        return render(request, 'crm/config/regla_form.html', {'embudo': embudo, 'regla': regla, 'form': form})

    def post(self, request, embudo_pk, pk=None):
        embudo, regla = self._obtener(embudo_pk, pk)
        if regla and request.POST.get('eliminar'):
            regla.delete()
            messages.success(request, f'Regla "{regla.nombre}" eliminada.')
            return redirect(reverse('crm:embudo_editar', args=[embudo.pk]) + '#reglas')
        form = ReglaAsignacionForm(request.POST, instance=regla, embudo=embudo)
        if not form.is_valid():
            return render(request, 'crm/config/regla_form.html', {'embudo': embudo, 'regla': regla, 'form': form})
        regla = form.save()
        messages.success(request, f'Regla "{regla.nombre}" guardada. Se aplica a los leads que entren desde ahora.')
        return redirect(reverse('crm:embudo_editar', args=[embudo.pk]) + '#reglas')


class EtapaAccionView(PermisoRequeridoMixin, View):
    permiso = 'embudos'

    def post(self, request, embudo_pk, accion, pk=None):
        embudo = get_object_or_404(Embudo, pk=embudo_pk)
        etapa = get_object_or_404(Etapa, pk=pk, embudo=embudo) if pk else None
        if accion == 'guardar':
            form = EtapaForm(request.POST, instance=etapa)
            if form.is_valid():
                nueva = form.save(commit=False)
                nueva.embudo = embudo
                if 'requeridos_enviados' in request.POST:
                    validas = {c for c, *_ in CAMPOS_FIJOS_REQUERIBLES} | {
                        f'cp:{slug}' for slug in CampoPersonalizado.objects.values_list('slug', flat=True)}
                    nueva.campos_requeridos = [c for c in request.POST.getlist('campos_requeridos') if c in validas]
                if etapa is None:
                    ultima = embudo.etapas.filter(tipo=Etapa.TIPO_NORMAL).order_by('-orden').first()
                    nueva.orden = (ultima.orden + 1) if ultima else 1
                    embudo.etapas.filter(orden__gte=nueva.orden).exclude(pk=nueva.pk).update(orden=F('orden') + 1)
                nueva.save()
                messages.success(request, f'Etapa "{nueva}" guardada.')
            else:
                messages.error(request, f'Revisá la etapa: {form.errors.as_text()}')
        elif accion in ('subir', 'bajar') and etapa:
            etapas = list(embudo.etapas.order_by('orden', 'pk'))
            i = etapas.index(etapa)
            j = i - 1 if accion == 'subir' else i + 1
            if 0 <= j < len(etapas):
                etapas[i], etapas[j] = etapas[j], etapas[i]
                for orden, e in enumerate(etapas, start=1):
                    Etapa.objects.filter(pk=e.pk).update(orden=orden)
        elif accion == 'eliminar' and etapa:
            if etapa.oportunidades.exists():
                messages.error(request, f'No se puede eliminar "{etapa}": tiene oportunidades. Movelas antes.')
            else:
                etapa.delete()
                messages.success(request, 'Etapa eliminada.')
        return redirect(reverse('crm:embudo_editar', args=[embudo.pk]) + '#etapas')


class TipificacionAccionView(PermisoRequeridoMixin, View):
    permiso = 'embudos'

    def post(self, request, embudo_pk, pk=None):
        embudo = get_object_or_404(Embudo, pk=embudo_pk)
        tip = get_object_or_404(Tipificacion, pk=pk) if pk else None
        if request.POST.get('accion') == 'toggle' and tip:
            tip.activa = not tip.activa
            tip.save(update_fields=['activa'])
        else:
            form = TipificacionForm(request.POST, instance=tip)
            if form.is_valid():
                t = form.save(commit=False)
                if t.embudo_id is None and tip is None:
                    t.embudo = embudo
                t.save()
                messages.success(request, f'Tipificación "{t}" guardada.')
            else:
                messages.error(request, f'Revisá la tipificación: {form.errors.as_text()}')
        return redirect(reverse('crm:embudo_editar', args=[embudo.pk]) + '#tipificaciones')


class ResultadoGestionView(PermisoRequeridoMixin, View):
    permiso = 'embudos'

    def post(self, request, embudo_pk, pk=None):
        from .forms import ResultadoGestionForm
        from .models import ResultadoGestion
        embudo = get_object_or_404(Embudo, pk=embudo_pk)
        res = get_object_or_404(ResultadoGestion, pk=pk) if pk else None
        if res and request.POST.get('accion') == 'toggle':
            res.activo = not res.activo
            res.save(update_fields=['activo'])
        else:
            form = ResultadoGestionForm(request.POST, instance=res, embudo=embudo)
            if form.is_valid():
                r = form.save(commit=False)
                if res is None and request.POST.get('solo_este'):
                    r.embudo = embudo
                r.save()
                messages.success(request, f'Resultado "{r}" guardado.')
            else:
                messages.error(request, f'Revisá el resultado: {form.errors.as_text()}')
        return redirect(reverse('crm:embudo_editar', args=[embudo.pk]) + '#resultados')


class EtiquetaView(LoginRequiredMixin, View):
    def get(self, request):
        return render(request, 'crm/config/etiquetas.html', {
            'etiquetas': Etiqueta.objects.annotate(n=Count('contactos')), 'form': EtiquetaForm()})

    def post(self, request):
        if request.POST.get('eliminar'):
            if not request.user.tiene_permiso('embudos'):
                raise PermissionDenied
            Etiqueta.objects.filter(pk=request.POST['eliminar']).delete()
        else:
            form = EtiquetaForm(request.POST)
            if form.is_valid():
                form.save()
            else:
                messages.error(request, form.errors.as_text())
        return redirect('crm:etiquetas')


# ═══════════════════════════════════════════════════════════════════════════
# Supervisión
# ═══════════════════════════════════════════════════════════════════════════

class SupervisionView(PermisoRequeridoMixin, View):
    permiso = 'supervision'

    def get(self, request):
        from apps.telefonia.models import Llamada
        from apps.users.models import User
        from apps.whatsapp.models import Conversacion
        ahora = timezone.now()
        hoy = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
        embudo, embudos = embudo_actual(request)
        ops = Oportunidad.objects.filter(embudo=embudo) if embudo else Oportunidad.objects.all()
        # Agregaciones por tabla (sin joins cruzados): escala a cientos de miles de oportunidades.
        por_agente = {r['agente']: r for r in ops.filter(agente__isnull=False).values('agente').annotate(
            abiertas=Count('pk', filter=Q(estado=Oportunidad.ESTADO_ABIERTA)),
            sin_gestion=Count('pk', filter=Q(estado=Oportunidad.ESTADO_ABIERTA, intentos_contacto=0)),
            ventas_mes=Count('pk', filter=Q(estado=Oportunidad.ESTADO_GANADA, cerrada_at__gte=hoy.replace(day=1))),
        )}
        vencidas = dict(Tarea.objects.filter(estado=Tarea.ESTADO_PENDIENTE, vence_at__lt=ahora, asignado_a__isnull=False)
                        .values_list('asignado_a').annotate(n=Count('pk')).values_list('asignado_a', 'n'))
        ids = set(embudo.agentes.values_list('pk', flat=True)) if embudo else set()
        ids |= {k for k, r in por_agente.items() if r['abiertas']}
        en_llamada = set(Llamada.objects.filter(estado__in=Llamada.ESTADOS_VIVOS,
                                                inicio_at__gte=ahora - timedelta(hours=3)).values_list('agente_id', flat=True))
        llamadas_hoy = dict(Llamada.objects.filter(inicio_at__gte=hoy).values_list('agente').annotate(n=Count('pk'))
                            .values_list('agente', 'n'))
        from apps.telefonia.services import q_sin_calificar
        sin_calificar = dict(Llamada.objects.filter(q_sin_calificar(), inicio_at__gte=ahora - timedelta(days=30))
                             .values_list('agente').annotate(n=Count('pk')).values_list('agente', 'n'))
        prioritarias = dict(ops.filter(prioritaria=True, estado=Oportunidad.ESTADO_ABIERTA, agente__isnull=False)
                            .values_list('agente').annotate(n=Count('pk')).values_list('agente', 'n'))
        wa = dict(Conversacion.objects.filter(archivada=False, no_leidos__gt=0).values_list('agente')
                  .annotate(n=Count('pk')).values_list('agente', 'n'))
        from django.db.models import Max
        from apps.users.models import SesionConexion
        online = presencia.conectados(ids)
        ausentes = presencia.ausentes(online, en_llamada=en_llamada)
        ultima_vez = dict(SesionConexion.objects.filter(usuario_id__in=ids).values_list('usuario')
                          .annotate(u=Max('ultimo')).values_list('usuario', 'u'))
        sesion_desde = dict(SesionConexion.objects.filter(usuario_id__in=online, fin__isnull=True,
                                                          ultimo__gte=ahora - timedelta(minutes=10))
                            .values_list('usuario').annotate(i=Max('inicio')).values_list('usuario', 'i'))
        filas = []
        for a in User.objects.filter(pk__in=ids, is_active=True).order_by('first_name', 'username'):
            r = por_agente.get(a.pk, {})
            a.abiertas, a.sin_gestion, a.ventas_mes = r.get('abiertas', 0), r.get('sin_gestion', 0), r.get('ventas_mes', 0)
            a.tareas_vencidas = vencidas.get(a.pk, 0)
            filas.append({'u': a, 'en_llamada': a.pk in en_llamada, 'llamadas_hoy': llamadas_hoy.get(a.pk, 0),
                          'wa': wa.get(a.pk, 0), 'online': a.pk in online, 'desde': sesion_desde.get(a.pk),
                          'ausente_desde': ausentes.get(a.pk), 'sin_calificar': sin_calificar.get(a.pk, 0),
                          'prioritarias': prioritarias.get(a.pk, 0),
                          'ultima_vez': ultima_vez.get(a.pk)})
        filas.sort(key=lambda f: (not f['online'], f['u'].display_name.lower()))
        return render(request, 'crm/supervision.html', {
            'embudo': embudo, 'embudos': embudos, 'filas': filas, 'n_online': sum(1 for f in filas if f['online'] and not f['ausente_desde']),
            'n_ausentes': sum(1 for f in filas if f['ausente_desde']), 'ausente_min': presencia.ausente_minutos(),
            'sin_asignar': ops.filter(agente__isnull=True, estado=Oportunidad.ESTADO_ABIERTA).count(),
            'sla_vencidos': ops.filter(crm.q_sla_vencido()).count() if embudo and embudo.sla_minutos else None,
            'wa_sin_asignar': Conversacion.objects.filter(agente__isnull=True, archivada=False).exclude(
                estado=Conversacion.ESTADO_CERRADA).count(),
            'estancados': ops.filter(estado=Oportunidad.ESTADO_ABIERTA, etapa__tipo=Etapa.TIPO_NORMAL,
                                     etapa_desde__lt=ahora - timedelta(days=(embudo.dias_estancado_alerta or 7)
                                                                        if embudo else 7)).count(),
            'agentes_todos': agentes_activos(), 'total_sin_calificar': sum(sin_calificar.values()),
        })

    def post(self, request):
        """Asignar la cola sin agente, o redistribuir todo lo abierto de un agente entre el resto."""
        from apps.users.models import User
        if not request.user.tiene_permiso('reasignar'):
            raise PermissionDenied
        embudo, _ = embudo_actual(request)
        if request.POST.get('accion') == 'asignar_cola':
            n = sum(1 for op in Oportunidad.objects.filter(embudo=embudo, agente__isnull=True,
                                                           estado=Oportunidad.ESTADO_ABIERTA).select_related('embudo', 'contacto')
                    if crm.asignar_oportunidad(op, forzar_horario=True))
            messages.success(request, f'{n} prospectos asignados.')
            return redirect('crm:supervision')
        origen = get_object_or_404(User, pk=request.POST.get('agente'))
        n = 0
        for op in (Oportunidad.objects.filter(agente=origen, estado__in=Oportunidad.ESTADOS_ACTIVOS, embudo=embudo)
                   .select_related('embudo', 'contacto')):
            nuevo = crm.elegir_y_reasignar(op, excluir=[origen], usuario=request.user)
            if nuevo:
                n += 1
        messages.success(request, f'{n} oportunidades de {origen.display_name} redistribuidas.')
        return redirect('crm:supervision')


class CampoListView(PermisoRequeridoMixin, View):
    permiso = 'campos'

    def get(self, request, pk=None):
        campo = get_object_or_404(CampoPersonalizado, pk=pk) if pk else None
        return self._render(request, CampoPersonalizadoForm(instance=campo), campo)

    def post(self, request, pk=None):
        campo = get_object_or_404(CampoPersonalizado, pk=pk) if pk else None
        if campo and request.POST.get('eliminar'):
            campo.delete()
            messages.success(request, f'Campo "{campo}" eliminado. Los valores ya cargados se conservan en los contactos.')
            return redirect('crm:campos')
        form = CampoPersonalizadoForm(request.POST, instance=campo)
        if form.is_valid():
            c = form.save()
            clave, elegidas = f'cp:{c.slug}', set(request.POST.getlist('etapas_requeridas'))
            for etapa in Etapa.objects.all():
                lista = list(etapa.campos_requeridos or [])
                tiene, debe = clave in lista, str(etapa.pk) in elegidas
                if tiene != debe:
                    etapa.campos_requeridos = [x for x in lista if x != clave] + ([clave] if debe else [])
                    etapa.save(update_fields=['campos_requeridos'])
            messages.success(request, f'Campo "{c}" guardado. Clave para API / importación / mensajes: {c.slug}')
            return redirect('crm:campos')
        return self._render(request, form, campo)

    def _render(self, request, form, campo):
        etapas = list(Etapa.objects.select_related('embudo').exclude(tipo=Etapa.TIPO_PERDIDO).order_by('embudo__orden', 'embudo__nombre', 'orden'))
        clave = f'cp:{campo.slug}' if campo else None
        campos = list(CampoPersonalizado.objects.select_related('embudo'))
        for c in campos:
            c.etapas_obligatorio = [e for e in etapas if f'cp:{c.slug}' in (e.campos_requeridos or [])]
        return render(request, 'crm/config/campos.html', {
            'campos': campos, 'form': form, 'campo': campo, 'etapas': etapas,
            'etapas_sel': {e.pk for e in etapas if clave and clave in (e.campos_requeridos or [])}})


class PuntajeView(PermisoRequeridoMixin, View):
    """Configuración → Puntaje de leads: reglas de lead scoring."""
    permiso = 'embudos'

    def get(self, request):
        from apps.pautas.models import Pauta
        from .models import ReglaPuntaje
        return render(request, 'crm/config/puntaje.html', {
            'reglas': ReglaPuntaje.objects.select_related('embudo', 'pauta', 'etapa'),
            'condiciones': ReglaPuntaje.CONDICIONES, 'embudos': Embudo.objects.filter(activo=True),
            'pautas': Pauta.objects.all(), 'canales': Oportunidad.ORIGEN_CHOICES,
            'etapas': Etapa.objects.select_related('embudo').order_by('embudo__nombre', 'orden'),
            'campos': [(c, l) for c, l, *_ in CAMPOS_FIJOS_REQUERIBLES] + [
                (c.slug, f'★ {c.nombre}') for c in CampoPersonalizado.objects.filter(activo=True)],
            'distribucion': list(Oportunidad.objects.filter(estado__in=Oportunidad.ESTADOS_ACTIVOS)
                                 .values('puntaje').annotate(n=Count('pk')).order_by('-puntaje')[:15]),
        })

    def post(self, request):
        from apps.pautas.models import Pauta
        from .models import ReglaPuntaje
        from .puntaje import recalcular_todos
        accion = request.POST.get('accion')
        if accion == 'borrar':
            ReglaPuntaje.objects.filter(pk=request.POST.get('id')).delete()
        elif accion == 'toggle':
            r = ReglaPuntaje.objects.filter(pk=request.POST.get('id')).first()
            if r:
                r.activa = not r.activa
                r.save(update_fields=['activa'])
        elif accion == 'nueva':
            d = request.POST
            cond = d.get('condicion')
            try:
                puntos = int(d.get('puntos') or 0)
            except ValueError:
                puntos = 0
            if cond not in dict(ReglaPuntaje.CONDICIONES) or not puntos:
                messages.error(request, 'Elegí la condición y los puntos (distinto de 0).')
                return redirect('crm:puntaje')
            ReglaPuntaje.objects.create(
                condicion=cond, puntos=puntos, embudo=Embudo.objects.filter(pk=d.get('embudo') or 0).first(),
                pauta=Pauta.objects.filter(pk=d.get('pauta') or 0).first() if cond == ReglaPuntaje.COND_PAUTA else None,
                etapa=Etapa.objects.filter(pk=d.get('etapa') or 0).first() if cond == ReglaPuntaje.COND_ETAPA else None,
                campo=(d.get('campo') or '')[:80], valor=(d.get('valor') or d.get('canal') or '')[:200],
                numero=int(d.get('numero') or 0) if str(d.get('numero') or '0').isdigit() else 0,
            )
        n = recalcular_todos()
        messages.success(request, f'Listo. Se recalculó el puntaje de los leads en curso ({n} cambiaron).')
        return redirect('crm:puntaje')
