"""
Cobro con Mercado Pago (opcional, por cliente). Dos formas:

· Suscripción (débito automático mensual, API "preapproval"): desde el panel se genera un link; el cliente lo
  abre, se loguea en Mercado Pago y autoriza la tarjeta. MP cobra solo todos los meses y avisa por webhook.
· Link de pago único (Checkout Pro): para un cliente que quiere pagar N meses con MP sin débito automático.

Cada pago aprobado llega al webhook (/mercadopago/webhook/ del dominio principal) y se registra con
`cobros.registrar_pago` (corre `pagado_hasta` y reactiva si estaba suspendido por falta de pago). El webhook
nunca confía en el cuerpo de la notificación: con el id consulta el recurso a la API de MP con nuestro token.

Config: MP_ACCESS_TOKEN (credenciales de producción de tu cuenta) y MP_WEBHOOK_SECRET (clave secreta de la
sección Webhooks de la aplicación en developers.mercadopago.com). Ver docs/SAAS.md.
"""
import hashlib
import hmac
import logging
from decimal import Decimal

import requests
from django.conf import settings
from django.urls import reverse

from . import cobros
from .cobros import en_plataforma
from .models import Cliente, Pago

logger = logging.getLogger('apps.clientes')

API = 'https://api.mercadopago.com'
PREFIJO_REFERENCIA = 'crm-cliente-'


class ErrorMP(Exception):
    pass


def configurado():
    return bool(settings.MP_ACCESS_TOKEN)


def _api(metodo, ruta, **kwargs):
    if not configurado():
        raise ErrorMP('Mercado Pago no está configurado (falta MP_ACCESS_TOKEN).')
    try:
        resp = requests.request(metodo, API + ruta, timeout=20,
                                headers={'Authorization': f'Bearer {settings.MP_ACCESS_TOKEN}'}, **kwargs)
    except requests.RequestException as e:
        raise ErrorMP(f'No se pudo conectar con Mercado Pago: {e}') from e
    if resp.status_code >= 300:
        try:
            detalle = resp.json().get('message') or resp.text
        except ValueError:
            detalle = resp.text
        raise ErrorMP(f'Mercado Pago respondió {resp.status_code}: {str(detalle)[:300]}')
    return resp.json()


def referencia(cliente):
    return f'{PREFIJO_REFERENCIA}{cliente.pk}'


def _cliente_de_referencia(ref):
    if not ref or not str(ref).startswith(PREFIJO_REFERENCIA):
        return None
    pk = str(ref)[len(PREFIJO_REFERENCIA):]
    return Cliente.objects.filter(pk=pk).first() if pk.isdigit() else None


def _url_plataforma(ruta):
    return settings.SITE_URL + ruta


# ── Suscripción (débito automático) ────────────────────────────────────────

@en_plataforma
def crear_suscripcion(cliente, email_pagador):
    """
    Crea la suscripción pendiente y guarda el link para mandarle al cliente. El email tiene que ser el de la
    cuenta de Mercado Pago con la que el cliente va a pagar (si no, MP rechaza la autorización).
    """
    if cliente.precio <= 0:
        raise ErrorMP('El cliente no tiene precio: asignale un plan con precio o un precio acordado.')
    if cliente.mp_suscripcion_id and cliente.mp_suscripcion_estado in ('pending', 'authorized'):
        cancelar_suscripcion(cliente)
    datos = _api('POST', '/preapproval', json={
        'reason': f'{settings.CRM_NOMBRE} · {cliente.nombre}',
        'external_reference': referencia(cliente),
        'payer_email': email_pagador,
        'auto_recurring': {'frequency': 1, 'frequency_type': 'months',
                           'transaction_amount': float(cliente.precio), 'currency_id': settings.SAAS_MONEDA},
        'back_url': _url_plataforma('/ingresar/'),
        'status': 'pending',
    })
    cliente.mp_suscripcion_id = datos['id']
    cliente.mp_suscripcion_estado = datos.get('status', 'pending')
    cliente.mp_suscripcion_link = datos.get('init_point', '')
    cliente.mp_email_pagador = email_pagador
    cliente.cobro = Cliente.COBRO_MERCADOPAGO
    cliente.save(update_fields=['mp_suscripcion_id', 'mp_suscripcion_estado', 'mp_suscripcion_link',
                                'mp_email_pagador', 'cobro'])
    return cliente.mp_suscripcion_link


@en_plataforma
def cancelar_suscripcion(cliente):
    if cliente.mp_suscripcion_id:
        _api('PUT', f'/preapproval/{cliente.mp_suscripcion_id}', json={'status': 'cancelled'})
    cliente.mp_suscripcion_estado = 'cancelled'
    cliente.save(update_fields=['mp_suscripcion_estado'])


@en_plataforma
def actualizar_suscripcion(cliente):
    """Trae el estado actual de la suscripción (pending / authorized / paused / cancelled)."""
    if not cliente.mp_suscripcion_id:
        return ''
    datos = _api('GET', f'/preapproval/{cliente.mp_suscripcion_id}')
    _guardar_estado_suscripcion(cliente, datos)
    return cliente.mp_suscripcion_estado


def _guardar_estado_suscripcion(cliente, datos):
    estado = datos.get('status', '')
    if estado and estado != cliente.mp_suscripcion_estado:
        cliente.mp_suscripcion_estado = estado
        cliente.save(update_fields=['mp_suscripcion_estado'])
        logger.info('Suscripción MP de %s → %s', cliente, estado)


# ── Link de pago único ─────────────────────────────────────────────────────

def crear_link_pago(cliente, meses=1):
    if cliente.precio <= 0:
        raise ErrorMP('El cliente no tiene precio: asignale un plan con precio o un precio acordado.')
    meses = int(meses)
    titulo = f'{settings.CRM_NOMBRE} · {cliente.nombre} · {meses} {"mes" if meses == 1 else "meses"}'
    datos = _api('POST', '/checkout/preferences', json={
        'items': [{'title': titulo, 'quantity': 1, 'currency_id': settings.SAAS_MONEDA,
                   'unit_price': float(cliente.precio * meses)}],
        'external_reference': referencia(cliente),
        'metadata': {'meses': meses},
        'notification_url': _url_plataforma(reverse('mercadopago_webhook')),
        'back_urls': {'success': _url_plataforma('/ingresar/')},
    })
    return datos['init_point']


# ── Webhook ────────────────────────────────────────────────────────────────

def firma_valida(request):
    """
    Valida el header x-signature (ts=...,v1=...) con MP_WEBHOOK_SECRET. Sin secreto configurado se acepta
    (igual no se confía en el cuerpo: todo se vuelve a consultar a la API).
    """
    if not settings.MP_WEBHOOK_SECRET:
        return True
    partes = dict(p.strip().split('=', 1) for p in request.headers.get('x-signature', '').split(',') if '=' in p)
    ts, v1 = partes.get('ts'), partes.get('v1')
    if not ts or not v1:
        return False
    data_id = request.GET.get('data.id', '')
    if data_id.isalnum():
        data_id = data_id.lower()
    manifiesto = f'id:{data_id};request-id:{request.headers.get("x-request-id", "")};ts:{ts};'
    esperado = hmac.new(settings.MP_WEBHOOK_SECRET.encode(), manifiesto.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(esperado, v1)


@en_plataforma
def procesar_notificacion(tipo, recurso_id):
    """Procesa una notificación de MP. Devuelve el Pago registrado (o None). Lanza ErrorMP si falla la API."""
    if not recurso_id:
        return None
    if tipo in ('subscription_preapproval', 'preapproval'):
        datos = _api('GET', f'/preapproval/{recurso_id}')
        cliente = (_cliente_de_referencia(datos.get('external_reference'))
                   or Cliente.objects.filter(mp_suscripcion_id=recurso_id).first())
        if cliente and cliente.mp_suscripcion_id == recurso_id:
            _guardar_estado_suscripcion(cliente, datos)
        return None
    if tipo == 'subscription_authorized_payment':
        datos = _api('GET', f'/authorized_payments/{recurso_id}')
        pago_mp = datos.get('payment') or {}
        if pago_mp.get('status') != 'approved' or not pago_mp.get('id'):
            return None
        cliente = Cliente.objects.filter(mp_suscripcion_id=datos.get('preapproval_id') or '-').first() \
            or _cliente_de_referencia(datos.get('external_reference'))
        if cliente is None:
            logger.warning('Pago de suscripción MP %s sin cliente (preapproval %s)', recurso_id,
                           datos.get('preapproval_id'))
            return None
        return _registrar(cliente, pago_mp['id'], datos.get('transaction_amount'), 1,
                          f'Suscripción {datos.get("preapproval_id", "")}')
    if tipo == 'payment':
        datos = _api('GET', f'/v1/payments/{recurso_id}')
        cliente = _cliente_de_referencia(datos.get('external_reference'))
        if cliente is None or datos.get('status') != 'approved':
            return None
        meses = (datos.get('metadata') or {}).get('meses') or 1
        return _registrar(cliente, datos['id'], datos.get('transaction_amount'), meses, 'Pago Mercado Pago')
    return None


def _registrar(cliente, pago_id, monto, meses, referencia_txt):
    return cobros.registrar_pago(
        cliente, monto=Decimal(str(monto or cliente.precio)), medio=Pago.MEDIO_MERCADOPAGO, meses=int(meses),
        mp_pago_id=str(pago_id), referencia=f'{referencia_txt} · pago {pago_id}', registrado_por='Mercado Pago',
        # No pisar una suspensión manual: solo reactiva si se suspendió por falta de pago.
        reactivar=cliente.motivo_suspension != Cliente.SUSPENSION_MANUAL,
    )
