"""
Archivos separados por cliente: cada uno en MEDIA_ROOT/<schema>/ (grabaciones, adjuntos, importaciones).
La URL no cambia (/media/<ruta>): la sirve core.views.media_protegida buscando en la carpeta del cliente
del subdominio, así un cliente no puede pedir archivos de otro aunque adivine la ruta.
"""
import os

from django.conf import settings
from django.core.files.storage import FileSystemStorage

from core.tenancy import schema_actual


def media_root():
    """Carpeta de archivos del cliente actual."""
    return os.path.join(settings.MEDIA_ROOT, schema_actual())


class ArchivosPorCliente(FileSystemStorage):
    @property
    def base_location(self):
        return media_root()

    @property
    def location(self):
        return os.path.abspath(self.base_location)

    @property
    def base_url(self):
        return settings.MEDIA_URL
