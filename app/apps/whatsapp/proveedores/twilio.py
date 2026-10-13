"""Proveedor Twilio (WhatsApp oficial vía BSP)."""
import json
import logging

from core import tenancy
from core.phone import normalizar_telefono

from .base import ErrorProveedor, MensajeEntrante, ProveedorBase, ResultadoWebhook, guardar_media, mediatype_de_mime, url_publica

logger = logging.getLogger('apps.whatsapp')
API = 'https://api.twilio.com/2010-04-01'
_ESTADOS = {'sent': 'sent', 'delivered': 'delivered', 'read': 'read', 'failed': 'failed', 'undelivered': 'failed'}


def _wa(telefono):
    t = (telefono or '').strip()
    if t.startswith('whatsapp:'):
        return t
    return 'whatsapp:' + (t if t.startswith('+') else '+' + t)


class ProveedorTwilio(ProveedorBase):
    nombre = 'Twilio'

    def _auth(self):
        return (self.linea.twilio_account_sid, self.linea.twilio_auth_token)

    def _crear(self, data):
        data = {'From': _wa(self.linea.twilio_from), 'StatusCallback': self.linea.webhook_url, **data}
        resp = self._http('POST', f'{API}/Accounts/{self.linea.twilio_account_sid}/Messages.json', data=data,
                          auth=self._auth(), log_body=data)
        if resp.status_code >= 300:
            try:
                err = resp.json()
                detalle = f'{err.get("code")} {err.get("message")}'
            except ValueError:
                detalle = resp.text[:300]
            raise ErrorProveedor(f'Twilio: {detalle}', reintentable=resp.status_code >= 500, status=resp.status_code)
        return resp.json().get('sid', '')

    def enviar_texto(self, telefono, texto):
        return self._crear({'To': _wa(telefono), 'Body': texto})

    def enviar_media(self, telefono, url, mime, filename='', caption=''):
        data = {'To': _wa(telefono), 'MediaUrl': url_publica(url)}
        if caption:
            data['Body'] = caption
        return self._crear(data)

    def enviar_plantilla(self, telefono, plantilla, valores):
        if not plantilla.twilio_content_sid:
            raise ErrorProveedor(f'La plantilla "{plantilla}" no tiene ContentSid de Twilio.', reintentable=False)
        data = {'To': _wa(telefono), 'ContentSid': plantilla.twilio_content_sid}
        if valores:
            data['ContentVariables'] = json.dumps({str(i + 1): str(v) for i, v in enumerate(valores)})
        return self._crear(data)

    def consultar_estado(self):
        from apps.whatsapp.models import LineaWhatsApp as L
        if not all(self._auth()) or not self.linea.twilio_from:
            return L.ESTADO_DESCONECTADA, 'Faltan credenciales de Twilio.'
        try:
            resp = self._http('GET', f'{API}/Accounts/{self.linea.twilio_account_sid}.json', auth=self._auth(), timeout=10)
        except ErrorProveedor as e:
            return L.ESTADO_ERROR, str(e)[:300]
        if not resp.ok:
            return L.ESTADO_ERROR, f'Credenciales inválidas (HTTP {resp.status_code})'
        return L.ESTADO_CONECTADA, f'Cuenta {resp.json().get("friendly_name", "")} ({resp.json().get("status", "")})'

    def validar_webhook(self, request):
        token = self.linea.twilio_auth_token
        if not token:
            return True
        firma = request.headers.get('X-Twilio-Signature', '')
        try:
            from twilio.request_validator import RequestValidator
        except ImportError:  # pragma: no cover
            return True
        url = tenancy.url_sitio() + request.get_full_path()
        return RequestValidator(token).validate(url, request.POST.dict(), firma)

    def parsear_webhook(self, request):
        post = request.POST
        res = ResultadoWebhook()
        estado = (post.get('MessageStatus') or post.get('SmsStatus') or '').lower()
        num_media = int(post.get('NumMedia') or 0)
        es_entrante = post.get('Body') is not None and estado in ('', 'received', 'receiving') or num_media
        if not es_entrante:
            sid = post.get('MessageSid') or post.get('SmsSid')
            if sid and _ESTADOS.get(estado):
                res.estados.append((sid, _ESTADOS[estado], post.get('ErrorMessage') or post.get('ErrorCode') or ''))
            return res
        telefono = normalizar_telefono(post.get('From', ''))
        if not telefono:
            return res
        mime = post.get('MediaContentType0', '') if num_media else ''
        tipo = mediatype_de_mime(mime) if mime else 'text'
        contenido = post.get('Body', '') or (f'[{tipo.capitalize()}]' if mime else '')
        extra = {}
        if post.get('ReferralSourceId') or post.get('ReferralHeadline'):  # chat abierto desde un anuncio de Meta
            extra['pauta'] = (post.get('ReferralHeadline') or post.get('ReferralSourceId') or 'Anuncio Meta')[:200]
            extra['referral'] = {'source_type': post.get('ReferralSourceType', ''),
                                 'source_id': post.get('ReferralSourceId', ''),
                                 'source_url': post.get('ReferralSourceUrl', ''),
                                 'headline': post.get('ReferralHeadline', ''),
                                 'ctwa_clid': post.get('ReferralCtwaClid', '')}
        res.mensajes.append(MensajeEntrante(
            telefono=telefono, wa_id=post.get('MessageSid', ''), tipo=tipo, contenido=contenido,
            nombre_perfil=post.get('ProfileName', ''), media_url=post.get('MediaUrl0', '') if num_media else '',
            media_mime=mime, extra=extra,
        ))
        return res

    def descargar_media(self, msg, conv_pk):
        if not msg.media_url:
            return '', ''
        try:
            resp = self._http('GET', msg.media_url, auth=self._auth(), timeout=60, log_body='<download>',
                              allow_redirects=True)
        except ErrorProveedor:
            return '', ''
        if not resp.ok:
            return '', ''
        mime = resp.headers.get('Content-Type', msg.media_mime) or msg.media_mime
        return guardar_media(resp.content, conv_pk, msg.wa_id, mime), mime
