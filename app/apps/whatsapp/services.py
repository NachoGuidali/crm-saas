import logging
import time

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import F, Sum
from django.utils import timezone

from core.phone import normalizar_telefono
from core.tenancy import schema_actual

from .models import Conversacion, LineaWhatsApp, Mensaje, Plantilla, reemplazar_variables_texto

logger = logging.getLogger('apps.whatsapp')


class ErrorEnvio(Exception):
    """Validación de envío (ventana de 24 h, contacto bloqueado…). El mensaje se muestra al usuario."""


# ── Contadores para la barra superior (cacheados: se consultan en cada página) ─

def _clave_no_leidos(user_id):
    return f'wa_no_leidos_{user_id}'


def contar_no_leidos_usuario(user):
    key = _clave_no_leidos(user.pk)
    n = cache.get(key)
    if n is None:
        n = (Conversacion.objects.visibles_para(user).filter(archivada=False, no_leidos__gt=0)
             .aggregate(t=Sum('no_leidos'))['t'] or 0)
        cache.set(key, n, 20)
    return n


def invalidar_no_leidos(user_ids):
    cache.delete_many([_clave_no_leidos(u) for u in user_ids if u])


def marcar_cambio_inbox():
    """Sello de 'hubo novedades' que consulta el polling liviano del inbox."""
    cache.set('wa_inbox_version', time.time(), None)


def version_inbox():
    return cache.get('wa_inbox_version') or 0


# ── Conversaciones ─────────────────────────────────────────────────────────

def obtener_conversacion(linea, telefono, nombre_perfil=''):
    telefono = normalizar_telefono(telefono)
    conv = Conversacion.objects.filter(linea=linea, telefono=telefono).first()
    if conv:
        return conv, False
    try:
        with transaction.atomic():
            # Si la línea tiene bot n8n, la conversación nueva arranca con el bot atendiendo.
            return Conversacion.objects.create(linea=linea, telefono=telefono, nombre_perfil=nombre_perfil[:200],
                                               bot_activo=linea.bot_n8n_activo), True
    except IntegrityError:
        return Conversacion.objects.get(linea=linea, telefono=telefono), False


def linea_para(contacto=None, oportunidad=None, user=None, preferida=None):
    """Elige por qué línea escribirle a un contacto: la del chat existente, la del embudo o la primera usable."""
    if preferida and preferida.activa:
        return preferida
    if contacto is not None:
        conv = (Conversacion.objects.filter(contacto=contacto, linea__activa=True)
                .select_related('linea').order_by('-ultimo_mensaje_at').first())
        if conv and (user is None or conv.linea.usable_por(user)):
            return conv.linea
    if oportunidad is not None and oportunidad.embudo.linea_whatsapp_id and oportunidad.embudo.linea_whatsapp.activa:
        return oportunidad.embudo.linea_whatsapp
    qs = LineaWhatsApp.para_usuario(user) if user else LineaWhatsApp.objects.filter(activa=True)
    return qs.first()


def conversacion_para_contacto(contacto, linea, agente=None):
    conv, creada = obtener_conversacion(linea, contacto.telefono)
    cambios = []
    if conv.contacto_id != contacto.pk:
        conv.contacto = contacto
        cambios.append('contacto')
    if agente and not conv.agente_id:
        conv.agente = agente
        cambios.append('agente')
    if cambios:
        conv.save(update_fields=cambios)
    return conv


# ── Entrantes ──────────────────────────────────────────────────────────────

def procesar_mensaje_entrante(linea, msg):
    """
    Registra un mensaje entrante. Idempotente (el proveedor puede reenviar el webhook).
    Si el número no existe en el CRM crea el contacto y, si la línea tiene embudo, la oportunidad.
    """
    from apps.crm import services as crm
    from apps.crm.models import Oportunidad

    if msg.wa_id and Mensaje.objects.filter(wa_id=msg.wa_id).exists():
        return None

    conv, conv_nueva = obtener_conversacion(linea, msg.telefono, msg.nombre_perfil)
    reabierta = conv.estado == Conversacion.ESTADO_CERRADA

    # Vincular con el CRM (sin duplicar: el contacto se busca por teléfono normalizado)
    oportunidad = None
    contacto = conv.contacto or crm.buscar_contacto(msg.telefono)
    if contacto is None or conv_nueva or reabierta:
        datos = {'telefono': msg.telefono, 'nombre': msg.nombre_perfil or f'WhatsApp {msg.telefono}'}
        if linea.embudo_id and (contacto is None or crm.oportunidad_activa_de(contacto) is None):
            from apps.pautas.services import resolver_pauta_whatsapp
            pauta, origen_pauta = resolver_pauta_whatsapp(msg.extra, msg.contenido)
            res = crm.ingresar_prospecto(datos, linea.embudo, Oportunidad.ORIGEN_WHATSAPP, fuente=linea.nombre,
                                         origen_pauta=origen_pauta, pauta=pauta)
            contacto, oportunidad = res.contacto, res.oportunidad
        elif contacto is None:
            contacto, _ = crm.upsert_contacto(datos)
    if oportunidad is None and contacto is not None:
        oportunidad = crm.oportunidad_activa_de(contacto)

    agente_id = conv.agente_id or (oportunidad.agente_id if oportunidad and oportunidad.activa else None)
    if agente_id is None and contacto is not None and (oportunidad is None or not oportunidad.activa):
        # Ya es cliente (socio): va a postventa o a su vendedor, no a la bandeja de ventas sin asignar
        responsable = crm.responsable_cliente(contacto)
        agente_id = responsable.pk if responsable else None

    local_url, mime = '', msg.media_mime
    if msg.tipo in Mensaje.TIPOS_MEDIA and (msg.media_id or msg.media_url or msg.wa_id):
        from .proveedores import get_proveedor
        try:
            local_url, mime = get_proveedor(linea).descargar_media(msg, conv.pk)
        except Exception as e:  # nunca perder el mensaje por la media
            logger.warning('Media no descargada (%s): %s', msg.wa_id, e)

    try:
        with transaction.atomic():
            mensaje = Mensaje.objects.create(
                conversacion=conv, direccion=Mensaje.DIR_ENTRANTE, tipo=msg.tipo, contenido=msg.contenido,
                media_url=local_url, media_mime=mime or '', media_filename=msg.media_filename, media_id=msg.media_id,
                wa_id=msg.wa_id, status=Mensaje.STATUS_ENTREGADO, timestamp=msg.timestamp or timezone.now(),
            )
    except IntegrityError:
        return None  # llegó dos veces en paralelo

    ahora = timezone.now()
    era_leida = conv.no_leidos == 0
    Conversacion.objects.filter(pk=conv.pk).update(
        contacto=contacto, agente_id=agente_id, no_leidos=F('no_leidos') + 1, ultimo_mensaje_at=ahora,
        ultimo_entrante_at=ahora, ultimo_mensaje_texto=(msg.contenido or f'[{msg.tipo}]')[:200], archivada=False,
        estado=Conversacion.ESTADO_PENDIENTE if reabierta or conv.estado == Conversacion.ESTADO_PENDIENTE
        else conv.estado,
        nombre_perfil=msg.nombre_perfil[:200] if msg.nombre_perfil else conv.nombre_perfil,
    )
    if oportunidad and oportunidad.activa:
        crm.tocar(oportunidad, ahora)
        from apps.automatizaciones.services import cliente_respondio
        transaction.on_commit(lambda: cliente_respondio(oportunidad))

    if agente_id and era_leida:
        from apps.users.models import User
        from apps.users.services import notificar
        agente = User.objects.filter(pk=agente_id).first()
        nombre = contacto.nombre if contacto else msg.telefono
        notificar(agente, 'whatsapp', f'WhatsApp de {nombre}', (msg.contenido or '')[:150],
                  f'/whatsapp/?conv={conv.pk}')
    invalidar_no_leidos([agente_id] + _ids_supervisores())
    marcar_cambio_inbox()

    # Bot n8n: si esta línea tiene bot y está atendiendo esta conversación, reenviar el entrante al flujo de n8n.
    if linea.bot_n8n_activo and linea.bot_n8n_url and \
            Conversacion.objects.filter(pk=conv.pk, bot_activo=True).exists():
        from .tasks import reenviar_a_n8n
        datos_bot = {
            'telefono': msg.telefono, 'contenido': msg.contenido or '', 'tipo': msg.tipo,
            'wa_id': msg.wa_id or '', 'nombre': (contacto.nombre if contacto else '') or msg.nombre_perfil or '',
        }
        transaction.on_commit(lambda: reenviar_a_n8n.delay(conv.pk, datos_bot))
    return mensaje


def _ids_supervisores():
    ids = cache.get('wa_ids_supervisores')
    if ids is None:
        from apps.users.models import User
        ids = [u.pk for u in User.objects.filter(is_active=True).exclude(rol=User.ROL_AGENTE)]
        cache.set('wa_ids_supervisores', ids, 300)
    return ids


def actualizar_estados(estados):
    """Actualiza entregado/leído/fallido sin retroceder (un 'delivered' tardío no pisa un 'read')."""
    for wa_id, status, error in estados:
        menores = [s for s, orden in Mensaje.STATUS_ORDEN.items() if orden < Mensaje.STATUS_ORDEN.get(status, 0)]
        qs = Mensaje.objects.filter(wa_id=wa_id)
        if status == Mensaje.STATUS_FALLIDO:
            qs.update(status=status, error=error[:500])
        else:
            qs.filter(status__in=menores).update(status=status)


# ── Salientes ──────────────────────────────────────────────────────────────

def enviar_mensaje(conv, usuario=None, texto='', plantilla=None, valores=None, archivo_url='', archivo_mime='',
                   archivo_nombre='', automatico=False, demora_segundos=0):
    """
    Encola un mensaje saliente. Valida la ventana de 24 h de las líneas oficiales.
    Devuelve el Mensaje creado (en estado pendiente); el envío real lo hace Celery.
    """
    if not conv.linea.activa:
        raise ErrorEnvio('La línea de WhatsApp está desactivada.')
    contacto = conv.contacto
    if automatico and contacto and not contacto.puede_recibir_mensajes:
        raise ErrorEnvio('El contacto no acepta mensajes (no contactar / teléfono inválido).')
    if plantilla is None and not conv.ventana_abierta:
        raise ErrorEnvio('Pasaron más de 24 h desde el último mensaje del cliente: en líneas oficiales '
                         'solo se puede enviar una plantilla aprobada.')
    if plantilla is not None and not plantilla.disponible_en(conv.linea):
        raise ErrorEnvio(f'La plantilla "{plantilla}" no está aprobada / configurada para la línea {conv.linea}.')

    if plantilla is not None:
        if valores is None:
            op = _oportunidad_de(contacto)
            valores = plantilla.valores_para(contacto, op, usuario)
        contenido, tipo = plantilla.renderizar(valores), Mensaje.TIPO_PLANTILLA
    elif archivo_url:
        from .proveedores.base import mediatype_de_mime
        contenido, tipo = texto, mediatype_de_mime(archivo_mime)
    else:
        if not (texto or '').strip():
            raise ErrorEnvio('El mensaje está vacío.')
        contenido, tipo = texto.strip(), Mensaje.TIPO_TEXTO

    ahora = timezone.now()
    mensaje = Mensaje.objects.create(
        conversacion=conv, direccion=Mensaje.DIR_SALIENTE, tipo=tipo, contenido=contenido, plantilla=plantilla,
        plantilla_valores=[str(v) for v in (valores or [])] if plantilla is not None else [],
        media_url=archivo_url, media_mime=archivo_mime, media_filename=archivo_nombre, enviado_por=usuario,
        automatico=automatico, timestamp=ahora, status=Mensaje.STATUS_PENDIENTE,
    )
    cambios = {'ultimo_mensaje_at': ahora, 'ultimo_mensaje_texto': (contenido or f'[{tipo}]')[:200], 'archivada': False}
    if usuario and not automatico:
        cambios['estado'] = Conversacion.ESTADO_ABIERTA
        cambios['no_leidos'] = 0
    Conversacion.objects.filter(pk=conv.pk).update(**cambios)

    from .tasks import enviar_mensaje_task
    mensaje_id = mensaje.pk
    transaction.on_commit(lambda: _encolar(enviar_mensaje_task, mensaje_id, demora_segundos))

    if contacto and usuario and not automatico:
        op = _oportunidad_de(contacto)
        if op:
            from apps.crm import services as crm
            crm.tocar(op)
            crm.marcar_primer_contacto(op)
            crm.avanzar_desde_inicial(op, usuario)
            if op.prioritaria and op.agente_id == usuario.pk:
                from apps.crm.models import Oportunidad
                Oportunidad.objects.filter(pk=op.pk).update(prioritaria=False, prioridad_motivo='')  # ya la atendió
    marcar_cambio_inbox()
    return mensaje


def _encolar(task, mensaje_id, demora):
    try:
        if demora:
            task.apply_async(args=[mensaje_id], countdown=demora)
        else:
            task.delay(mensaje_id)
    except Exception as e:
        logger.error('No se pudo encolar el mensaje %s: %s', mensaje_id, e)
        Mensaje.objects.filter(pk=mensaje_id).update(status=Mensaje.STATUS_FALLIDO, error=f'Cola no disponible: {e}')


def _oportunidad_de(contacto):
    if contacto is None:
        return None
    from apps.crm.services import oportunidad_activa_de
    return oportunidad_activa_de(contacto)


def enviar_automatico(oportunidad, plantilla=None, texto='', linea=None, accion=None):
    """
    Mensaje disparado por una automatización. Respeta 'no contactar', la ventana de 24 h
    (si no hay plantilla y la ventana está cerrada, no se envía) y el ritmo anti-bloqueo de la línea.
    Devuelve (Mensaje | None, detalle).
    """
    contacto = oportunidad.contacto
    if not contacto.puede_recibir_mensajes:
        return None, 'Omitido: el contacto no acepta mensajes o no tiene teléfono válido.'
    if wa_dia_restante() == 0:
        return None, 'Omitido: se alcanzó el límite diario de WhatsApp del plan.'
    linea = linea_para(contacto, oportunidad, preferida=linea)
    if linea is None:
        return None, 'Omitido: no hay ninguna línea de WhatsApp activa.'
    conv = conversacion_para_contacto(contacto, linea, agente=oportunidad.agente)
    if plantilla is None and linea.es_oficial and not conv.ventana_abierta:
        return None, (f'Omitido: la línea {linea} es oficial y la ventana de 24 h está cerrada; '
                      'configurá una plantilla aprobada para esta acción.')
    if plantilla is not None and not plantilla.disponible_en(linea):
        if linea.es_oficial:
            return None, f'Omitido: la plantilla "{plantilla}" no está aprobada para {linea}.'
    if plantilla is None:
        texto = reemplazar_variables_texto(texto, contacto, oportunidad)
    demora = demora_para(linea)
    try:
        msg = enviar_mensaje(conv, texto=texto, plantilla=plantilla, automatico=True, demora_segundos=demora)
    except ErrorEnvio as e:
        return None, f'Omitido: {e}'
    if accion is not None:
        Mensaje.objects.filter(pk=msg.pk).update(accion=accion)
    return msg, f'Encolado por {linea}' + (f' (sale en {int(demora)} s)' if demora > 1 else '')


# ── Ritmo de envío por línea (anti-bloqueo) ────────────────────────────────

_LUA_TURNO = """
local ahora = tonumber(ARGV[1])
local gap = tonumber(ARGV[2])
local proximo = tonumber(redis.call('GET', KEYS[1]) or '0')
if proximo < ahora then proximo = ahora end
redis.call('SET', KEYS[1], tostring(proximo + gap), 'EX', 86400)
return tostring(proximo - ahora)
"""


def _redis():
    backend = settings.CACHES['default']['BACKEND']
    if 'redis' not in backend.lower():
        return None
    try:
        import redis
        return redis.Redis.from_url(settings.REDIS_URL)
    except Exception:  # pragma: no cover
        return None


def _gap_aleatorio(linea) -> int:
    """Pausa entre envíos: mínima, o aleatoria entre mínimo y máximo si se configuró un máximo mayor."""
    lo = max(int(linea.min_segundos_entre_envios or 0), 0)
    hi = max(int(linea.max_segundos_entre_envios or 0), 0)
    if hi > lo:
        import random
        return random.randint(lo, hi)
    return lo


def demora_para(linea, humanizar=True) -> float:
    """
    Segundos a esperar antes de enviar por esta línea. Combina el espaciado anti-ráfaga
    (reservar_turno, atómico entre workers) con una pausa aleatoria mínima tipo 'tipeando'
    para que el bot/automático no responda al instante y no genere un patrón detectable.
    """
    espera = reservar_turno(linea)
    if humanizar:
        espera = max(espera, _gap_aleatorio(linea))
    return espera


def reservar_turno(linea) -> float:
    """
    Reserva el próximo hueco de envío de la línea y devuelve cuántos segundos hay que esperar.
    Es atómico en Redis: varios workers encolando a la vez no rompen el espaciado, y ningún
    worker queda dormido esperando (el envío se programa con countdown). El gap es aleatorio
    (mínimo–máximo) para romper la periodicidad en ráfagas.
    """
    gap = _gap_aleatorio(linea)
    if gap == 0:
        return 0
    ahora = time.time()
    r = _redis()
    if r is not None:
        try:
            # Redis directo (no pasa por la KEY_FUNCTION de la caché): el schema del cliente va en la clave.
            clave = f'crmv:{schema_actual()}:wa_turno:{linea.pk}'
            return float(r.eval(_LUA_TURNO, 1, clave, ahora, gap))
        except Exception as e:  # pragma: no cover
            logger.warning('Redis no disponible para ritmo de envío: %s', e)
    key = f'wa_turno_{linea.pk}'
    proximo = max(cache.get(key) or 0, ahora)
    cache.set(key, proximo + gap, 86400)
    return proximo - ahora


def wa_salientes_hoy():
    """
    Mensajes de WhatsApp automáticos/difusiones enviados hoy en el cliente actual (para el límite diario del plan).
    Los mensajes 1 a 1 que escribe un agente (automatico=False) no cuentan ni se frenan.
    """
    from django.utils import timezone
    from .models import Mensaje
    return Mensaje.objects.filter(direccion=Mensaje.DIR_SALIENTE, automatico=True,
                                  timestamp__date=timezone.localdate()).count()


def wa_dia_restante():
    """Cuántos WhatsApp más puede mandar hoy el cliente. None = sin límite (o fuera del modo multi-cliente)."""
    from core.tenancy import cliente_actual
    cliente = cliente_actual()
    if cliente is None or not cliente.limite_wa_dia:
        return None
    return max(cliente.limite_wa_dia - wa_salientes_hoy(), 0)
