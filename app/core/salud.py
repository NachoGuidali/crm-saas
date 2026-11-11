"""
Salud del sistema: ¿están vivos los workers de Celery (y beat)? Cada minuto beat manda un "latido" a cada cola;
el worker que la atiende anota la hora en Redis. Si una cola no late hace más de MAX_SILENCIO, su worker está caído
(o beat, si no late ninguna). Se ve en /salud/ (para un monitor externo), en un cartel para admins y por email.
"""
import logging
import time

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger('core.salud')

# cola → contenedor que la atiende (docker-compose.yml)
COLAS = {
    'default': 'worker', 'entrantes': 'worker',
    'salientes': 'worker-masivos', 'masivos': 'worker-masivos',
    'agente': 'worker-agente',
}
MAX_SILENCIO = 5 * 60
_INICIO = time.time()  # al arrancar la web todavía no hubo latidos: no alarmar en los primeros minutos


def _redis():
    import redis
    return redis.Redis.from_url(settings.REDIS_URL, socket_timeout=2, socket_connect_timeout=2)


def latir(cola):
    _redis().set(f'salud:latido:{cola}', int(time.time()), ex=3600)


def estado():
    """{'ok': bool, 'problemas': [texto], 'colas': {cola: segundos_desde_el_ultimo_latido o None}}"""
    problemas, colas = [], {}
    try:
        r = _redis()
        ahora = time.time()
        valores = r.mget([f'salud:latido:{c}' for c in COLAS])
    except Exception as e:
        return {'ok': False, 'problemas': [f'No hay conexión con Redis: {e}'], 'colas': {}}
    recien_arrancada = time.time() - _INICIO < MAX_SILENCIO
    caidos = {}
    for cola, v in zip(COLAS, valores):
        edad = int(ahora - int(v)) if v else None
        colas[cola] = edad
        if (edad is None and not recien_arrancada) or (edad is not None and edad > MAX_SILENCIO):
            caidos.setdefault(COLAS[cola], []).append(cola)
    if caidos and len(caidos) == len(set(COLAS.values())) and not recien_arrancada:
        problemas.append('No responde ningún worker (o está caído beat): no se envían ni procesan mensajes.')
    else:
        for contenedor, cs in caidos.items():
            problemas.append(f'El contenedor "{contenedor}" no responde (colas: {", ".join(cs)}).')
    return {'ok': not problemas, 'problemas': problemas, 'colas': colas}


def estado_cacheado():
    """Para el cartel de las páginas: se recalcula cada 30 s, no en cada request. Si hay problema, avisa por email."""
    e = cache.get('salud:estado')
    if e is None:
        e = estado()
        cache.set('salud:estado', e, 30)
        if not e['ok']:
            avisar_por_email(e)
    return e


def avisar_por_email(e):
    """Un email cada 30 minutos mientras dure el problema. Se manda desde la web (no por Celery: puede estar caído)."""
    destinatarios = list(dict.fromkeys(d for d in [settings.ALERTAS_EMAIL, settings.SAAS_EMAIL_AVISOS_COPIA] if d))
    if not destinatarios:
        return
    try:
        if not _redis().set('salud:email_enviado', 1, ex=1800, nx=True):
            return
        from django.core.mail import send_mail
        send_mail(f'⚠ {settings.CRM_NOMBRE}: falla en el servidor', 'Se detectó un problema:\n\n- '
                  + '\n- '.join(e['problemas']) + '\n\nRevisá en el servidor: docker compose ps / docker compose logs.',
                  None, destinatarios, fail_silently=True)
    except Exception:
        logger.exception('No se pudo mandar el aviso de salud')
