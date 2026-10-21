"""Lógica del módulo de postventa: estados por defecto, conversión al ganar la venta y kanban."""
import calendar
import logging
from datetime import date

from django.db import transaction
from django.utils import timezone

from .models import AvisoRecurrente, Cuenta, EstadoProyecto, Proyecto, ESTADOS_PROYECTO_DEFAULT

logger = logging.getLogger('apps.cuentas')


def estados_proyecto():
    """Columnas del kanban. Si todavía no hay ninguna, crea las de ejemplo (editables después)."""
    if not EstadoProyecto.objects.exists():
        EstadoProyecto.objects.bulk_create([
            EstadoProyecto(nombre=nombre, color=color, orden=i, final=final)
            for i, (nombre, color, final) in enumerate(ESTADOS_PROYECTO_DEFAULT)
        ])
    return list(EstadoProyecto.objects.all())


def crear_cuenta_desde_oportunidad(op):
    """
    Al ganar una venta: asegura una Cuenta para ese contacto.
    Si el contacto ya está en una cuenta, no crea otra. Devuelve (cuenta, creada).
    """
    contacto = op.contacto
    if contacto is None:
        return None, False
    cuenta = Cuenta.objects.filter(contactos=contacto).first()
    if cuenta is not None:
        return cuenta, False
    cuenta = Cuenta.objects.create(
        nombre=contacto.nombre or contacto.telefono or 'Cliente',
        responsable=op.agente,
        email=contacto.email or '',
        telefono=contacto.telefono or '',
        origen_oportunidad=op,
    )
    cuenta.contactos.add(contacto)
    return cuenta, True


@transaction.atomic
def mover_proyecto(proyecto, estado, orden=None):
    """Mueve un proyecto a otra columna del kanban (y opcionalmente lo reordena)."""
    proyecto.estado = estado
    campos = ['estado', 'updated_at']
    if orden is not None:
        proyecto.orden = orden
        campos.append('orden')
    proyecto.save(update_fields=campos)
    return proyecto


# ── Avisos recurrentes de WhatsApp (mensualidades, suscripciones…) ─────────────

def proxima_fecha(desde: date, cada_meses: int, dia_mes: int) -> date:
    """Fecha `cada_meses` después de `desde`, fijada al día `dia_mes` (acotado al último día del mes)."""
    cada_meses = max(1, cada_meses)
    mes = desde.month - 1 + cada_meses
    anio = desde.year + mes // 12
    mes = mes % 12 + 1
    ultimo = calendar.monthrange(anio, mes)[1]
    return date(anio, mes, min(max(1, dia_mes), ultimo))


def primer_envio(cada_meses: int, dia_mes: int, hoy: date = None) -> date:
    """Primera fecha de envío: el `dia_mes` de este mes si todavía no pasó; si no, el del próximo ciclo."""
    hoy = hoy or timezone.localdate()
    ultimo = calendar.monthrange(hoy.year, hoy.month)[1]
    candidata = date(hoy.year, hoy.month, min(max(1, dia_mes), ultimo))
    if candidata < hoy:
        return proxima_fecha(hoy, cada_meses, dia_mes)
    return candidata


def enviar_aviso(aviso):
    """Manda un aviso ahora (reusando el envío de WhatsApp del CRM). Devuelve el texto de detalle."""
    from apps.whatsapp import services as wa
    from apps.whatsapp.services import ErrorEnvio

    contacto = aviso.contacto or aviso.cuenta.contactos.first()
    if contacto is None:
        return 'Omitido: la cuenta no tiene contactos.'
    if not contacto.puede_recibir_mensajes:
        return 'Omitido: el contacto no acepta mensajes o no tiene teléfono válido.'
    if wa.wa_dia_restante() == 0:
        return 'Omitido: se alcanzó el límite diario de WhatsApp del plan.'
    linea = wa.linea_para(contacto, preferida=aviso.linea)
    if linea is None:
        return 'Omitido: no hay ninguna línea de WhatsApp activa.'
    conv = wa.conversacion_para_contacto(contacto, linea)
    demora = wa.reservar_turno(linea)
    try:
        if aviso.plantilla_id:
            wa.enviar_mensaje(conv, plantilla=aviso.plantilla, automatico=True, demora_segundos=demora)
        else:
            texto = wa.reemplazar_variables_texto(aviso.mensaje, contacto)
            texto = texto.replace('{empresa}', aviso.cuenta.nombre)
            wa.enviar_mensaje(conv, texto=texto, automatico=True, demora_segundos=demora)
    except ErrorEnvio as e:
        return f'Omitido: {e}'
    return f'Enviado por {linea}'


def disparar_aviso(aviso, hoy=None):
    """Envía el aviso y agenda el próximo, pase lo que pase con el envío (se registra el detalle)."""
    hoy = hoy or timezone.localdate()
    detalle = 'Error inesperado'
    try:
        detalle = enviar_aviso(aviso)
    except Exception:  # pragma: no cover - nunca frenar el barrido por un aviso
        logger.warning('Falló el aviso recurrente %s', aviso.pk, exc_info=True)
    aviso.ultimo_envio_at = timezone.now()
    aviso.ultimo_detalle = detalle[:200]
    aviso.proximo_envio = proxima_fecha(hoy, aviso.cada_meses, aviso.dia_mes)
    aviso.save(update_fields=['ultimo_envio_at', 'ultimo_detalle', 'proximo_envio'])
    return detalle
