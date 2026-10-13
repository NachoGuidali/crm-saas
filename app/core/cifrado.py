"""Cifrado simétrico de secretos guardados en la base (credenciales de integraciones). La clave se deriva de
SECRET_KEY: si se cambia SECRET_KEY hay que volver a cargar esos secretos."""
import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import models

PREFIJO = 'fernet:'


def _fernet():
    clave = hashlib.sha256(f'crm-cifrado:{settings.SECRET_KEY}'.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(clave))


def cifrar(texto):
    if not texto:
        return ''
    return PREFIJO + _fernet().encrypt(texto.encode()).decode()


def descifrar(valor):
    if not valor:
        return ''
    if not valor.startswith(PREFIJO):
        return valor
    try:
        return _fernet().decrypt(valor[len(PREFIJO):].encode()).decode()
    except InvalidToken:
        return ''


class CampoCifrado(models.CharField):
    """
    CharField que cifra el valor en la base y lo descifra al leerlo: transparente para el resto del código
    (el modelo sigue viendo el texto plano). Pensado para tokens y contraseñas de integraciones.

    No se puede filtrar en la base por este campo (el cifrado no es determinístico), pero ninguno de estos
    secretos se consulta con `.filter()`. Tolera valores viejos en texto plano: `descifrar` los devuelve tal
    cual, así una migración de datos puede cifrarlos de a poco con solo volver a guardar la fila.
    """

    def __init__(self, *args, **kwargs):
        kwargs.setdefault('max_length', 2048)
        super().__init__(*args, **kwargs)

    def from_db_value(self, value, expression, connection):
        return descifrar(value) if value else value

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        return cifrar(value) if value else value
