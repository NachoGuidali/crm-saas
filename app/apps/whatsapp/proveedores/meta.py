"""Proveedor WhatsApp Cloud API (Meta, oficial)."""
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone as dt_tz

from core.phone import normalizar_telefono, telefono_para_proveedor

from .base import (ErrorProveedor, MensajeEntrante, ProveedorBase, ResultadoWebhook, guardar_media,
                   mediatype_de_mime, url_publica)

logger = logging.getLogger('apps.whatsapp')
GRAPH = 'https://graph.facebook.com'


class ProveedorMeta(ProveedorBase):
    nombre = 'Meta Cloud API'

    def _url(self, path):
        return f'{GRAPH}/{self.linea.meta_api_version or "v21.0"}/{path}'

    def _headers(self):
        return {'Authorization': f'Bearer {self.linea.meta_access_token}', 'Content-Type': 'application/json'}

    def _enviar(self, payload, timeout=20):
        payload = {'messaging_product': 'whatsapp', **payload}
        resp = self._http('POST', self._url(f'{self.linea.meta_phone_number_id}/messages'), json=payload,
                          headers=self._headers(), timeout=timeout)
        if resp.status_code >= 300:
            detalle = _error_meta(resp)
            # 131047 = fuera de la ventana de 24 h; 131026 = número sin WhatsApp: no tiene sentido reintentar
            raise ErrorProveedor(f'Meta: {detalle}', reintentable=resp.status_code >= 500, status=resp.status_code)
        mensajes = resp.json().get('messages') or [{}]
        return mensajes[0].get('id', '')

    def enviar_texto(self, telefono, texto):
        return self._enviar({'to': telefono_para_proveedor(telefono), 'type': 'text',
                             'text': {'body': texto, 'preview_url': True}})

    def enviar_media(self, telefono, url, mime, filename='', caption=''):
        tipo = mediatype_de_mime(mime)
        obj = {'link': url_publica(url)}
        if caption and tipo != 'audio':
            obj['caption'] = caption
        if filename and tipo == 'document':
            obj['filename'] = filename
        return self._enviar({'to': telefono_para_proveedor(telefono), 'type': tipo, tipo: obj}, timeout=40)

    def enviar_plantilla(self, telefono, plantilla, valores):
        componentes = []
        if valores:
            componentes.append({'type': 'body', 'parameters': [{'type': 'text', 'text': str(v) or '-'} for v in valores]})
        return self._enviar({'to': telefono_para_proveedor(telefono), 'type': 'template', 'template': {
            'name': plantilla.get_meta_nombre(), 'language': {'code': plantilla.idioma}, 'components': componentes,
        }})

    def consultar_estado(self):
        from apps.whatsapp.models import LineaWhatsApp as L
        if not (self.linea.meta_access_token and self.linea.meta_phone_number_id):
            return L.ESTADO_DESCONECTADA, 'Faltan credenciales de Meta.'
        try:
            resp = self._http('GET', self._url(self.linea.meta_phone_number_id), headers=self._headers(), timeout=10,
                              params={'fields': 'display_phone_number,verified_name,quality_rating'})
        except ErrorProveedor as e:
            return L.ESTADO_ERROR, str(e)[:300]
        if not resp.ok:
            return L.ESTADO_ERROR, _error_meta(resp)[:300]
        data = resp.json()
        return L.ESTADO_CONECTADA, f'{data.get("verified_name", "")} · calidad {data.get("quality_rating", "?")}'

    def sincronizar_plantillas(self):
        """Actualiza el estado de aprobación de las plantillas locales con lo que tiene Meta."""
        from apps.whatsapp.models import Plantilla
        if not self.linea.meta_waba_id:
            return 0
        url, params, remotas = self._url(f'{self.linea.meta_waba_id}/message_templates'), {'limit': 100}, []
        while url:
            resp = self._http('GET', url, headers=self._headers(), params=params, timeout=20)
            self._chequear(resp, 'Plantillas Meta')
            data = resp.json()
            remotas.extend(data.get('data', []))
            url, params = (data.get('paging') or {}).get('next'), None
        actualizadas = 0
        por_nombre = {t.get('name'): t for t in remotas}
        for p in Plantilla.objects.all():
            t = por_nombre.get(p.get_meta_nombre())
            if t and (p.meta_estado != t.get('status') or p.meta_rechazo != (t.get('rejected_reason') or '')):
                p.meta_estado = t.get('status', p.meta_estado)[:10]
                p.meta_rechazo = t.get('rejected_reason') or ''
                p.save(update_fields=['meta_estado', 'meta_rechazo'])
                actualizadas += 1
        return actualizadas

    def crear_plantilla(self, plantilla):
        payload = {'name': plantilla.get_meta_nombre(), 'language': plantilla.idioma, 'category': plantilla.categoria,
                   'components': [{'type': 'BODY', 'text': plantilla.cuerpo}]}
        n = plantilla.cantidad_variables()
        if n:
            payload['components'][0]['example'] = {'body_text': [[f'ejemplo{i}' for i in range(1, n + 1)]]}
        resp = self._http('POST', self._url(f'{self.linea.meta_waba_id}/message_templates'), json=payload,
                          headers=self._headers())
        if resp.status_code >= 300:
            raise ErrorProveedor(_error_meta(resp), reintentable=False)
        return resp.json()

    # ── Webhook ──────────────────────────────────────────────────────────
    def verificar_handshake(self, request):
        """GET de suscripción del webhook en Meta. Devuelve el challenge o None."""
        if (request.GET.get('hub.mode') == 'subscribe' and self.linea.meta_verify_token
                and request.GET.get('hub.verify_token') == self.linea.meta_verify_token):
            return request.GET.get('hub.challenge', '')
        return None

    def validar_webhook(self, request):
        secreto = self.linea.meta_app_secret
        if not secreto:
            return True  # sin app secret cargado no se puede validar (la URL ya es secreta)
        firma = request.headers.get('X-Hub-Signature-256', '')
        if not firma.startswith('sha256='):
            return False
        esperado = hmac.new(secreto.encode(), request.body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(esperado, firma.split('=', 1)[1])

    def parsear_webhook(self, request):
        res = ResultadoWebhook()
        try:
            payload = json.loads(request.body or b'{}')
        except ValueError:
            return res
        for entry in payload.get('entry', []):
            for change in entry.get('changes', []):
                value = change.get('value') or {}
                perfiles = {c.get('wa_id'): (c.get('profile') or {}).get('name', '') for c in value.get('contacts', [])}
                for m in value.get('messages', []):
                    msg = _parsear(m, perfiles)
                    if msg:
                        res.mensajes.append(msg)
                for st in value.get('statuses', []):
                    estado = {'sent': 'sent', 'delivered': 'delivered', 'read': 'read', 'failed': 'failed'}.get(st.get('status'))
                    if st.get('id') and estado:
                        errores = st.get('errors') or []
                        error = (errores[0].get('title') or errores[0].get('message') or '') if errores else ''
                        res.estados.append((st['id'], estado, error))
        return res

    def descargar_media(self, msg, conv_pk):
        if not msg.media_id:
            return '', ''
        try:
            meta = self._http('GET', self._url(msg.media_id), headers=self._headers(), timeout=15)
            if not meta.ok:
                return '', ''
            info = meta.json()
            archivo = self._http('GET', info['url'], headers=self._headers(), timeout=60, log_body='<download>')
            if not archivo.ok:
                return '', ''
        except (ErrorProveedor, KeyError, ValueError) as e:
            logger.warning('Media Meta %s no descargada: %s', msg.media_id, e)
            return '', ''
        mime = info.get('mime_type') or msg.media_mime
        return guardar_media(archivo.content, conv_pk, msg.media_id, mime, msg.media_filename), mime


def _error_meta(resp):
    try:
        err = resp.json().get('error', {})
        return f'{err.get("code", resp.status_code)} {err.get("message", "")} {(err.get("error_data") or {}).get("details", "")}'.strip()
    except ValueError:
        return resp.text[:300]


def _parsear(m, perfiles):
    tipo_raw = m.get('type', 'text')
    telefono = normalizar_telefono(m.get('from', ''))
    if not telefono:
        return None
    contenido, media_id, mime, filename = '', '', '', ''
    if tipo_raw == 'text':
        contenido = (m.get('text') or {}).get('body', '')
    elif tipo_raw in ('image', 'video', 'audio', 'document', 'sticker'):
        obj = m.get(tipo_raw) or {}
        media_id, mime, filename = obj.get('id', ''), obj.get('mime_type', ''), obj.get('filename', '')
        contenido = obj.get('caption') or filename or {'audio': '[Audio]', 'video': '[Video]', 'sticker': '[Sticker]',
                                                         'document': '[Documento]'}.get(tipo_raw, '')
    elif tipo_raw == 'button':
        contenido = (m.get('button') or {}).get('text', '')
    elif tipo_raw == 'interactive':
        inter = m.get('interactive') or {}
        contenido = (inter.get('button_reply') or inter.get('list_reply') or {}).get('title', '')
    else:
        contenido = f'[{tipo_raw}]'
    tipo = {'sticker': 'image'}.get(tipo_raw, tipo_raw if tipo_raw in ('image', 'video', 'audio', 'document') else 'text')
    try:
        ts = datetime.fromtimestamp(int(m.get('timestamp')), tz=dt_tz.utc)
    except (TypeError, ValueError):
        ts = None
    extra = {}
    referral = m.get('referral') or {}
    if referral:  # el chat se abrió desde un anuncio de Meta ("clic para WhatsApp")
        extra['pauta'] = (referral.get('headline') or referral.get('source_id') or 'Anuncio Meta')[:200]
        extra['referral'] = {k: referral.get(k) for k in ('source_type', 'source_id', 'source_url', 'headline', 'ctwa_clid')}
    return MensajeEntrante(telefono=telefono, wa_id=m.get('id', ''), tipo=tipo, contenido=contenido,
                           nombre_perfil=perfiles.get(m.get('from'), ''), timestamp=ts, media_id=media_id,
                           media_mime=mime, media_filename=filename, extra=extra)
