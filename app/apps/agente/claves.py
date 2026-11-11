"""
Qué API key usa el agente: la propia del cliente (cifrada en ConfigAgente) o la de la plataforma (.env),
según lo que se eligió en el panel para su plan o para el cliente (Cliente.politica_ia_keys).
"""
from django.conf import settings

from core.tenancy import cliente_actual

NOMBRES = {'anthropic': 'Anthropic', 'openai': 'OpenAI'}


class SinClave(Exception):
    pass


def politica():
    cliente = cliente_actual()
    return cliente.politica_ia_keys if cliente is not None else 'ambas'


def _de_plataforma(proveedor):
    return settings.ANTHROPIC_API_KEY if proveedor == 'anthropic' else settings.OPENAI_API_KEY


def _del_cliente(config, proveedor):
    return (config.anthropic_api_key if proveedor == 'anthropic' else config.openai_api_key) or ''


def clave(proveedor, config=None):
    """(key, origen) con origen 'cliente' o 'plataforma'. Levanta SinClave con un mensaje para mostrar."""
    from .models import ConfigAgente
    config = config or ConfigAgente.get()
    pol = politica()
    propia = _del_cliente(config, proveedor) if pol in ('propia', 'ambas') else ''
    if propia:
        return propia, 'cliente'
    plataforma = _de_plataforma(proveedor) if pol in ('plataforma', 'ambas') else ''
    if plataforma:
        return plataforma, 'plataforma'
    nombre = NOMBRES[proveedor]
    if pol == 'propia':
        raise SinClave(f'Falta tu API key de {nombre}: cargala en Agente IA → Configuración.')
    raise SinClave(f'No hay API key de {nombre} configurada (ni de la empresa ni de la plataforma).')


def disponible(proveedor, config=None):
    try:
        clave(proveedor, config)
        return True
    except SinClave:
        return False


def probar(proveedor, key):
    """Valida una key sin gastar: lista los modelos. Devuelve (ok, mensaje)."""
    try:
        if proveedor == 'anthropic':
            import anthropic
            anthropic.Anthropic(api_key=key, timeout=15.0, max_retries=0).models.list(limit=1)
        else:
            import openai
            openai.OpenAI(api_key=key, timeout=15.0, max_retries=0).models.list()
    except Exception as e:
        nombre = type(e).__name__
        if 'Authentication' in nombre or 'PermissionDenied' in nombre:
            return False, 'La key no es válida o no tiene permisos.'
        return False, f'No se pudo verificar ({nombre}): {str(e)[:150]}'
    return True, f'La key de {NOMBRES[proveedor]} funciona.'
