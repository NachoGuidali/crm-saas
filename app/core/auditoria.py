"""
Bitácora de acciones sensibles (C2). `auditar(...)` escribe una fila en el schema actual: en un request de un
cliente queda en el schema del cliente; en el panel (schema público), en el público.

Nunca debe tumbar la acción que la originó: si el registro falla, se loguea y sigue.
"""
import logging

logger = logging.getLogger('auditoria')


def auditar(accion, *, request=None, usuario=None, objeto='', detalle='', ip='', username=''):
    """
    accion: una de RegistroAuditoria.ACCION_CHOICES.
    usuario: el actor (User) o None; si no se pasa, se toma de request.user autenticado.
    username: nombre tipeado cuando no hay usuario (ej. login fallido).
    objeto/detalle: sobre qué y con qué datos.
    """
    try:
        from apps.users.models import RegistroAuditoria
        if usuario is None and request is not None:
            u = getattr(request, 'user', None)
            usuario = u if getattr(u, 'is_authenticated', False) else None
        if not ip and request is not None:
            from apps.users.services import ip_de
            ip = ip_de(request)
        desc = str(usuario) if usuario else (username or '—')
        RegistroAuditoria.objects.create(
            accion=accion, usuario=usuario if usuario and usuario.pk else None,
            usuario_desc=desc[:150], objeto=str(objeto)[:200], detalle=str(detalle)[:2000], ip=ip or None,
        )
    except Exception:  # pragma: no cover
        logger.exception('No se pudo registrar auditoría: %s %s', accion, objeto)
