"""Proveedor Evolution API (Baileys): conexión por QR, sin verificación de Meta."""
import base64
import json
import logging
import os
from datetime import datetime, timezone as dt_tz

from django.conf import settings
from django.core.cache import cache

from core.phone import normalizar_telefono, telefono_para_proveedor
from core.tenancy import schema_actual

from .base import ErrorProveedor, MensajeEntrante, ProveedorBase, ResultadoWebhook, guardar_media, mediatype_de_mime

logger = logging.getLogger('apps.whatsapp')

_TIPOS = {
    'conversation': 'text', 'extendedTextMessage': 'text', 'imageMessage': 'image', 'stickerMessage': 'image',
    'videoMessage': 'video', 'audioMessage': 'audio', 'documentMessage': 'document',
    'documentWithCaptionMessage': 'document', 'buttonsResponseMessage': 'text', 'listResponseMessage': 'text',
    'templateButtonReplyMessage': 'text',
}
_ESTADOS_MSG = {'PENDING': 'pending', 'SERVER_ACK': 'sent', 'SENT': 'sent', 'DELIVERY_ACK': 'delivered',
                'READ': 'read', 'PLAYED': 'read', 'ERROR': 'failed', 'FAILED': 'failed'}


def prefijo_instancia():
    """Evolution es un servidor compartido por todos los clientes: los nombres llevan el schema para no chocar."""
    # El schema no tiene guiones (acme-sa → c_acme_sa), así el "-" final separa sin ambigüedad: "crm-c_acme-"
    # nunca es prefijo de las instancias de "crm-c_acme_sa-".
    return f'crm-{schema_actual()}-'


def nombre_instancia(linea):
    return f'{prefijo_instancia()}linea-{linea.pk}'


class ProveedorEvolution(ProveedorBase):
    nombre = 'Evolution API'

    @property
    def instancia(self):
        return self.linea.evolution_instancia or nombre_instancia(self.linea)

    def _url(self, path):
        return f'{self.linea.get_evolution_url()}{path}'

    def _headers(self):
        return {'apikey': self.linea.get_evolution_key(), 'Content-Type': 'application/json'}

    def _post(self, path, payload, log_body=None, timeout=20):
        resp = self._http('POST', self._url(path), json=payload, headers=self._headers(), timeout=timeout,
                          log_body=log_body)
        self._chequear(resp, 'Evolution')
        try:
            return resp.json()
        except ValueError:
            return {}

    # ── Envío ────────────────────────────────────────────────────────────
    def enviar_texto(self, telefono, texto):
        data = self._post(f'/message/sendText/{self.instancia}',
                          {'number': telefono_para_proveedor(telefono), 'text': texto})
        return (data.get('key') or {}).get('id', '')

    def enviar_media(self, telefono, url, mime, filename='', caption=''):
        tipo = mediatype_de_mime(mime)
        media = url
        ruta_local = _ruta_local(url)
        if ruta_local:  # archivo nuestro: se manda en base64, no hace falta URL pública
            with open(ruta_local, 'rb') as f:
                media = base64.b64encode(f.read()).decode()
        if tipo == 'audio':
            data = self._post(f'/message/sendWhatsAppAudio/{self.instancia}',
                              {'number': telefono_para_proveedor(telefono), 'audio': media},
                              log_body={'number': telefono, 'audio': '<audio>'}, timeout=60)
        else:
            payload = {'number': telefono_para_proveedor(telefono), 'mediatype': tipo, 'media': media,
                       'mimetype': mime, 'fileName': filename or os.path.basename(url)}
            if caption:
                payload['caption'] = caption
            data = self._post(f'/message/sendMedia/{self.instancia}', payload,
                              log_body={**payload, 'media': '<media>'}, timeout=60)
        return (data.get('key') or {}).get('id', '')

    # ── Conexión / QR ────────────────────────────────────────────────────
    def consultar_estado(self):
        from apps.whatsapp.models import LineaWhatsApp as L
        try:
            resp = self._http('GET', self._url(f'/instance/connectionState/{self.instancia}'),
                              headers=self._headers(), timeout=10)
        except ErrorProveedor as e:
            return L.ESTADO_ERROR, str(e)[:300]
        if resp.status_code == 404:
            return L.ESTADO_DESCONECTADA, 'La instancia no existe todavía en Evolution.'
        if resp.status_code in (401, 403):
            return L.ESTADO_ERROR, 'API key de Evolution inválida.'
        try:
            data = resp.json()
        except ValueError:
            return L.ESTADO_ERROR, resp.text[:300]
        state = (data.get('instance') or {}).get('state') or data.get('state') or 'close'
        return {'open': L.ESTADO_CONECTADA, 'connecting': L.ESTADO_CONECTANDO}.get(state, L.ESTADO_DESCONECTADA), ''

    def asegurar_instancia(self):
        try:
            resp = self._http('GET', self._url('/instance/fetchInstances'), headers=self._headers(), timeout=10)
            if resp.ok:
                for item in resp.json() if isinstance(resp.json(), list) else []:
                    nombre = (item.get('instance') or {}).get('instanceName') or item.get('instanceName') or item.get('name')
                    if nombre == self.instancia:
                        return
        except (ErrorProveedor, ValueError):
            pass
        resp = self._http('POST', self._url('/instance/create'), headers=self._headers(), timeout=20,
                          json={'instanceName': self.instancia, 'integration': 'WHATSAPP-BAILEYS', 'qrcode': True})
        if resp.status_code not in (200, 201, 403, 409):
            self._chequear(resp, 'Crear instancia')

    def configurar_webhook(self):
        # Se mandan ambas variantes de nombres de campo (cambiaron entre versiones 2.x de Evolution).
        payload = {'webhook': {
            'enabled': True, 'url': self.linea.webhook_url, 'byEvents': False, 'base64': False,
            'webhook_by_events': False, 'webhook_base64': False,
            'events': ['MESSAGES_UPSERT', 'MESSAGES_UPDATE', 'CONNECTION_UPDATE', 'QRCODE_UPDATED'],
        }}
        self._post(f'/webhook/set/{self.instancia}', payload)

    def obtener_qr(self):
        """Pide el QR para vincular. Devuelve el data-URI base64 o None si ya está conectada."""
        self.asegurar_instancia()
        try:
            self.configurar_webhook()
        except ErrorProveedor as e:
            logger.warning('No se pudo configurar webhook de %s: %s', self.instancia, e)
        resp = self._http('GET', self._url(f'/instance/connect/{self.instancia}'), headers=self._headers(), timeout=20)
        qr = None
        if resp.ok:
            try:
                data = resp.json()
                qr = data.get('base64') or (data.get('qrcode') or {}).get('base64')
            except ValueError:
                pass
        return qr or cache.get(self.clave_qr())

    def clave_qr(self):
        return f'wa_qr_linea_{self.linea.pk}'

    def desconectar(self):
        try:
            self._http('DELETE', self._url(f'/instance/logout/{self.instancia}'), headers=self._headers(), timeout=10)
        except ErrorProveedor:
            pass

    # ── Webhook ──────────────────────────────────────────────────────────
    def parsear_webhook(self, request):
        from apps.whatsapp.models import LineaWhatsApp as L
        try:
            payload = json.loads(request.body or b'{}')
        except ValueError:
            return ResultadoWebhook()
        evento = (payload.get('event') or '').lower().replace('_', '.')
        data = payload.get('data')
        res = ResultadoWebhook()
        if evento == 'messages.upsert':
            items = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
            for item in items:
                msg = _parsear_mensaje(item)
                if msg:
                    res.mensajes.append(msg)
        elif evento == 'messages.update':
            items = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
            for item in items:
                wa_id = (item.get('key') or {}).get('id') or item.get('keyId') or ''
                raw = (item.get('update') or {}).get('status') or item.get('status') or ''
                estado = _ESTADOS_MSG.get(str(raw).upper())
                if wa_id and estado:
                    res.estados.append((wa_id, estado, ''))
        elif evento == 'connection.update' and isinstance(data, dict):
            state = data.get('state') or data.get('connection')
            res.conexion = {'open': L.ESTADO_CONECTADA, 'connecting': L.ESTADO_CONECTANDO,
                            'close': L.ESTADO_DESCONECTADA}.get(state)
        elif evento == 'qrcode.updated' and isinstance(data, dict):
            qr = (data.get('qrcode') or {}).get('base64') or data.get('base64')
            if qr:
                cache.set(self.clave_qr(), qr, 60)
                res.qr = qr
        return res

    def descargar_media(self, msg, conv_pk):
        try:
            data = self._post(f'/chat/getBase64FromMediaMessage/{self.instancia}',
                              {'message': {'key': {'id': msg.wa_id}}}, timeout=60)
        except ErrorProveedor as e:
            logger.warning('No se pudo descargar media %s: %s', msg.wa_id, e)
            return '', ''
        b64 = data.get('base64') or ''
        if not b64:
            return '', ''
        if ',' in b64[:100]:
            b64 = b64.split(',', 1)[1]
        mime = data.get('mimetype') or msg.media_mime or 'application/octet-stream'
        return guardar_media(base64.b64decode(b64), conv_pk, msg.wa_id, mime, msg.media_filename), mime


def _ruta_local(url):
    if url.startswith(settings.MEDIA_URL):
        from core.storage import media_root
        ruta = os.path.join(media_root(), url[len(settings.MEDIA_URL):])
        return ruta if os.path.exists(ruta) else None
    return None


def _parsear_mensaje(data):
    if not isinstance(data, dict):
        return None
    key = data.get('key') or {}
    if key.get('fromMe'):
        return None
    jid = key.get('remoteJid') or ''
    if '@g.us' in jid or 'status@broadcast' in jid:
        return None
    # Chats con identificador "@lid" (privacidad nueva de WhatsApp): el teléfono real viene aparte.
    if '@lid' in jid:
        jid = key.get('remoteJidAlt') or key.get('senderPn') or data.get('senderPn') or ''
    telefono = normalizar_telefono(jid.split('@')[0])
    if not telefono:
        return None
    raw_tipo = data.get('messageType') or 'conversation'
    message = data.get('message') or {}
    tipo = _TIPOS.get(raw_tipo, 'text')
    contenido, url, mime, filename = _contenido(message, raw_tipo)
    ts = data.get('messageTimestamp')
    try:
        timestamp = datetime.fromtimestamp(int(ts), tz=dt_tz.utc) if ts else None
    except (TypeError, ValueError):
        timestamp = None
    extra = {}
    ctx = data.get('contextInfo') or next((v.get('contextInfo') for v in message.values()
                                           if isinstance(v, dict) and v.get('contextInfo')), None) or {}
    anuncio = ctx.get('externalAdReply') or {}
    if anuncio:  # el chat se abrió desde un anuncio ("clic para WhatsApp")
        extra['pauta'] = (anuncio.get('title') or anuncio.get('sourceId') or 'Anuncio Meta')[:200]
        extra['referral'] = {k: anuncio.get(k) for k in ('sourceType', 'sourceId', 'sourceUrl', 'title', 'ctwaClid')}
    return MensajeEntrante(
        telefono=telefono, wa_id=key.get('id', ''), tipo=tipo, contenido=contenido,
        nombre_perfil=data.get('pushName') or '', timestamp=timestamp,
        media_url=url, media_mime=mime, media_filename=filename, extra=extra,
    )


def _contenido(message, raw):
    if raw in ('conversation', 'extendedTextMessage'):
        return message.get('conversation') or (message.get('extendedTextMessage') or {}).get('text', ''), '', '', ''
    if raw == 'buttonsResponseMessage':
        return (message.get(raw) or {}).get('selectedDisplayText', ''), '', '', ''
    if raw == 'listResponseMessage':
        return (message.get(raw) or {}).get('title', ''), '', '', ''
    if raw == 'templateButtonReplyMessage':
        return (message.get(raw) or {}).get('selectedDisplayText', ''), '', '', ''
    obj = message.get(raw) or {}
    if raw == 'documentWithCaptionMessage':
        obj = ((obj.get('message') or {}).get('documentMessage')) or {}
    etiqueta = {'imageMessage': '', 'stickerMessage': '[Sticker]', 'videoMessage': '[Video]',
                'audioMessage': '[Audio]'}.get(raw, '[Documento]')
    texto = obj.get('caption') or obj.get('title') or obj.get('fileName') or etiqueta
    if raw in _TIPOS:
        return texto, obj.get('url', ''), obj.get('mimetype', ''), obj.get('fileName') or obj.get('title') or ''
    return f'[{raw}]', '', '', ''
