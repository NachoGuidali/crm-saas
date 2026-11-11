"""Notas de voz → texto con la API de transcripción de OpenAI (modelo gpt-transcribe)."""
import logging
import os
import shutil
import subprocess
import tempfile

import requests
from django.conf import settings

logger = logging.getLogger('apps.agente')

URL = 'https://api.openai.com/v1/audio/transcriptions'
MAX_BYTES = 25 * 1024 * 1024
# gpt-transcribe no acepta OGG/Opus (el formato de WhatsApp): se pasa a mp3 antes.
FORMATOS_OK = ('.mp3', '.mp4', '.mpeg', '.mpga', '.m4a', '.wav', '.webm')


class ErrorTranscripcion(Exception):
    pass


def disponible():
    return bool(getattr(settings, 'OPENAI_API_KEY', ''))


def _a_mp3(ruta):
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        raise ErrorTranscripcion('El servidor no tiene ffmpeg para convertir el audio.')
    salida = tempfile.NamedTemporaryFile(suffix='.mp3', delete=False).name
    r = subprocess.run([ffmpeg, '-y', '-loglevel', 'error', '-i', ruta, '-vn', '-ac', '1', '-ar', '16000',
                        '-b:a', '48k', salida], capture_output=True, timeout=120)
    if r.returncode != 0:
        os.unlink(salida)
        raise ErrorTranscripcion(f'No se pudo convertir el audio: {r.stderr.decode(errors="replace")[:200]}')
    return salida


def transcribir(ruta_local):
    """Texto del audio o ErrorTranscripcion."""
    if not disponible():
        raise ErrorTranscripcion('Falta OPENAI_API_KEY en el .env.')
    convertido = None
    try:
        ruta = ruta_local
        if not ruta.lower().endswith(FORMATOS_OK):
            ruta = convertido = _a_mp3(ruta_local)
        if os.path.getsize(ruta) > MAX_BYTES:
            raise ErrorTranscripcion('El audio supera los 25 MB.')
        with open(ruta, 'rb') as f:
            try:
                resp = requests.post(
                    URL, headers={'Authorization': f'Bearer {settings.OPENAI_API_KEY}'}, timeout=120,
                    files={'file': (os.path.basename(ruta), f)},
                    data={'model': settings.OPENAI_TRANSCRIBE_MODEL, 'languages[]': 'es'},
                )
            except requests.RequestException as e:
                raise ErrorTranscripcion(f'No se pudo conectar con OpenAI: {e}')
        if resp.status_code != 200:
            raise ErrorTranscripcion(f'OpenAI respondió {resp.status_code}: {resp.text[:200]}')
        return (resp.json().get('text') or '').strip()
    finally:
        if convertido and os.path.exists(convertido):
            os.unlink(convertido)


def ruta_de_media(url):
    """/media/<schema>/... → ruta en disco (o None)."""
    from apps.whatsapp.proveedores.evolution import _ruta_local
    return _ruta_local(url or '')
