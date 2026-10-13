"""
Campos personalizados de tipo archivo: el valor en Contacto.datos_extra[slug] es una lista de
{"url", "nombre", "fecha", "por"}. Se suben desde la ficha o se guardan desde un adjunto de WhatsApp.
"""
import os
import secrets

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.utils import timezone
from django.utils.text import get_valid_filename

from .models import Actividad, CampoPersonalizado

TAMANIO_MAXIMO = 20 * 1024 * 1024  # 20 MB


class ErrorArchivo(Exception):
    pass


def campos_archivo(embudo=None):
    return [c for c in CampoPersonalizado.activos(embudo) if c.es_archivo]


def archivos_de(contacto, campo):
    valor = (contacto.datos_extra or {}).get(campo.slug)
    return valor if isinstance(valor, list) else []


def adjuntar(contacto, campo, contenido: bytes, nombre, usuario=None, oportunidad=None, origen=''):
    """Guarda el archivo y lo agrega al campo del contacto. Devuelve el dict guardado."""
    if not campo.es_archivo:
        raise ErrorArchivo('Ese campo no es de tipo archivo.')
    if not contenido:
        raise ErrorArchivo('El archivo está vacío.')
    if len(contenido) > TAMANIO_MAXIMO:
        raise ErrorArchivo('El archivo supera los 20 MB.')
    nombre = get_valid_filename(os.path.basename(nombre or 'archivo')) or 'archivo'
    ruta = default_storage.save(f'documentos/{contacto.pk}/{secrets.token_hex(6)}_{nombre}', ContentFile(contenido))
    item = {'url': default_storage.url(ruta), 'ruta': ruta, 'nombre': nombre,
            'fecha': timezone.localtime().strftime('%Y-%m-%d %H:%M'),
            'por': usuario.display_name if usuario else '', 'origen': origen}
    contacto.refresh_from_db(fields=['datos_extra'])
    extra = dict(contacto.datos_extra or {})
    extra[campo.slug] = archivos_de(contacto, campo) + [item]
    contacto.datos_extra = extra
    contacto.save(update_fields=['datos_extra', 'updated_at'])
    Actividad.objects.create(contacto=contacto, oportunidad=oportunidad, tipo=Actividad.TIPO_CAMBIO, usuario=usuario,
                             texto=f'Archivo agregado a «{campo.nombre}»: {nombre}' + (f' ({origen})' if origen else ''))
    return item


def quitar(contacto, campo, indice, usuario=None, oportunidad=None):
    lista = archivos_de(contacto, campo)
    if not 0 <= indice < len(lista):
        raise ErrorArchivo('Archivo inexistente.')
    item = lista.pop(indice)
    extra = dict(contacto.datos_extra or {})
    if lista:
        extra[campo.slug] = lista
    else:
        extra.pop(campo.slug, None)
    contacto.datos_extra = extra
    contacto.save(update_fields=['datos_extra', 'updated_at'])
    if item.get('ruta'):
        default_storage.delete(item['ruta'])
    Actividad.objects.create(contacto=contacto, oportunidad=oportunidad, tipo=Actividad.TIPO_CAMBIO, usuario=usuario,
                             texto=f'Archivo quitado de «{campo.nombre}»: {item.get("nombre", "")}')
    return item


def leer_media_local(url):
    """Contenido de un adjunto de WhatsApp ya descargado al servidor (media_url = /media/...)."""
    from django.conf import settings
    prefijo = settings.MEDIA_URL
    if not url or not url.startswith(prefijo):
        raise ErrorArchivo('El adjunto no está disponible en el servidor.')
    ruta = url[len(prefijo):]
    if not default_storage.exists(ruta):
        raise ErrorArchivo('El adjunto ya no está en el servidor.')
    with default_storage.open(ruta, 'rb') as f:
        return f.read()
