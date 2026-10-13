import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime

import requests
from django.conf import settings

from core import tenancy

logger = logging.getLogger('apps.whatsapp')


class ErrorProveedor(Exception):
    """Error al hablar con el proveedor. `reintentable` indica si tiene sentido reintentar."""

    def __init__(self, mensaje, reintentable=True, status=None):
        super().__init__(mensaje)
        self.reintentable = reintentable
        self.status = status


@dataclass
class MensajeEntrante:
    telefono: str
    wa_id: str
    tipo: str = 'text'
    contenido: str = ''
    nombre_perfil: str = ''
    timestamp: datetime | None = None
    media_id: str = ''
    media_url: str = ''
    media_mime: str = ''
    media_filename: str = ''
    extra: dict = field(default_factory=dict)

    def to_dict(self):
        d = self.__dict__.copy()
        d['timestamp'] = self.timestamp.isoformat() if self.timestamp else None
        return d

    @classmethod
    def from_dict(cls, d):
        d = dict(d)
        if d.get('timestamp'):
            d['timestamp'] = datetime.fromisoformat(d['timestamp'])
        return cls(**d)


@dataclass
class ResultadoWebhook:
    mensajes: list = field(default_factory=list)          # [MensajeEntrante]
    estados: list = field(default_factory=list)           # [(wa_id, status, error)]
    conexion: str | None = None                           # nuevo estado de la línea, si vino
    qr: str | None = None                                 # QR (Evolution)


def mediatype_de_mime(mime: str) -> str:
    m = (mime or '').split(';')[0].strip().lower()
    if m.startswith('image/'):
        return 'image'
    if m.startswith('video/'):
        return 'video'
    if m.startswith('audio/'):
        return 'audio'
    return 'document'


_EXT = {
    'image/jpeg': '.jpg', 'image/png': '.png', 'image/gif': '.gif', 'image/webp': '.webp',
    'audio/ogg': '.ogg', 'audio/mpeg': '.mp3', 'audio/mp4': '.m4a', 'audio/aac': '.aac', 'audio/wav': '.wav',
    'video/mp4': '.mp4', 'video/3gpp': '.3gp', 'application/pdf': '.pdf',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document': '.docx',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': '.xlsx',
    'application/msword': '.doc', 'application/vnd.ms-excel': '.xls',
}


def extension(mime: str, filename: str = '') -> str:
    if filename and '.' in filename:
        return '.' + filename.rsplit('.', 1)[-1].lower()[:8]
    return _EXT.get((mime or '').split(';')[0].strip().lower(), '.bin')


def guardar_media(contenido: bytes, conv_pk: int, nombre_base: str, mime: str, filename: str = '') -> str:
    """Guarda un archivo recibido en la carpeta del cliente y devuelve su URL relativa (/media/...)."""
    seguro = ''.join(c for c in nombre_base if c.isalnum())[:40] or str(int(time.time()))
    nombre = f'{seguro}{extension(mime, filename)}'
    from core.storage import media_root
    carpeta = os.path.join(media_root(), 'whatsapp', f'conv_{conv_pk}')
    os.makedirs(carpeta, exist_ok=True)
    with open(os.path.join(carpeta, nombre), 'wb') as f:
        f.write(contenido)
    return f'{settings.MEDIA_URL}whatsapp/conv_{conv_pk}/{nombre}'


def url_publica(url: str) -> str:
    """
    Los proveedores descargan la media desde internet: /media/... → https://dominio/media/...
    Los adjuntos salientes llevan una firma con vencimiento (no son públicos permanentes).
    """
    if url.startswith('http'):
        return url
    completa = tenancy.url_sitio() + url
    ruta = url[len(settings.MEDIA_URL):] if url.startswith(settings.MEDIA_URL) else url.lstrip('/')
    if ruta.startswith('whatsapp/salientes/'):
        from urllib.parse import quote
        from core.media_firmada import firmar
        completa += f'?sig={quote(firmar(ruta))}'
    return completa


class ProveedorBase:
    nombre = ''

    def __init__(self, linea):
        self.linea = linea

    # ── Envío ────────────────────────────────────────────────────────────
    def enviar_texto(self, telefono: str, texto: str) -> str:
        raise NotImplementedError

    def enviar_media(self, telefono: str, url: str, mime: str, filename: str = '', caption: str = '') -> str:
        raise NotImplementedError

    def enviar_plantilla(self, telefono: str, plantilla, valores: list) -> str:
        """Por defecto (Evolution) se manda el texto renderizado."""
        return self.enviar_texto(telefono, plantilla.renderizar(valores))

    # ── Conexión ─────────────────────────────────────────────────────────
    def consultar_estado(self) -> tuple:
        """Devuelve (estado, detalle) con estados de LineaWhatsApp."""
        raise NotImplementedError

    # ── Webhooks ─────────────────────────────────────────────────────────
    def validar_webhook(self, request) -> bool:
        return True

    def parsear_webhook(self, request) -> ResultadoWebhook:
        raise NotImplementedError

    def descargar_media(self, msg: MensajeEntrante, conv_pk: int) -> tuple:
        """Devuelve (url_local, mime) o ('', '')."""
        return '', ''

    # ── Utilidades HTTP con log ───────────────────────────────────────────
    def _http(self, metodo, url, log_body=None, **kwargs):
        inicio = time.monotonic()
        resp, error = None, None
        try:
            resp = requests.request(metodo, url, timeout=kwargs.pop('timeout', 20), **kwargs)
            return resp
        except requests.RequestException as e:
            error = e
            raise ErrorProveedor(f'No se pudo conectar con {self.nombre}: {e}', reintentable=True) from e
        finally:
            self._log(url, metodo, log_body if log_body is not None else kwargs.get('json'), resp, error,
                      int((time.monotonic() - inicio) * 1000))

    def _log(self, url, metodo, body, resp, error, ms):
        from apps.whatsapp.models import LogAPIWhatsApp
        try:
            LogAPIWhatsApp.objects.create(
                linea=self.linea, endpoint=url[:300], metodo=metodo, status=resp.status_code if resp is not None else None,
                request=(json.dumps(body, default=str) if isinstance(body, (dict, list)) else str(body or ''))[:4000],
                response=(resp.text[:4000] if resp is not None else str(error or '')),
                exitoso=resp is not None and resp.status_code < 300, duracion_ms=ms,
            )
        except Exception:  # pragma: no cover
            pass

    @staticmethod
    def _chequear(resp, contexto=''):
        if resp.status_code < 300:
            return
        reintentable = resp.status_code >= 500 or resp.status_code == 429
        raise ErrorProveedor(f'{contexto} HTTP {resp.status_code}: {resp.text[:300]}', reintentable=reintentable,
                             status=resp.status_code)
