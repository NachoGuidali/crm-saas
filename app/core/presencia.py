"""
Presencia de usuarios ("conectados"): cada pantalla del CRM late cada 4–15 s (/pulso/); si un usuario no late en
TTL segundos se lo considera desconectado. Al cerrar la pestaña, el navegador avisa (sendBeacon) y queda desconectado
a los pocos segundos si no le queda otra pestaña abierta. Vive en Redis; las sesiones (para "tiempo conectado") en la base.
"""
import logging

from django.core.cache import cache

logger = logging.getLogger('apps.users')

TTL = 150      # sin señal de ninguna pestaña en este tiempo = desconectado (compu suspendida, sin internet, etc.)
GRACIA = 8     # al cerrar o cambiar de página: si la pestaña no vuelve a latir en estos segundos, se cerró


def _clave(user_id):
    return f'presencia:{user_id}'


def _tabs(user_id):
    return cache.get(f'presencia:tabs:{user_id}') or {}


def _vivas(tabs, ahora):
    return {t: ts for t, ts in tabs.items() if ts > ahora}


def ausente_minutos():
    from django.conf import settings
    return getattr(settings, 'PRESENCIA_AUSENTE_MINUTOS', 15)


def registrar_uso(user, segundos_sin_uso):
    """El navegador informa hace cuánto no se toca el CRM (mouse, teclado, clics) en ninguna pestaña."""
    import time
    try:
        seg = max(0, int(float(segundos_sin_uso)))
    except (TypeError, ValueError):
        return
    ultimo_uso = time.time() - seg
    clave = f'presencia:uso:{user.pk}'
    anterior = cache.get(clave) or 0
    if ultimo_uso > anterior + 1:
        cache.set(clave, ultimo_uso, 60 * 60 * 12)


def ausentes(ids, en_llamada=()):
    """{user_id: datetime del último uso} de los conectados que no tocan el CRM hace más de N minutos.
    Quien está en una llamada nunca figura ausente. No cambia el reparto: es solo informativo."""
    import time
    from datetime import datetime, timezone as dt_tz
    ids = [i for i in ids if i not in set(en_llamada)]
    if not ids:
        return {}
    limite = time.time() - ausente_minutos() * 60
    usos = cache.get_many([f'presencia:uso:{i}' for i in ids])
    out = {}
    for i in conectados(ids):
        uso = usos.get(f'presencia:uso:{i}')
        if uso and uso < limite:
            out[i] = datetime.fromtimestamp(uso, tz=dt_tz.utc)
    return out


def marcar(user, tab=None):
    """
    Marca al usuario como conectado (cada pestaña late con su propio id). Si recién se conecta, abre la sesión y
    reparte los prospectos que esperaban a alguien.
    """
    import time
    ahora = time.time()
    tabs = _vivas(_tabs(user.pk), ahora)
    # Sin id de pestaña (versión vieja del script en caché): cuenta poco, para no dejarla "en línea" de más
    tabs[tab or 'sin-id'] = ahora + (TTL if tab else 30)
    cache.set(f'presencia:tabs:{user.pk}', tabs, TTL)
    if cache.add(_clave(user.pk), 1, TTL):
        _abrir_sesion(user)
        _al_conectarse()
        return True
    cache.touch(_clave(user.pk), TTL)
    _registrar_pulso(user)
    return False


def salir_pestana(user, tab):
    """
    La pestaña se cerró o navegó a otra página del CRM. Se le da GRACIA segundos: si es una navegación, la página
    nueva late enseguida y no pasa nada; si se cerró y no queda ninguna otra pestaña abierta, queda desconectada.
    """
    import time
    ahora = time.time()
    tabs = _vivas(_tabs(user.pk), ahora)
    if tab in tabs:
        tabs[tab] = ahora + GRACIA
    vence = max(tabs.values(), default=ahora + GRACIA)
    cache.set(f'presencia:tabs:{user.pk}', tabs, TTL)
    restante = max(int(vence - ahora) + 1, 1)
    cache.touch(_clave(user.pk), restante)  # la clave del usuario vence con su última pestaña
    if not any(ts > ahora + GRACIA for ts in tabs.values()):
        from django.utils import timezone
        from apps.users.models import SesionConexion
        sid = cache.get(f'presencia:sesion:{user.pk}')
        if sid:
            SesionConexion.objects.filter(pk=sid, fin__isnull=True).update(ultimo=timezone.now())


def desconectar(user):
    cache.delete_many([_clave(user.pk), f'presencia:tabs:{user.pk}'])
    from django.utils import timezone
    from apps.users.models import SesionConexion
    sid = cache.get(f'presencia:sesion:{user.pk}')
    if sid:
        SesionConexion.objects.filter(pk=sid, fin__isnull=True).update(fin=timezone.now(), ultimo=timezone.now())
        cache.delete(f'presencia:sesion:{user.pk}')


def _abrir_sesion(user):
    from django.utils import timezone
    from apps.users.models import SesionConexion
    ahora = timezone.now()
    sesion = SesionConexion.objects.create(usuario=user, inicio=ahora, ultimo=ahora)
    cache.set(f'presencia:sesion:{user.pk}', sesion.pk, 60 * 60 * 24)


def _registrar_pulso(user):
    """Actualiza el "último pulso" de la sesión como mucho una vez cada 30 s (no escribe en cada pulso)."""
    if not cache.add(f'presencia:pulso_db:{user.pk}', 1, 30):
        return
    from django.utils import timezone
    from apps.users.models import SesionConexion
    sid = cache.get(f'presencia:sesion:{user.pk}')
    if sid and SesionConexion.objects.filter(pk=sid, fin__isnull=True).update(ultimo=timezone.now()):
        return
    _abrir_sesion(user)  # se perdió la referencia (reinicio de Redis): abre otra


def conectados(ids):
    ids = list(ids)
    if not ids:
        return set()
    encontrados = cache.get_many([_clave(i) for i in ids])
    return {i for i in ids if _clave(i) in encontrados}


def esta_conectado(user):
    return bool(cache.get(_clave(user.pk)))


def _al_conectarse():
    if not cache.add('presencia:reparto', 1, 15):  # una sola corrida aunque se conecten varios a la vez
        return
    try:
        from apps.crm.tasks import asignar_pendientes
        asignar_pendientes.delay()
    except Exception:  # sin broker: lo levanta el barrido periódico
        logger.warning('No se pudo encolar el reparto de pendientes al conectarse un usuario', exc_info=True)
