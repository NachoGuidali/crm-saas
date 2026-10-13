import csv
from datetime import datetime, timedelta

from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Avg, Count, DurationField, ExpressionWrapper, F, Q, Sum
from django.db.models.functions import TruncDate
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views import View

from core.permisos import PermisoRequeridoMixin


def inicio(request):
    if not request.user.is_authenticated:
        return redirect('users:login')
    if request.user.tiene_permiso('reportes'):
        return redirect('reportes:dashboard')
    return redirect('crm:mi_dia')


def _periodo(request):
    hoy = timezone.localdate()
    p = request.GET.get('p', '30d')
    if p == 'hoy':
        desde = hoy
    elif p == '7d':
        desde = hoy - timedelta(days=6)
    elif p == 'mes':
        desde = hoy.replace(day=1)
    elif p == 'custom' and request.GET.get('desde'):
        try:
            desde = datetime.strptime(request.GET['desde'], '%Y-%m-%d').date()
        except ValueError:
            desde = hoy - timedelta(days=29)
    else:
        p, desde = '30d', hoy - timedelta(days=29)
    hasta = hoy
    if p == 'custom' and request.GET.get('hasta'):
        try:
            hasta = datetime.strptime(request.GET['hasta'], '%Y-%m-%d').date()
        except ValueError:
            pass
    tz = timezone.get_current_timezone()
    ini = timezone.make_aware(datetime.combine(desde, datetime.min.time()), tz)
    fin = timezone.make_aware(datetime.combine(hasta, datetime.max.time()), tz)
    return p, desde, hasta, ini, fin


def _pct(a, b):
    return round(a * 100 / b, 1) if b else 0


def metricas(embudo, ini, fin, agente=None):
    from apps.crm.models import Etapa, Oportunidad, Tarea
    from apps.telefonia.models import Llamada
    ops = Oportunidad.objects.filter(embudo=embudo) if embudo else Oportunidad.objects.all()
    if agente:
        ops = ops.filter(agente=agente)
    ingresos = ops.filter(created_at__range=(ini, fin))
    cerradas = ops.filter(cerrada_at__range=(ini, fin))
    n_ingresos = ingresos.count()
    ventas = cerradas.filter(estado=Oportunidad.ESTADO_GANADA).count()
    perdidas = cerradas.filter(estado=Oportunidad.ESTADO_PERDIDA).count()
    efectivos = ingresos.filter(contacto_efectivo_at__isnull=False).count()
    gestionados = ingresos.filter(intentos_contacto__gt=0).count()
    t_contacto = ingresos.filter(contacto_efectivo_at__isnull=False).aggregate(
        t=Avg(ExpressionWrapper(F('contacto_efectivo_at') - F('created_at'), output_field=DurationField())))['t']
    ahora = timezone.now()
    llamadas = Llamada.objects.filter(inicio_at__range=(ini, fin))
    if agente:
        llamadas = llamadas.filter(agente=agente)
    return {
        'ingresos': n_ingresos, 'ventas': ventas, 'perdidas': perdidas,
        'conversion': _pct(ventas, ventas + perdidas),
        'contactabilidad': _pct(efectivos, n_ingresos), 'gestionados': _pct(gestionados, n_ingresos),
        'tiempo_contacto_h': round(t_contacto.total_seconds() / 3600, 1) if t_contacto else None,
        'en_curso': ops.filter(estado=Oportunidad.ESTADO_ABIERTA).count(),
        'pausadas': ops.filter(estado=Oportunidad.ESTADO_PAUSADA).count(),
        'sin_asignar': ops.filter(estado=Oportunidad.ESTADO_ABIERTA, agente__isnull=True).count(),
        'estancados': ops.filter(estado=Oportunidad.ESTADO_ABIERTA, etapa__tipo=Etapa.TIPO_NORMAL,
                                 etapa_desde__lt=ahora - timedelta(days=(embudo.dias_estancado_alerta or 7)
                                                                    if embudo else 7)).count(),
        'tareas_vencidas': Tarea.objects.filter(estado=Tarea.ESTADO_PENDIENTE, vence_at__lt=ahora,
                                                **({'asignado_a': agente} if agente else {})).count(),
        'valor_ventas': cerradas.filter(estado=Oportunidad.ESTADO_GANADA).aggregate(s=Sum('valor'))['s'] or 0,
        'llamadas': llamadas.count(),
        'llamadas_perdidas': llamadas.filter(direccion=Llamada.DIR_ENTRANTE,
                                             estado__in=[Llamada.ESTADO_NO_ATENDIDA, Llamada.ESTADO_OCUPADO]).count(),
        'minutos': round((llamadas.aggregate(s=Sum('duracion_seg'))['s'] or 0) / 60),
    }


def ranking_agentes(embudo, ini, fin):
    from apps.crm.models import Oportunidad
    from apps.telefonia.models import Llamada
    from apps.users.models import User
    ops = Oportunidad.objects.filter(embudo=embudo) if embudo else Oportunidad.objects.all()
    stats = {r['agente']: r for r in ops.filter(agente__isnull=False).values('agente').annotate(
        asignados=Count('pk', filter=Q(asignada_at__range=(ini, fin))),
        efectivos=Count('pk', filter=Q(contacto_efectivo_at__range=(ini, fin))),
        ventas=Count('pk', filter=Q(estado=Oportunidad.ESTADO_GANADA, cerrada_at__range=(ini, fin))),
        perdidas=Count('pk', filter=Q(estado=Oportunidad.ESTADO_PERDIDA, cerrada_at__range=(ini, fin))),
        abiertas=Count('pk', filter=Q(estado=Oportunidad.ESTADO_ABIERTA)),
    )}
    usuarios = []
    for u in User.objects.filter(pk__in=list(stats), is_active=True):
        r = stats[u.pk]
        if r['asignados'] or r['abiertas'] or r['ventas']:
            for k in ('asignados', 'efectivos', 'ventas', 'perdidas', 'abiertas'):
                setattr(u, k, r[k])
            usuarios.append(u)
    llam = {r['agente']: r for r in Llamada.objects.filter(inicio_at__range=(ini, fin)).values('agente')
            .annotate(n=Count('pk'), seg=Sum('duracion_seg'))}
    filas = []
    for u in usuarios:
        l = llam.get(u.pk, {})
        filas.append({'u': u, 'asignados': u.asignados, 'efectivos': u.efectivos, 'ventas': u.ventas,
                      'perdidas': u.perdidas, 'abiertas': u.abiertas,
                      'conversion': _pct(u.ventas, u.ventas + u.perdidas),
                      'llamadas': l.get('n', 0), 'minutos': round((l.get('seg') or 0) / 60)})
    return sorted(filas, key=lambda f: (-f['ventas'], -f['conversion'], f['u'].display_name))


class DashboardView(PermisoRequeridoMixin, View):
    permiso = ('reportes', 'supervision')

    def get(self, request):
        from apps.crm.models import Oportunidad, Tipificacion
        from apps.crm.views import embudo_actual
        embudo, embudos = embudo_actual(request)
        p, desde, hasta, ini, fin = _periodo(request)
        m = metricas(embudo, ini, fin)
        ops = Oportunidad.objects.filter(embudo=embudo) if embudo else Oportunidad.objects.all()

        # Embudo actual (oportunidades activas por etapa)
        conteo = dict(ops.filter(estado__in=Oportunidad.ESTADOS_ACTIVOS).values_list('etapa')
                      .annotate(n=Count('pk')).values_list('etapa', 'n'))
        etapas = [{'nombre': e.nombre, 'n': conteo.get(e.pk, 0), 'color': e.color}
                  for e in (embudo.etapas.filter(tipo='normal').order_by('orden') if embudo else [])]

        # Ingresos vs ventas por día
        dias = [(desde + timedelta(days=i)) for i in range((hasta - desde).days + 1)]
        ing = dict(ops.filter(created_at__range=(ini, fin)).annotate(d=TruncDate('created_at')).values_list('d')
                   .annotate(n=Count('pk')).values_list('d', 'n'))
        ven = dict(ops.filter(estado=Oportunidad.ESTADO_GANADA, cerrada_at__range=(ini, fin))
                   .annotate(d=TruncDate('cerrada_at')).values_list('d').annotate(n=Count('pk')).values_list('d', 'n'))
        serie = {'labels': [d.strftime('%d/%m') for d in dias], 'ingresos': [ing.get(d, 0) for d in dias],
                 'ventas': [ven.get(d, 0) for d in dias]}

        # Tipificaciones de cierre
        cierres = (ops.filter(cerrada_at__range=(ini, fin), tipificacion__isnull=False)
                   .values('tipificacion__resultado', 'tipificacion__categoria', 'tipificacion__nombre')
                   .annotate(n=Count('pk')).order_by('-n'))
        tip_venta = [c for c in cierres if c['tipificacion__resultado'] == Tipificacion.RESULTADO_VENTA]
        tip_no = [c for c in cierres if c['tipificacion__resultado'] == Tipificacion.RESULTADO_NO_VENTA]
        categorias = {}
        for c in tip_no:
            categorias[c['tipificacion__categoria'] or 'Sin categoría'] = categorias.get(
                c['tipificacion__categoria'] or 'Sin categoría', 0) + c['n']
        postergados = ops.filter(estado=Oportunidad.ESTADO_PAUSADA, tipificacion__isnull=True,
                                 motivo_pausa__icontains='recontact').count()

        origenes = list(ops.filter(created_at__range=(ini, fin)).values('origen').annotate(n=Count('pk')).order_by('-n'))
        nombres_origen = dict(Oportunidad.ORIGEN_CHOICES)
        for o in origenes:
            o['nombre'] = nombres_origen.get(o['origen'], o['origen'])

        from . import analisis
        return render(request, 'reportes/dashboard.html', {
            'proy': analisis.proyeccion(embudo),
            'sla': analisis.sla(embudo, ini, fin), 'call_center': analisis.call_center(ini, fin),
            'unicos': analisis.leads_unicos(embudo, ini, fin), 'conversion': analisis.embudo_conversion(embudo, ini, fin),
            'pipeline': analisis.matriz_pipeline(embudo), 'actividad': analisis.actividad_vendedoras(ini, fin),
            'columnas_actividad': analisis.COLUMNAS_ACTIVIDAD,
            'resultados': analisis.resultados_gestion(ini, fin, embudo),
            'embudo': embudo, 'embudos': embudos, 'p': p, 'desde': desde, 'hasta': hasta, 'm': m,
            'etapas': etapas, 'serie': serie, 'tip_venta': tip_venta, 'tip_no': tip_no,
            'categorias': sorted(categorias.items(), key=lambda x: -x[1]), 'postergados': postergados,
            'origenes': origenes, 'ranking': ranking_agentes(embudo, ini, fin), 'filtros': request.GET,
        })


class ExportarRankingView(PermisoRequeridoMixin, View):
    permiso = 'reportes'

    def get(self, request):
        from apps.crm.views import embudo_actual
        embudo, _ = embudo_actual(request)
        _, desde, hasta, ini, fin = _periodo(request)
        resp = HttpResponse(content_type='text/csv; charset=utf-8')
        resp['Content-Disposition'] = f'attachment; filename="ranking_{desde:%Y%m%d}_{hasta:%Y%m%d}.csv"'
        resp.write('﻿')
        w = csv.writer(resp, delimiter=';')
        w.writerow(['Agente', 'Asignados', 'Contactos efectivos', 'Ventas', 'No ventas', 'Conversión %', 'Abiertas',
                    'Llamadas', 'Minutos'])
        for f in ranking_agentes(embudo, ini, fin):
            w.writerow([f['u'].display_name, f['asignados'], f['efectivos'], f['ventas'], f['perdidas'],
                        f['conversion'], f['abiertas'], f['llamadas'], f['minutos']])
        return resp


class ExportarActividadView(PermisoRequeridoMixin, View):
    permiso = 'reportes'

    def get(self, request):
        from . import analisis
        _, desde, hasta, ini, fin = _periodo(request)
        resp = HttpResponse(content_type='text/csv; charset=utf-8')
        resp['Content-Disposition'] = f'attachment; filename="actividad_{desde:%Y%m%d}_{hasta:%Y%m%d}.csv"'
        resp.write('\ufeff')
        w = csv.writer(resp, delimiter=';')
        w.writerow(['Vendedora'] + [t for _, t in analisis.COLUMNAS_ACTIVIDAD])
        for f in analisis.actividad_vendedoras(ini, fin):
            w.writerow([f['u'].display_name] + [f[k] for k, _ in analisis.COLUMNAS_ACTIVIDAD])
        return resp


class CalidadEnviosView(PermisoRequeridoMixin, View):
    permiso = ('reportes', 'automatizaciones')

    def get(self, request):
        from . import analisis
        p, desde, hasta, ini, fin = _periodo(request)
        horas = int(request.GET.get('horas') or 48) if str(request.GET.get('horas') or '48').isdigit() else 48
        return render(request, 'reportes/calidad_envios.html', {
            'p': p, 'desde': desde, 'hasta': hasta, 'c': analisis.calidad_envios(ini, fin, horas_respuesta=horas),
        })


class MisNumerosView(LoginRequiredMixin, View):
    """Panel personal de la vendedora (o de una vendedora elegida, para quien tiene reportes)."""

    def get(self, request):
        from apps.crm.models import Oportunidad
        from apps.crm.views import agentes_activos, embudo_actual
        from apps.users.models import User
        from . import analisis
        embudo, embudos = embudo_actual(request)
        p, desde, hasta, ini, fin = _periodo(request)
        agente = request.user
        if request.GET.get('agente') and request.user.tiene_permiso('reportes'):
            agente = User.objects.filter(pk=request.GET['agente']).first() or request.user
        actividad = analisis.actividad_vendedoras(ini, fin, agentes=[agente])
        return render(request, 'reportes/mis_numeros.html', {
            'agente': agente, 'embudo': embudo, 'embudos': embudos, 'p': p, 'desde': desde, 'hasta': hasta,
            'sla': analisis.sla(embudo, ini, fin, agente),
            'cc': next(iter(analisis.call_center(ini, fin, agentes=[agente])), None),
            'm': metricas(embudo, ini, fin, agente=agente), 'unicos': analisis.leads_unicos(embudo, ini, fin, agente),
            'conversion': analisis.embudo_conversion(embudo, ini, fin, agente),
            'pipeline': analisis.matriz_pipeline(embudo, agente),
            'act': actividad[0] if actividad else None, 'columnas_actividad': analisis.COLUMNAS_ACTIVIDAD,
            'agentes': agentes_activos() if request.user.tiene_permiso('reportes') else [],
            'sin_contactar': Oportunidad.objects.filter(agente=agente, estado=Oportunidad.ESTADO_ABIERTA,
                                                        intentos_contacto=0).count(),
        })


class PulsoView(LoginRequiredMixin, View):
    """Un solo polling liviano para toda la barra superior: contadores + llamada activa."""

    def get(self, request):
        from django.http import JsonResponse
        from apps.crm.services import contar_tareas_hoy
        from apps.telefonia.services import estado_llamada_json, llamada_activa, usuario_tiene_interno
        from apps.users.services import contar_no_leidas
        from apps.whatsapp.services import contar_no_leidos_usuario
        from core import presencia
        user = request.user
        presencia.marcar(user, request.GET.get('tab'))
        if request.GET.get('sin_uso') is not None:
            presencia.registrar_uso(user, request.GET.get('sin_uso'))
        data = {'notif': contar_no_leidas(user), 'tareas': contar_tareas_hoy(user),
                'wa': contar_no_leidos_usuario(user), 'llamada': None}
        if usuario_tiene_interno(user):
            from apps.telefonia.services import pendiente_de_calificar, pendiente_json
            activa = llamada_activa(user)
            data['llamada'] = estado_llamada_json(activa)
            data['calificar'] = None if activa else pendiente_json(pendiente_de_calificar(user))
        return JsonResponse(data)


class SalirPestanaView(LoginRequiredMixin, View):
    """La pestaña se cierra o cambia de página (navigator.sendBeacon al ocultarse)."""

    def post(self, request):
        from django.http import HttpResponse
        from core import presencia
        if request.POST.get('tab'):
            presencia.salir_pestana(request.user, request.POST['tab'])
        return HttpResponse(status=204)
