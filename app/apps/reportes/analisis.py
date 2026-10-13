"""
Reportes comerciales: leads únicos, embudo de conversión etapa a etapa, pipeline por vendedora y actividad por
vendedora. Todo se calcula con datos que el CRM ya registra (oportunidades, historial, mensajes, llamadas).
"""
from django.db.models import Count, F, Max, Q, Sum


def _pct(a, b):
    return round(a * 100 / b, 1) if b else 0


def leads_unicos(embudo, ini, fin, agente=None):
    """Personas distintas que entraron en el período: nuevas (oportunidad creada) + las que reingresaron."""
    from apps.crm.models import Actividad, Oportunidad
    ops = Oportunidad.objects.filter(created_at__range=(ini, fin))
    reing = Actividad.objects.filter(tipo=Actividad.TIPO_REINGRESO, created_at__range=(ini, fin))
    if embudo is not None:
        ops = ops.filter(embudo=embudo)
        reing = reing.filter(oportunidad__embudo=embudo)
    if agente is not None:
        ops = ops.filter(agente=agente)
        reing = reing.filter(oportunidad__agente=agente)
    nuevos = set(ops.values_list('contacto_id', flat=True))
    reingresos = set(reing.values_list('contacto_id', flat=True))
    return {'unicos': len(nuevos | reingresos), 'nuevos': len(nuevos), 'reingresos': len(reingresos - nuevos),
            'eventos_reingreso': reing.count()}


def embudo_conversion(embudo, ini, fin, agente=None):
    """
    De los leads que ingresaron en el período (cohorte), cuántos llegaron a cada etapa y qué porcentaje pasó de una
    etapa a la siguiente. "Llegó" = estuvo alguna vez en esa etapa o en una posterior; una venta cuenta como haber
    pasado por todas.
    """
    from apps.crm.models import Etapa, Oportunidad
    if embudo is None:
        return []
    etapas = list(embudo.etapas.filter(tipo=Etapa.TIPO_NORMAL).order_by('orden', 'pk'))
    if not etapas:
        return []
    ops = Oportunidad.objects.filter(embudo=embudo, created_at__range=(ini, fin))
    if agente is not None:
        ops = ops.filter(agente=agente)
    filas = ops.annotate(max_hist=Max('historial__etapa_nueva__orden',
                                      filter=Q(historial__etapa_nueva__embudo=embudo,
                                               historial__etapa_nueva__tipo=Etapa.TIPO_NORMAL)))
    tope = max(e.orden for e in etapas) + 1
    alcanzado = []
    ventas = 0
    for estado, etapa_orden, etapa_tipo, max_hist in filas.values_list('estado', 'etapa__orden', 'etapa__tipo',
                                                                      'max_hist'):
        if estado == Oportunidad.ESTADO_GANADA:
            ventas += 1
            alcanzado.append(tope)
            continue
        actual = etapa_orden if etapa_tipo == Etapa.TIPO_NORMAL else 0
        alcanzado.append(max(actual or 0, max_hist or 0))
    total = len(alcanzado)
    resultado, anterior = [], total
    for e in etapas:
        n = sum(1 for a in alcanzado if a >= e.orden) if e != etapas[0] else total
        resultado.append({'nombre': e.nombre, 'color': e.color, 'n': n, 'pct_total': _pct(n, total),
                          'pct_anterior': _pct(n, anterior) if e != etapas[0] else 100.0})
        anterior = n
    resultado.append({'nombre': 'Venta', 'color': '#22C97A', 'n': ventas, 'pct_total': _pct(ventas, total),
                      'pct_anterior': _pct(ventas, anterior), 'venta': True})
    return resultado


def matriz_pipeline(embudo, agente=None):
    """Oportunidades en curso por vendedora × etapa (foto de ahora)."""
    from apps.crm.models import Etapa, Oportunidad
    from apps.users.models import User
    if embudo is None:
        return None
    etapas = list(embudo.etapas.filter(tipo=Etapa.TIPO_NORMAL).order_by('orden', 'pk'))
    ops = Oportunidad.objects.filter(embudo=embudo, estado__in=Oportunidad.ESTADOS_ACTIVOS)
    if agente is not None:
        ops = ops.filter(agente=agente)
    conteo = {}
    for ag, et, n in ops.values_list('agente', 'etapa').annotate(n=Count('pk')).values_list('agente', 'etapa', 'n'):
        conteo[(ag, et)] = n
    agentes = {u.pk: u for u in User.objects.filter(pk__in={a for a, _ in conteo if a})}
    filas = []
    for ag_id in sorted({a for a, _ in conteo}, key=lambda a: (a is None, agentes[a].display_name if a else '')):
        celdas = [conteo.get((ag_id, e.pk), 0) for e in etapas]
        filas.append({'agente': agentes.get(ag_id), 'celdas': celdas, 'total': sum(celdas)})
    totales = [sum(f['celdas'][i] for f in filas) for i in range(len(etapas))]
    return {'etapas': etapas, 'filas': filas, 'totales': totales, 'total': sum(totales),
            'max': max([c for f in filas for c in f['celdas']] or [0])}


def actividad_vendedoras(ini, fin, agentes=None):
    """
    Lo que hizo cada vendedora en el período (todos los embudos): mensajes manuales y plantillas, mensajes y emails
    automáticos a sus leads, llamadas y minutos, notas, intentos, cambios de etapa y tareas completadas.
    """
    from apps.crm.models import Actividad, Tarea
    from apps.telefonia.models import Llamada
    from apps.telefonia.services import q_sin_calificar
    from apps.users.models import User
    from apps.whatsapp.models import Mensaje

    def por(qs, campo, **ann):
        return {r[campo]: r for r in qs.values(campo).annotate(**ann)}

    salientes = Mensaje.objects.filter(direccion=Mensaje.DIR_SALIENTE, timestamp__range=(ini, fin))
    manuales = por(salientes.filter(automatico=False, enviado_por__isnull=False), 'enviado_por',
                   mensajes=Count('pk'), plantillas=Count('pk', filter=Q(plantilla__isnull=False)))
    automaticos = por(salientes.filter(automatico=True, conversacion__agente__isnull=False), 'conversacion__agente',
                      n=Count('pk'))
    acts = Actividad.objects.filter(created_at__range=(ini, fin))
    propias = por(acts.filter(usuario__isnull=False), 'usuario',
                  notas=Count('pk', filter=Q(tipo=Actividad.TIPO_NOTA)),
                  intentos=Count('pk', filter=Q(tipo=Actividad.TIPO_INTENTO)),
                  etapas=Count('pk', filter=Q(tipo__in=[Actividad.TIPO_ETAPA, Actividad.TIPO_CIERRE])),
                  emails=Count('pk', filter=Q(tipo=Actividad.TIPO_EMAIL)),
                  sms=Count('pk', filter=Q(tipo=Actividad.TIPO_SMS)))
    emails_auto = por(acts.filter(tipo=Actividad.TIPO_EMAIL, usuario__isnull=True, oportunidad__agente__isnull=False),
                      'oportunidad__agente', n=Count('pk'))
    llamadas = por(Llamada.objects.filter(inicio_at__range=(ini, fin), agente__isnull=False), 'agente',
                   n=Count('pk'), salientes=Count('pk', filter=Q(direccion=Llamada.DIR_SALIENTE)),
                   atendidas=Count('pk', filter=Q(estado=Llamada.ESTADO_ATENDIDA)), seg=Sum('duracion_seg'),
                   efectivas=Count('pk', filter=Q(resultado__contactado=True)),
                   sin_calificar=Count('pk', filter=q_sin_calificar()))
    tareas = por(Tarea.objects.filter(estado=Tarea.ESTADO_COMPLETADA, completada_at__range=(ini, fin),
                                      asignado_a__isnull=False), 'asignado_a', n=Count('pk'))
    ids = set(manuales) | set(automaticos) | set(propias) | set(emails_auto) | set(llamadas) | set(tareas)
    usuarios = User.objects.filter(pk__in=ids)
    if agentes is not None:
        usuarios = usuarios.filter(pk__in=[a.pk for a in agentes])
    filas = []
    for u in usuarios:
        m, p, ll = manuales.get(u.pk, {}), propias.get(u.pk, {}), llamadas.get(u.pk, {})
        seg = ll.get('seg') or 0
        filas.append({
            'u': u, 'mensajes': m.get('mensajes', 0), 'plantillas': m.get('plantillas', 0),
            'automaticos': automaticos.get(u.pk, {}).get('n', 0), 'emails': p.get('emails', 0),
            'emails_auto': emails_auto.get(u.pk, {}).get('n', 0), 'sms': p.get('sms', 0),
            'llamadas': ll.get('n', 0), 'llamadas_salientes': ll.get('salientes', 0),
            'llamadas_atendidas': ll.get('atendidas', 0), 'minutos': round(seg / 60),
            'efectivas': ll.get('efectivas', 0), 'sin_calificar': ll.get('sin_calificar', 0),
            'promedio_seg': round(seg / ll['atendidas']) if ll.get('atendidas') else 0,
            'notas': p.get('notas', 0), 'intentos': p.get('intentos', 0), 'etapas': p.get('etapas', 0),
            'tareas': tareas.get(u.pk, {}).get('n', 0),
        })
    return sorted(filas, key=lambda f: f['u'].display_name.lower())


COLUMNAS_ACTIVIDAD = [
    ('mensajes', 'WhatsApp enviados'), ('plantillas', 'de ellos, plantillas'), ('automaticos', 'WhatsApp automáticos'),
    ('emails', 'Emails'), ('emails_auto', 'Emails automáticos'), ('sms', 'SMS'), ('llamadas', 'Llamadas'),
    ('llamadas_atendidas', 'Atendidas'), ('efectivas', 'Habló con el cliente'), ('sin_calificar', 'Sin calificar'),
    ('minutos', 'Minutos'), ('promedio_seg', 'Duración prom. (s)'),
    ('intentos', 'Intentos'), ('notas', 'Notas'), ('etapas', 'Cambios de etapa'), ('tareas', 'Tareas completadas'),
]


def sla(embudo, ini, fin, agente=None):
    """De los leads asignados en el período (en embudos con SLA): cuántos tuvieron la primera gestión a tiempo."""
    from datetime import timedelta
    from django.db.models import Avg, DurationField, ExpressionWrapper, F
    from django.utils import timezone
    from apps.crm.models import Embudo, Oportunidad
    from apps.crm.services import q_sla_vencido
    embudos = [embudo] if embudo is not None else list(Embudo.objects.filter(sla_minutos__gt=0))
    embudos = [e for e in embudos if e and e.sla_minutos]
    if not embudos:
        return None
    ahora = timezone.now()
    dentro = fuera = pendientes = 0
    for e in embudos:
        limite = F('asignada_at') + timedelta(minutes=e.sla_minutos)
        ops = Oportunidad.objects.filter(embudo=e, asignada_at__range=(ini, fin))
        if agente is not None:
            ops = ops.filter(agente=agente)
        dentro += ops.filter(primer_contacto_at__lte=limite).count()
        fuera += ops.filter(primer_contacto_at__gt=limite).count()
        fuera += ops.filter(primer_contacto_at__isnull=True, asignada_at__lt=ahora - timedelta(minutes=e.sla_minutos)).count()
        pendientes += ops.filter(primer_contacto_at__isnull=True,
                                 asignada_at__gte=ahora - timedelta(minutes=e.sla_minutos)).count()
    base = Oportunidad.objects.filter(embudo__in=embudos, asignada_at__range=(ini, fin), primer_contacto_at__isnull=False,
                                      primer_contacto_at__gte=F('asignada_at'))
    if agente is not None:
        base = base.filter(agente=agente)
    prom = base.aggregate(t=Avg(ExpressionWrapper(F('primer_contacto_at') - F('asignada_at'),
                                                  output_field=DurationField())))['t']
    vencidos = Oportunidad.objects.filter(q_sla_vencido(ahora), embudo__in=embudos)
    if agente is not None:
        vencidos = vencidos.filter(agente=agente)
    return {'minutos': embudos[0].sla_minutos if len(embudos) == 1 else None, 'dentro': dentro, 'fuera': fuera,
            'pendientes': pendientes, 'pct': _pct(dentro, dentro + fuera),
            'promedio_min': round(prom.total_seconds() / 60) if prom else None, 'vencidos_ahora': vencidos.count()}


def calidad_envios(ini, fin, horas_respuesta=48, dias_avance=7):
    """
    Respuesta a los envíos (WhatsApp automáticos y plantillas; emails automáticos): entregados, leídos, respondidos
    en las N horas siguientes y tarjetas que cambiaron de etapa en los N días siguientes.
    """
    from datetime import timedelta
    from django.db.models import Exists, OuterRef
    from apps.automatizaciones.models import EmailEnviado
    from apps.crm.models import HistorialEtapa
    from apps.whatsapp.models import Mensaje
    respuesta = Mensaje.objects.filter(conversacion=OuterRef('conversacion'), direccion=Mensaje.DIR_ENTRANTE,
                                       timestamp__gt=OuterRef('timestamp'),
                                       timestamp__lte=OuterRef('timestamp') + timedelta(hours=horas_respuesta))
    avance = HistorialEtapa.objects.filter(oportunidad__contacto=OuterRef('conversacion__contacto'),
                                           etapa_anterior__isnull=False, created_at__gt=OuterRef('timestamp'),
                                           created_at__lte=OuterRef('timestamp') + timedelta(days=dias_avance))
    base = (Mensaje.objects.filter(direccion=Mensaje.DIR_SALIENTE, timestamp__range=(ini, fin))
            .filter(Q(automatico=True) | Q(plantilla__isnull=False))
            .annotate(resp=Exists(respuesta), avanzo=Exists(avance)))
    medidas = dict(n=Count('pk'), entregados=Count('pk', filter=Q(status__in=[Mensaje.STATUS_ENTREGADO, Mensaje.STATUS_LEIDO])),
                   leidos=Count('pk', filter=Q(status=Mensaje.STATUS_LEIDO)),
                   fallidos=Count('pk', filter=Q(status=Mensaje.STATUS_FALLIDO)),
                   respondidos=Count('pk', filter=Q(resp=True)), avanzaron=Count('pk', filter=Q(avanzo=True)))

    def filas(qs, campos, nombre):
        out = []
        for r in qs.values(*campos).annotate(**medidas).order_by('-n'):
            r.update(nombre=nombre(r), pct_entregados=_pct(r['entregados'], r['n']), pct_leidos=_pct(r['leidos'], r['n']),
                     pct_respondidos=_pct(r['respondidos'], r['n']), pct_avanzaron=_pct(r['avanzaron'], r['n']))
            out.append(r)
        return out

    por_automatizacion = filas(base.filter(automatico=True), ['accion', 'accion__nombre', 'accion__embudo__nombre',
                                                              'accion__etapa__nombre'],
                               lambda r: r['accion__nombre'] or 'Automático (sin automatización registrada)')
    por_plantilla = filas(base.filter(plantilla__isnull=False), ['plantilla', 'plantilla__nombre'],
                          lambda r: r['plantilla__nombre'])
    emails = []
    for r in (EmailEnviado.objects.filter(enviado_at__range=(ini, fin))
              .values('accion', 'accion__nombre').annotate(n=Count('pk'), abiertos=Count('pk', filter=Q(abierto_at__isnull=False)),
                                                           clics=Count('pk', filter=Q(clic_at__isnull=False))).order_by('-n')):
        r.update(nombre=r['accion__nombre'] or 'Email automático', pct_abiertos=_pct(r['abiertos'], r['n']),
                 pct_clics=_pct(r['clics'], r['n']))
        emails.append(r)
    return {'automatizaciones': por_automatizacion, 'plantillas': por_plantilla, 'emails': emails,
            'horas_respuesta': horas_respuesta, 'dias_avance': dias_avance}


def call_center(ini, fin, agentes=None, acw_max_min=15):
    """
    Por vendedora: llamadas, tiempo de habla (total y promedio), after call work (tiempo entre que corta y su próxima
    acción en el CRM, hasta un tope) y tiempo conectado al CRM en el período.
    """
    from datetime import timedelta
    from django.db.models import OuterRef, Subquery
    from apps.crm.models import Actividad
    from apps.telefonia.models import Llamada
    from apps.users.models import SesionConexion, User
    tope = timedelta(minutes=acw_max_min)
    proxima = (Actividad.objects.filter(usuario=OuterRef('agente'), created_at__gt=OuterRef('fin_at'),
                                        created_at__lte=OuterRef('fin_at') + tope)
               .exclude(tipo=Actividad.TIPO_LLAMADA).order_by('created_at').values('created_at')[:1])
    proxima_llamada = (Llamada.objects.filter(agente=OuterRef('agente'), inicio_at__gt=OuterRef('fin_at'),
                                              inicio_at__lte=OuterRef('fin_at') + tope)
                       .order_by('inicio_at').values('inicio_at')[:1])
    llamadas = Llamada.objects.filter(inicio_at__range=(ini, fin), agente__isnull=False)
    if agentes is not None:
        llamadas = llamadas.filter(agente__in=agentes)
    datos = {}
    for ag, direccion, estado, seg, fin_at, prox, prox_ll in (
            llamadas.annotate(prox=Subquery(proxima), prox_ll=Subquery(proxima_llamada))
            .values_list('agente', 'direccion', 'estado', 'duracion_seg', 'fin_at', 'prox', 'prox_ll')):
        d = datos.setdefault(ag, {'llamadas': 0, 'salientes': 0, 'entrantes': 0, 'atendidas': 0, 'habla': 0,
                                  'acw': [], 'conectado': 0})
        d['llamadas'] += 1
        d['salientes' if direccion == Llamada.DIR_SALIENTE else 'entrantes'] += 1
        if estado == Llamada.ESTADO_ATENDIDA:
            d['atendidas'] += 1
            d['habla'] += seg or 0
            siguiente = min([t for t in (prox, prox_ll) if t] or [None]) if (prox or prox_ll) else None
            if fin_at and siguiente:
                d['acw'].append((siguiente - fin_at).total_seconds())
    sesiones = SesionConexion.objects.filter(inicio__lte=fin).filter(Q(fin__gte=ini) | Q(fin__isnull=True, ultimo__gte=ini))
    if agentes is not None:
        sesiones = sesiones.filter(usuario__in=agentes)
    for s in sesiones:
        desde, hasta = max(s.inicio, ini), min(s.termina, fin)
        if hasta > desde:
            datos.setdefault(s.usuario_id, {'llamadas': 0, 'salientes': 0, 'entrantes': 0, 'atendidas': 0, 'habla': 0,
                                            'acw': [], 'conectado': 0})['conectado'] += (hasta - desde).total_seconds()
    usuarios = {u.pk: u for u in User.objects.filter(pk__in=list(datos))}
    filas = []
    for pk, d in datos.items():
        acw = d.pop('acw')
        d.update(u=usuarios[pk], habla_min=round(d['habla'] / 60), promedio_habla=round(d['habla'] / d['atendidas']) if d['atendidas'] else 0,
                 acw_prom=round(sum(acw) / len(acw)) if acw else None, conectado_h=round(d['conectado'] / 3600, 1),
                 ocupacion=_pct(d['habla'], d['conectado']) if d['conectado'] else None)
        filas.append(d)
    return sorted(filas, key=lambda f: f['u'].display_name.lower())


def proyeccion(embudo, dias_historia=90):
    """
    Dos proyecciones simples: (1) ritmo del mes: ventas hasta hoy llevadas a fin de mes; (2) ventas esperadas del
    pipeline actual: en curso por etapa × % que terminó en venta (de los cerrados en los últimos N días que pasaron
    por esa etapa).
    """
    import calendar
    from datetime import timedelta
    from django.utils import timezone
    from apps.crm.models import Etapa, HistorialEtapa, Oportunidad
    if embudo is None:
        return None
    hoy = timezone.localdate()
    tz = timezone.get_current_timezone()
    from datetime import datetime
    ini_mes = timezone.make_aware(datetime.combine(hoy.replace(day=1), datetime.min.time()), tz)
    ventas_mes = Oportunidad.objects.filter(embudo=embudo, estado=Oportunidad.ESTADO_GANADA, cerrada_at__gte=ini_mes).count()
    dias_mes = calendar.monthrange(hoy.year, hoy.month)[1]
    ritmo = round(ventas_mes * dias_mes / hoy.day) if hoy.day else ventas_mes
    desde = timezone.now() - timedelta(days=dias_historia)
    cerradas = Oportunidad.objects.filter(embudo=embudo, cerrada_at__gte=desde,
                                          estado__in=[Oportunidad.ESTADO_GANADA, Oportunidad.ESTADO_PERDIDA])
    esperadas, detalle = 0.0, []
    for e in embudo.etapas.filter(tipo=Etapa.TIPO_NORMAL).order_by('orden'):
        pasaron = cerradas.filter(pk__in=HistorialEtapa.objects.filter(etapa_nueva=e).values('oportunidad_id'))
        total = pasaron.count()
        tasa = pasaron.filter(estado=Oportunidad.ESTADO_GANADA).count() / total if total else 0
        en_curso = Oportunidad.objects.filter(embudo=embudo, etapa=e, estado__in=Oportunidad.ESTADOS_ACTIVOS).count()
        esperadas += en_curso * tasa
        detalle.append({'etapa': e.nombre, 'en_curso': en_curso, 'tasa': round(tasa * 100, 1),
                        'esperadas': round(en_curso * tasa, 1)})
    return {'ventas_mes': ventas_mes, 'ritmo_fin_de_mes': ritmo, 'pipeline_esperadas': round(esperadas),
            'detalle': detalle, 'dias_historia': dias_historia}


def resultados_gestion(ini, fin, embudo=None):
    """Resultado de las llamadas atendidas por vendedora (filas) y resultado (columnas), con las sin calificar."""
    from apps.crm.models import ResultadoGestion
    from apps.telefonia.models import Llamada
    from apps.telefonia.services import q_sin_calificar
    qs = Llamada.objects.filter(inicio_at__range=(ini, fin), agente__isnull=False, estado=Llamada.ESTADO_ATENDIDA)
    if embudo is not None:
        qs = qs.filter(oportunidad__embudo=embudo)
    conteo = {(r['agente'], r['resultado']): r['n'] for r in qs.filter(resultado__isnull=False)
              .values('agente', 'resultado').annotate(n=Count('pk'))}
    sin = dict(qs.filter(q_sin_calificar()).values_list('agente').annotate(n=Count('pk')).values_list('agente', 'n'))
    if not conteo and not sin:
        return None
    resultados = list(ResultadoGestion.objects.filter(pk__in={r for _, r in conteo}).order_by('orden', 'nombre'))
    from apps.users.models import User
    filas = []
    for u in User.objects.filter(pk__in={a for a, _ in conteo} | set(sin)).order_by('first_name', 'username'):
        celdas = [conteo.get((u.pk, r.pk), 0) for r in resultados]
        filas.append({'u': u, 'celdas': celdas, 'sin': sin.get(u.pk, 0), 'total': sum(celdas) + sin.get(u.pk, 0)})
    totales = [sum(f['celdas'][i] for f in filas) for i in range(len(resultados))]
    return {'resultados': resultados, 'filas': filas, 'totales': totales, 'sin': sum(sin.values()),
            'total': sum(f['total'] for f in filas)}


def gestion_por(campo, ini, fin, embudo=None):
    """
    Cuánto se trabajó cada pauta (campo='pauta') o base importada (campo='lote') en el período: llamadas, atendidas,
    minutos, WhatsApp y emails enviados a esos leads. {id: {...}} (id None = sin pauta / sin base).
    """
    from apps.automatizaciones.models import EmailEnviado
    from apps.telefonia.models import Llamada
    from apps.whatsapp.models import Mensaje
    filtro_op = {'oportunidad__embudo': embudo} if embudo is not None else {}
    res = {}

    def sumar(filas, *claves):
        for r in filas:
            d = res.setdefault(r['k'], {'llamadas': 0, 'atendidas': 0, 'seg': 0, 'whatsapp': 0, 'emails': 0})
            for c in claves:
                d[c] += r[c] or 0

    sumar(Llamada.objects.filter(inicio_at__range=(ini, fin), oportunidad__isnull=False, **filtro_op)
          .values(k=F(f'oportunidad__{campo}')).annotate(llamadas=Count('pk'),
                                                          atendidas=Count('pk', filter=Q(estado=Llamada.ESTADO_ATENDIDA)),
                                                          seg=Sum('duracion_seg')),
          'llamadas', 'atendidas', 'seg')
    rel = 'conversacion__contacto__oportunidades'
    filtro_msg = {f'{rel}__embudo': embudo} if embudo is not None else {}
    sumar(Mensaje.objects.filter(direccion=Mensaje.DIR_SALIENTE, timestamp__range=(ini, fin),
                                 conversacion__contacto__isnull=False, **filtro_msg)
          .values(k=F(f'{rel}__{campo}')).annotate(whatsapp=Count('pk', distinct=True)), 'whatsapp')
    sumar(EmailEnviado.objects.filter(enviado_at__range=(ini, fin), oportunidad__isnull=False, **filtro_op)
          .values(k=F(f'oportunidad__{campo}')).annotate(emails=Count('pk')), 'emails')
    for d in res.values():
        d['minutos'] = round(d.pop('seg') / 60)
    return res
