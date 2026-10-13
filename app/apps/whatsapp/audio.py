"""Notas de voz grabadas en el navegador → OGG/Opus, el formato de las notas de voz de WhatsApp (Meta, Twilio, Evolution)."""
import shutil
import subprocess
import tempfile


class ErrorAudio(Exception):
    pass


def a_ogg_opus(datos: bytes) -> bytes:
    """Convierte (o reempaqueta) un audio a OGG/Opus mono. Chrome graba en WebM/Opus, Firefox en OGG/Opus, Safari en MP4/AAC."""
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        raise ErrorAudio('El servidor no tiene ffmpeg instalado: no se pueden convertir notas de voz.')
    with tempfile.NamedTemporaryFile(suffix='.in') as entrada, tempfile.NamedTemporaryFile(suffix='.ogg') as salida:
        entrada.write(datos)
        entrada.flush()
        for args in (['-c:a', 'copy'], ['-c:a', 'libopus', '-b:a', '32k', '-ac', '1', '-ar', '48000']):
            r = subprocess.run([ffmpeg, '-y', '-loglevel', 'error', '-i', entrada.name, '-vn', '-map_metadata', '-1',
                                *args, '-f', 'ogg', salida.name], capture_output=True, timeout=60)
            if r.returncode == 0:
                salida.seek(0)
                contenido = salida.read()
                if contenido:
                    return contenido
        raise ErrorAudio('No se pudo convertir la nota de voz.')
