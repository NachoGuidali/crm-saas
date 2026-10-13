import logging

from django.conf import settings
from django.core.cache import cache

from core import tenancy

logger = logging.getLogger('apps.users')


# ── Notificaciones internas ─────────────────────────────────────────────────

def _cache_key_notif(user_id):
    return f'notif_no_leidas_{user_id}'


def notificar(destinatario, tipo, titulo, cuerpo='', url=''):
    """Crea una notificación interna. Nunca rompe el flujo que la llama."""
    from .models import NotificacionInterna
    if destinatario is None:
        return None
    try:
        notif = NotificacionInterna.objects.create(
            destinatario=destinatario, tipo=tipo, titulo=titulo[:200], cuerpo=cuerpo or '', url=url or '',
        )
        cache.delete(_cache_key_notif(destinatario.pk))
        return notif
    except Exception as e:  # pragma: no cover - defensivo
        logger.error('No se pudo crear notificación para %s: %s', destinatario, e)
        return None


def notificar_varios(destinatarios, tipo, titulo, cuerpo='', url=''):
    vistos = set()
    for d in destinatarios:
        if d and d.pk not in vistos:
            vistos.add(d.pk)
            notificar(d, tipo, titulo, cuerpo, url)


def contar_no_leidas(user):
    key = _cache_key_notif(user.pk)
    count = cache.get(key)
    if count is None:
        count = user.notificaciones.filter(leida=False).count()
        cache.set(key, count, 60)
    return count


def invalidar_contador(user):
    cache.delete(_cache_key_notif(user.pk))


def supervisores_de(embudo=None):
    """Usuarios que deben enterarse de eventos de supervisión (ventas, estancados)."""
    from .models import User
    if embudo is not None and embudo.supervisores.exists():
        return list(embudo.supervisores.filter(is_active=True))
    return [u for u in User.objects.filter(is_active=True).exclude(rol=User.ROL_AGENTE)
            if u.tiene_permiso('supervision')]


# ── Bloqueo de login por intentos fallidos ──────────────────────────────────

def _clave_intentos(username, ip):
    return f'login_fail_{(username or "").lower()}_{ip}'


def login_bloqueado(username, ip) -> bool:
    return cache.get(_clave_intentos(username, ip), 0) >= settings.LOGIN_MAX_INTENTOS


def registrar_login_fallido(username, ip):
    key = _clave_intentos(username, ip)
    ttl = settings.LOGIN_BLOQUEO_MINUTOS * 60
    if not cache.add(key, 1, ttl):
        try:
            cache.incr(key)
        except ValueError:
            cache.set(key, 1, ttl)


def limpiar_login_fallido(username, ip):
    cache.delete(_clave_intentos(username, ip))


def ip_de(request):
    """
    IP real del cliente. nginx agrega la IP del verdadero cliente al final de X-Forwarded-For, así que se toma
    la posición PROXIES_CONFIABLES desde la derecha. Un cliente que falsee el header solo agrega entradas a la
    izquierda: no puede mover esa posición. Sin proxies (dev) se usa REMOTE_ADDR.
    """
    n = settings.PROXIES_CONFIABLES
    fwd = [p.strip() for p in request.META.get('HTTP_X_FORWARDED_FOR', '').split(',') if p.strip()]
    if n > 0 and len(fwd) >= n:
        return fwd[-n]
    return request.META.get('REMOTE_ADDR', '')


# ── Recupero de contraseña / invitaciones ───────────────────────────────────

def enviar_link_password(user, request=None, bienvenida=False):
    """Manda el email con el link para definir contraseña (recupero o invitación de alta)."""
    from django.contrib.auth.forms import PasswordResetForm
    if not user.email:
        return False
    form = PasswordResetForm({'email': user.email})
    if not form.is_valid():
        return False
    dominio = tenancy.host_sitio()
    form.save(
        request=request,
        use_https=tenancy.usa_https(),
        domain_override=dominio,
        subject_template_name='users/emails/reset_subject.txt',
        email_template_name='users/emails/reset_body.txt',
        html_email_template_name='users/emails/reset_body.html',
        extra_email_context={'bienvenida': bienvenida, 'crm_nombre': settings.CRM_NOMBRE},
        from_email=settings.DEFAULT_FROM_EMAIL,
    )
    return True
