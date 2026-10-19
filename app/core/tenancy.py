"""
Ayudas para el modo multi-cliente (SaaS): cada cliente tiene su schema y su subdominio.

En un request web el cliente lo resuelve django-tenants por el subdominio; en las tareas Celery lo fija
tenant-schemas-celery con el schema con el que se encoló la tarea. En los dos casos queda en `connection.tenant`.
"""
from urllib.parse import urlsplit

from django.conf import settings
from django.db import connection


def cliente_actual():
    """El cliente (tenant) en el que se está trabajando, o None si es el schema público."""
    tenant = getattr(connection, 'tenant', None)
    if tenant is None or getattr(tenant, 'schema_name', None) == _schema_publico():
        return None
    from apps.clientes.models import Cliente
    if not isinstance(tenant, Cliente):
        # schema_context() deja un "FakeTenant" que solo tiene schema_name: se busca el cliente real una vez.
        real = getattr(tenant, '_cliente_real', None)
        if real is None:
            real = Cliente.objects.filter(schema_name=tenant.schema_name).first()
            if real is None:
                return None
            tenant._cliente_real = real
        return real
    return tenant


def schema_actual():
    return getattr(connection, 'schema_name', None) or _schema_publico()


def _schema_publico():
    from django_tenants.utils import get_public_schema_name
    return get_public_schema_name()


def url_sitio():
    """
    URL pública del CRM del cliente actual (links de emails, webhooks, media para Meta/Twilio).
    Toma esquema y puerto de SITE_URL y el host del dominio principal del cliente:
    SITE_URL=https://tucrm.com + cliente "acme" → https://acme.tucrm.com
    """
    cliente = cliente_actual()
    if cliente is None:
        return settings.SITE_URL
    dominio = getattr(cliente, '_dominio_cache', None)
    if dominio is None:
        dominio = cliente.dominio_principal
        cliente._dominio_cache = dominio
    if not dominio:
        return settings.SITE_URL
    partes = urlsplit(settings.SITE_URL)
    puerto = f':{partes.port}' if partes.port else ''
    return f'{partes.scheme}://{dominio}{puerto}'


def host_sitio():
    return url_sitio().split('://', 1)[-1]


def usa_https():
    return url_sitio().startswith('https')


def dominio_para(subdominio):
    """Dominio completo de un cliente nuevo: acme → acme.tucrm.com"""
    return f'{subdominio}.{settings.SAAS_DOMINIO}'


# ── Límites del plan ───────────────────────────────────────────────────────

def limite_alcanzado(recurso):
    """
    (alcanzado, limite) para 'usuarios' o 'lineas_whatsapp' del cliente actual.
    Fuera del modo multi-cliente (schema público, tests, comandos) no hay límites.
    """
    cliente = cliente_actual()
    if cliente is None:
        return False, 0
    if recurso == 'usuarios':
        from apps.users.models import User
        limite = cliente.limite_usuarios
        usados = User.objects.filter(is_active=True).count()
    elif recurso == 'lineas_whatsapp':
        from apps.whatsapp.models import LineaWhatsApp
        limite = cliente.limite_lineas_whatsapp
        usados = LineaWhatsApp.objects.count()
    else:  # pragma: no cover
        raise ValueError(recurso)
    return bool(limite) and usados >= limite, limite


# ── Funciones extra del cliente ────────────────────────────────────────────

def funciones_cliente():
    """Set de claves de funciones extra habilitadas para el cliente actual (vacío en el schema público)."""
    cliente = cliente_actual()
    return cliente.funciones_habilitadas if cliente is not None else set()


def tiene_funcion(clave):
    """¿El cliente actual tiene habilitada la función extra `clave`? (False fuera del modo multi-cliente)."""
    return clave in funciones_cliente()
