"""
Cobro de la plataforma: pagos, vencimientos, avisos y suspensión por falta de pago.

Cada cliente tiene `pagado_hasta`. Cada pago (manual desde el panel o de Mercado Pago por webhook) lo corre
N meses. Todos los días `revisar_cobros` (Celery beat, schema público):
  · avisa por email a los que vencen en SAAS_DIAS_AVISO días (una vez por vencimiento),
  · suspende a los vencidos hace más de SAAS_DIAS_GRACIA días (si el cliente tiene suspensión automática).
Al registrar el pago se reactiva solo el cliente que estaba suspendido por falta de pago.
"""
import calendar
import functools
import logging
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.template.loader import render_to_string
from django.utils import timezone
from django_tenants.utils import get_public_schema_name, schema_context

from .models import Cliente, Pago

logger = logging.getLogger('apps.clientes')


def en_plataforma(func):
    """
    Los clientes y pagos viven en el schema público y django-tenants solo deja grabar un Cliente desde ahí:
    estas funciones se pueden llamar desde cualquier contexto (panel, webhook, beat, shell de un cliente).
    """
    @functools.wraps(func)
    def envoltura(*args, **kwargs):
        with schema_context(get_public_schema_name()):
            return func(*args, **kwargs)
    return envoltura


def sumar_meses(fecha, meses):
    """31/01 + 1 mes → 28/02 (o 29): se ajusta al último día del mes si hace falta."""
    mes = fecha.month - 1 + meses
    anio, mes = fecha.year + mes // 12, mes % 12 + 1
    return fecha.replace(year=anio, month=mes, day=min(fecha.day, calendar.monthrange(anio, mes)[1]))


def desde_sugerido(cliente, hoy=None):
    """
    Desde cuándo cubre el próximo pago:
      · al día o vencido dentro de la gracia → desde el vencimiento (se mantiene el ciclo de facturación),
      · vencido hace más (volvió después de una suspensión larga) → desde hoy,
      · nunca pagó → desde el fin de la prueba si todavía está corriendo, si no desde hoy.
    """
    hoy = hoy or timezone.localdate()
    if cliente.pagado_hasta:
        if cliente.pagado_hasta >= hoy - timedelta(days=settings.SAAS_DIAS_GRACIA):
            return cliente.pagado_hasta
        return hoy
    if cliente.estado == Cliente.ESTADO_PRUEBA and cliente.prueba_hasta and cliente.prueba_hasta > hoy:
        return cliente.prueba_hasta
    return hoy


@en_plataforma
def registrar_pago(cliente, *, monto, medio=Pago.MEDIO_TRANSFERENCIA, meses=1, desde=None, fecha=None,
                   referencia='', mp_pago_id='', notas='', registrado_por='', reactivar=True):
    """
    Registra el pago, extiende `pagado_hasta` y, si corresponde, deja al cliente activo.
    `reactivar`: si está suspendido lo reactiva (los pagos de Mercado Pago solo reactivan si la suspensión fue
    por falta de pago, para no pisar una suspensión manual).
    Con `mp_pago_id` es idempotente: si ese pago ya estaba registrado devuelve el existente.
    """
    if mp_pago_id:
        existente = Pago.objects.filter(mp_pago_id=mp_pago_id).first()
        if existente:
            return existente
    try:
        with transaction.atomic():
            cliente = Cliente.objects.select_for_update().get(pk=cliente.pk)
            desde = desde or desde_sugerido(cliente)
            hasta = sumar_meses(desde, int(meses))
            pago = Pago.objects.create(
                cliente=cliente, monto=Decimal(monto), medio=medio, fecha=fecha or timezone.localdate(),
                periodo_desde=desde, periodo_hasta=hasta, referencia=referencia[:120], mp_pago_id=mp_pago_id,
                notas=notas[:255], registrado_por=registrado_por[:150],
            )
            campos = ['aviso_vencimiento_enviado']
            if not cliente.pagado_hasta or hasta > cliente.pagado_hasta:
                cliente.pagado_hasta = hasta
                campos.append('pagado_hasta')
            cliente.aviso_vencimiento_enviado = None
            if cliente.estado == Cliente.ESTADO_PRUEBA:
                cliente.estado = Cliente.ESTADO_ACTIVO
                campos.append('estado')
            elif cliente.estado == Cliente.ESTADO_SUSPENDIDO and reactivar:
                cliente.estado, cliente.motivo_suspension = Cliente.ESTADO_ACTIVO, ''
                campos += ['estado', 'motivo_suspension']
            cliente.save(update_fields=campos)
    except IntegrityError:
        if mp_pago_id:  # el mismo webhook llegó dos veces a la vez
            return Pago.objects.get(mp_pago_id=mp_pago_id)
        raise
    logger.info('Pago registrado: %s %s (%s) → pagado hasta %s', cliente, monto, medio, cliente.pagado_hasta)
    return pago


@en_plataforma
def anular_pago(pago):
    """Anula un pago cargado por error y recalcula hasta cuándo tiene pago el cliente."""
    with transaction.atomic():
        pago.anulado = True
        pago.save(update_fields=['anulado'])
        cliente = Cliente.objects.select_for_update().get(pk=pago.cliente_id)
        ultimo = cliente.pagos.filter(anulado=False).order_by('-periodo_hasta').first()
        cliente.pagado_hasta = ultimo.periodo_hasta if ultimo else None
        cliente.save(update_fields=['pagado_hasta'])
    return cliente


@en_plataforma
def suspender(cliente, motivo=Cliente.SUSPENSION_MANUAL):
    cliente.estado, cliente.motivo_suspension = Cliente.ESTADO_SUSPENDIDO, motivo
    cliente.save(update_fields=['estado', 'motivo_suspension'])
    logger.info('Cliente %s suspendido (%s)', cliente, motivo)


@en_plataforma
def reactivar(cliente):
    cliente.estado, cliente.motivo_suspension = Cliente.ESTADO_ACTIVO, ''
    cliente.save(update_fields=['estado', 'motivo_suspension'])
    logger.info('Cliente %s reactivado', cliente)


def debita_solo(cliente):
    """Tiene una suscripción de Mercado Pago activa: el cobro es automático y no hace falta avisarle."""
    return cliente.cobro == Cliente.COBRO_MERCADOPAGO and cliente.mp_suscripcion_estado == 'authorized'


# ── Revisión diaria ────────────────────────────────────────────────────────

@en_plataforma
def revisar_cobros(hoy=None):
    """Avisa a los que están por vencer y suspende a los vencidos. Devuelve {'avisados': [...], 'suspendidos': [...]}."""
    hoy = hoy or timezone.localdate()
    resultado = {'avisados': [], 'suspendidos': []}
    clientes = (Cliente.objects.exclude(schema_name=get_public_schema_name())
                .filter(estado__in=Cliente.ESTADOS_CON_ACCESO).select_related('plan'))
    for cliente in clientes:
        venc = cliente.vencimiento
        if venc is None:
            continue
        dias = (venc - hoy).days
        if dias < -settings.SAAS_DIAS_GRACIA:
            if settings.SAAS_SUSPENDER_VENCIDOS and cliente.suspension_automatica:
                suspender(cliente, Cliente.SUSPENSION_FALTA_PAGO)
                _avisar(cliente, 'suspension', venc)
                resultado['suspendidos'].append(cliente)
        elif dias <= settings.SAAS_DIAS_AVISO and cliente.aviso_vencimiento_enviado != venc \
                and not debita_solo(cliente):
            if _avisar(cliente, 'vencimiento', venc):
                cliente.aviso_vencimiento_enviado = venc
                cliente.save(update_fields=['aviso_vencimiento_enviado'])
                resultado['avisados'].append(cliente)
    return resultado


def _avisar(cliente, tipo, vencimiento):
    """Email al contacto del cliente (y copia a la plataforma). tipo: 'vencimiento' | 'suspension'."""
    if not settings.SAAS_EMAIL_AVISOS or not cliente.contacto_email:
        return False
    ctx = {'cliente': cliente, 'vencimiento': vencimiento, 'CRM_NOMBRE': settings.CRM_NOMBRE, 'datos_pago': settings.SAAS_DATOS_PAGO,
           'moneda': settings.SAAS_MONEDA, 'dias_gracia': settings.SAAS_DIAS_GRACIA}
    asunto = {'vencimiento': f'{settings.CRM_NOMBRE}: tu suscripción vence el {vencimiento:%d/%m/%Y}',
              'suspension': f'{settings.CRM_NOMBRE}: suspendimos el acceso de {cliente.nombre}'}[tipo]
    destinatarios = [cliente.contacto_email]
    if settings.SAAS_EMAIL_AVISOS_COPIA:
        destinatarios.append(settings.SAAS_EMAIL_AVISOS_COPIA)
    try:
        send_mail(asunto, render_to_string(f'clientes/email_{tipo}.txt', ctx), None, destinatarios)
        return True
    except Exception:
        logger.exception('No se pudo mandar el aviso de %s a %s', tipo, cliente)
        return False
