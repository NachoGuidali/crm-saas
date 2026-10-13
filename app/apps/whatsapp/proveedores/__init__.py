"""
Proveedores de WhatsApp intercambiables por línea. El resto del CRM usa siempre
`get_proveedor(linea)` y la misma interfaz (ver base.ProveedorBase), sin saber
si la línea es Evolution (QR), Meta Cloud API o Twilio.
"""
from .base import ErrorProveedor, MensajeEntrante, ProveedorBase  # noqa: F401


def get_proveedor(linea) -> ProveedorBase:
    from apps.whatsapp.models import LineaWhatsApp
    if linea.proveedor == LineaWhatsApp.PROV_META:
        from .meta import ProveedorMeta
        return ProveedorMeta(linea)
    if linea.proveedor == LineaWhatsApp.PROV_DEMO:
        from .demo import ProveedorDemo
        return ProveedorDemo(linea)
    if linea.proveedor == LineaWhatsApp.PROV_TWILIO:
        from .twilio import ProveedorTwilio
        return ProveedorTwilio(linea)
    from .evolution import ProveedorEvolution
    return ProveedorEvolution(linea)
