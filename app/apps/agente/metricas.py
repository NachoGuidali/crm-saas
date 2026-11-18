"""
Métricas del agente IA para la pantalla Resultados: mensajes que manda (entregas, lecturas, fallos, respuestas),
embudo de la conversación (dónde se cae la gente) y conversiones de los closers (avisadas por su CRM).
"""
from datetime import timedelta

from django.db.models import Count, Exists, OuterRef, Q

from .models import TurnoAgente

VENTANA_RESPUESTA = timedelta(hours=48)
EVENTO_VENTA, EVENTO_NO_VENTA = 'venta', 'no_venta'


def _pct(n, base):
    return round(n * 100 / base) if base else 0


def mensajes(desde):
    """Por tipo de turno: cuántos mensajes salieron y qué pasó con ellos. Respondidos = el lead escribió en 48 h."""
    from apps.whatsapp.models import Mensaje as M
    salida = {}
    grupos = {'proactivos': [TurnoAgente.MODO_INICIO, TurnoAgente.MODO_SEGUIMIENTO],
              'respuestas': [TurnoAgente.MODO_RESPUESTA]}
    for nombre, modos in grupos.items():
        turnos = TurnoAgente.objects.filter(simulado=False, created_at__gte=desde, modo__in=modos, error='')
        msgs = M.objects.filter(turnos_agente__in=turnos).distinct()
        agg = msgs.aggregate(total=Count('pk', distinct=True),
                             entregados=Count('pk', filter=Q(status__in=[M.STATUS_ENTREGADO, M.STATUS_LEIDO]), distinct=True),
                             leidos=Count('pk', filter=Q(status=M.STATUS_LEIDO), distinct=True),
                             fallidos=Count('pk', filter=Q(status=M.STATUS_FALLIDO), distinct=True))
        respuesta = M.objects.filter(conversacion=OuterRef('conversacion'), direccion=M.DIR_ENTRANTE,
                                     timestamp__gt=OuterRef('created_at'))
        n_turnos = turnos.count()
        respondidos = turnos.annotate(r=Exists(respuesta)).filter(r=True).count() if nombre == 'proactivos' else None
        agg.update(turnos=n_turnos, respondidos=respondidos,
                   pct={k: _pct(agg[k], agg['total']) for k in ('entregados', 'leidos', 'fallidos')},
                   pct_respondidos=_pct(respondidos or 0, n_turnos))
        salida[nombre] = agg
    return salida


def _contactos(desde):
    from apps.crm.models import Contacto
    ids = (TurnoAgente.objects.filter(simulado=False, created_at__gte=desde, conversacion__contacto__isnull=False)
           .values_list('conversacion__contacto', flat=True).distinct())
    return Contacto.objects.filter(pk__in=ids)


def embudo(desde, config):
    """[(etapa, cantidad, % del total)] sobre los leads que atendió el agente en el período."""
    from apps.crm.models import Actividad
    from apps.whatsapp.models import Mensaje as M
    from . import herramientas
    from .models import CAMPOS_BASE_AGENTE
    contactos = list(_contactos(desde).only('pk', 'datos_extra', *[k for k, _ in CAMPOS_BASE_AGENTE]))
    total = len(contactos)
    pasos = [('Atendidos por el agente', total)]
    ids = [c.pk for c in contactos]
    respondieron = set(M.objects.filter(conversacion__contacto__in=ids, direccion=M.DIR_ENTRANTE)
                       .values_list('conversacion__contacto', flat=True))
    pasos.append(('Respondieron', len(respondieron)))
    base = dict(CAMPOS_BASE_AGENTE)
    claves = list(config.campos_obligatorios or []) or \
        [k for k, *_ in herramientas.campos_disponibles(config) if k not in ('nombre', herramientas.CAMPO_OBSERVACION)]
    etiquetas = {k: e for k, e, *_ in herramientas.campos_disponibles(config)}
    for clave in claves:
        n = sum(1 for c in contactos
                if (getattr(c, clave, None) if clave in base else (c.datos_extra or {}).get(clave)) not in (None, '', []))
        pasos.append((f'Dio: {etiquetas.get(clave, clave)}', n))
    if config.leer_recibos:
        pasos.append(('Mandó el recibo', sum(1 for c in contactos if any(
            (c.datos_extra or {}).get(s) not in (None, '') for s, *_ in herramientas.CAMPOS_RECIBO))))
    aptos = Actividad.objects.filter(contacto__in=ids, texto__startswith='Agente IA derivó') \
        .values('contacto').distinct().count()
    pasos.append(('Aptos (derivados al closer)', aptos))
    ventas = Actividad.objects.filter(contacto__in=ids, texto__startswith=f'Evento externo "{EVENTO_VENTA}"') \
        .values('contacto').distinct().count()
    pasos.append(('Ventas cerradas por el closer', ventas))
    return [(etapa, n, _pct(n, total)) for etapa, n in pasos]


def conversiones(desde, costo_total):
    """De los leads derivados en el período: cuántos cerró el closer, cuántos no, y cuántos sin resultado aún."""
    from apps.crm.models import Actividad
    derivados = set(Actividad.objects.filter(created_at__gte=desde, texto__startswith='Agente IA derivó')
                    .values_list('oportunidad', flat=True))
    derivados.discard(None)
    eventos = Actividad.objects.filter(oportunidad__in=derivados)
    ventas = set(eventos.filter(texto__startswith=f'Evento externo "{EVENTO_VENTA}"').values_list('oportunidad', flat=True))
    no_ventas = set(eventos.filter(texto__startswith=f'Evento externo "{EVENTO_NO_VENTA}"')
                    .values_list('oportunidad', flat=True)) - ventas
    n = len(derivados)
    return {'derivados': n, 'ventas': len(ventas), 'no_ventas': len(no_ventas),
            'sin_resultado': n - len(ventas) - len(no_ventas), 'tasa': _pct(len(ventas), n),
            'costo_por_venta': (costo_total / len(ventas)) if ventas else None,
            'informado': bool(ventas or no_ventas)}
