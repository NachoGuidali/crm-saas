"""Lead scoring por reglas configurables (Configuración → Puntaje de leads)."""
from django.db.models import Q
from django.utils import timezone

from .models import Oportunidad, ReglaPuntaje


def _reglas(embudo_id):
    return [r for r in ReglaPuntaje.objects.filter(activa=True).filter(Q(embudo_id=embudo_id) | Q(embudo__isnull=True))]


def _contactos_que_respondieron(contacto_ids):
    from apps.crm.models import Actividad
    from apps.telefonia.models import Llamada
    from apps.whatsapp.models import Mensaje
    ids = set(Mensaje.objects.filter(direccion=Mensaje.DIR_ENTRANTE, conversacion__contacto_id__in=contacto_ids)
              .values_list('conversacion__contacto_id', flat=True))
    ids |= set(Llamada.objects.filter(contacto_id__in=contacto_ids, direccion=Llamada.DIR_ENTRANTE,
                                      estado=Llamada.ESTADO_ATENDIDA).values_list('contacto_id', flat=True))
    ids |= set(Actividad.objects.filter(contacto_id__in=contacto_ids, tipo=Actividad.TIPO_SMS,
                                        datos__direccion='in').values_list('contacto_id', flat=True))
    return ids


def _cumple(regla, op, respondieron, ahora):
    from apps.pautas.models import normalizar_clave
    from .services import valor_actual
    c = regla.condicion
    if c == regla.COND_RESPONDIO:
        return op.contacto_id in respondieron
    if c == regla.COND_PAUTA:
        return op.pauta_id == regla.pauta_id
    if c == regla.COND_CANAL:
        return op.origen == regla.valor
    if c == regla.COND_ORIGEN:
        buscado = normalizar_clave(regla.valor)
        return bool(buscado) and any(buscado in normalizar_clave(t) for t in (op.origen_pauta, op.fuente) if t)
    if c == regla.COND_EMAIL:
        return bool(op.contacto.email)
    if c == regla.COND_ETAPA:
        return op.etapa_id == regla.etapa_id
    if c == regla.COND_REINGRESOS:
        return op.ingresos >= regla.numero
    if c == regla.COND_INTENTOS:
        return op.contacto_efectivo_at is None and op.intentos_contacto >= regla.numero
    if c == regla.COND_INACTIVO:
        return bool(op.ultima_actividad_at) and (ahora - op.ultima_actividad_at).days >= regla.numero
    if c == regla.COND_CAMPO:
        clave = regla.campo if regla.campo.startswith('cp:') or regla.campo in ('valor',) else regla.campo
        actual = valor_actual(op, clave)
        if actual in (None, '', []):
            actual = valor_actual(op, f'cp:{regla.campo}')
        if not regla.valor:
            return actual not in (None, '', [])
        return normalizar_clave(actual) == normalizar_clave(regla.valor)
    return False


def calcular(op, reglas=None, respondieron=None, ahora=None):
    reglas = _reglas(op.embudo_id) if reglas is None else reglas
    if not reglas:
        return 0
    respondieron = _contactos_que_respondieron([op.contacto_id]) if respondieron is None else respondieron
    ahora = ahora or timezone.now()
    return sum(r.puntos for r in reglas if _cumple(r, op, respondieron, ahora))


def recalcular(op):
    if op is None:
        return 0
    op = Oportunidad.objects.select_related('contacto').get(pk=op.pk)
    p = calcular(op)
    if p != op.puntaje:
        Oportunidad.objects.filter(pk=op.pk).update(puntaje=p)
    return p


def recalcular_todos(lote=500):
    """Recalcula las oportunidades en curso (para las reglas que dependen del tiempo). Devuelve cuántas cambiaron."""
    ahora, cambiadas = timezone.now(), 0
    hay_reglas = ReglaPuntaje.objects.filter(activa=True).exists()
    qs = Oportunidad.objects.filter(estado__in=Oportunidad.ESTADOS_ACTIVOS).select_related('contacto').order_by('pk')
    if not hay_reglas:
        return qs.exclude(puntaje=0).update(puntaje=0)
    reglas_por_embudo = {}
    ultimo = 0
    while True:
        bloque = list(qs.filter(pk__gt=ultimo)[:lote])
        if not bloque:
            break
        ultimo = bloque[-1].pk
        respondieron = _contactos_que_respondieron({o.contacto_id for o in bloque})
        cambios = []
        for op in bloque:
            reglas = reglas_por_embudo.setdefault(op.embudo_id, _reglas(op.embudo_id))
            p = calcular(op, reglas, respondieron, ahora)
            if p != op.puntaje:
                op.puntaje = p
                cambios.append(op)
        Oportunidad.objects.bulk_update(cambios, ['puntaje'], batch_size=500)
        cambiadas += len(cambios)
    return cambiadas
