"""
Firma con expiración para los adjuntos salientes de WhatsApp (A4). El proveedor (Meta/Twilio) descarga el
archivo desde una URL pública al momento de enviar, así que no se puede pedir sesión; en su lugar la URL lleva
una firma con vencimiento. Pasado el plazo, el link deja de servir (el agente igual lo ve con su sesión).
"""
from django.conf import settings
from django.core import signing

SALT = 'wa-media-saliente'


def firmar(ruta):
    """Devuelve el token firmado para una ruta relativa de media (sin /media/)."""
    return signing.dumps(ruta, salt=SALT)


def ruta_firmada_valida(sig, ruta):
    if not sig:
        return False
    try:
        return signing.loads(sig, salt=SALT, max_age=settings.MEDIA_FIRMA_SEGUNDOS) == ruta
    except signing.BadSignature:
        return False
