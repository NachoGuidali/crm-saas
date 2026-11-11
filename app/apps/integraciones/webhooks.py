"""
Webhooks salientes y eventos entrantes.

Saliente: una automatización (o la derivación del agente IA) manda los datos del lead a un WebhookDestino.
  Formato estándar (JSON completo) o una plantilla propia con variables {nombre}, {campo:slug}, etc.
  Reintenta si el otro sistema no responde y deja registro (EnvioWebhook + actividad en la ficha).

Entrante: cada envío incluye una URL de retorno firmada (por oportunidad). El sistema externo le hace POST con
  {"evento": "validacion_ok", "datos": {...}} y se disparan las automatizaciones "Cuando llega un evento externo".
"""
import json
import logging
import re

from django.core import signing
from django.utils import timezone

logger = logging.getLogger('apps.integraciones')

SAL_CALLBACK = 'evento-externo'
MAX_INTENTOS = 6


class ErrorPlantilla(Exception):
    pass


# ── URL de retorno ─────────────────────────────────────────────────────────

def token_evento(op):
    return signing.dumps({'op': op.pk}, salt=SAL_CALLBACK, compress=True)


def oportunidad_de_token(token):
    from apps.crm.models import Oportunidad
    try:
        datos = signing.loads(token, salt=SAL_CALLBACK)
    except signing.BadSignature:
        return None
    return Oportunidad.objects.select_related('contacto', 'embudo', 'etapa').filter(pk=datos.get('op')).first()


def url_evento(op):
    from core import tenancy
    return f'{tenancy.url_sitio()}/api/v1/eventos/{token_evento(op)}/'


# ── Datos que se mandan ────────────────────────────────────────────────────

def _valor(v):
    return v.isoformat() if hasattr(v, 'isoformat') else v


def variables(op):
    """Valores planos para las plantillas: {nombre}, {campo:slug}, {etapa}…"""
    from core import tenancy
    from apps.crm.models import CampoPersonalizado
    c = op.contacto
    base = tenancy.url_sitio()
    v = {
        'oportunidad_id': op.pk, 'contacto_id': c.pk, 'nombre': c.nombre, 'telefono': c.telefono, 'email': c.email,
        'dni': c.dni, 'fecha_nacimiento': _valor(c.fecha_nacimiento) or '', 'localidad': c.localidad,
        'provincia': c.provincia, 'embudo': str(op.embudo), 'etapa': str(op.etapa),
        'agente': op.agente.get_full_name() or op.agente.username if op.agente_id else '',
        'agente_email': op.agente.email if op.agente_id else '', 'origen': op.get_origen_display(), 'fuente': op.fuente,
        'campana': str(op.pauta) if op.pauta_id else '', 'link_ficha': f'{base}{op.get_absolute_url()}',
        'callback_url': url_evento(op), 'fecha': timezone.localtime().isoformat(),
    }
    extra = c.datos_extra or {}
    for campo in CampoPersonalizado.objects.filter(activo=True):
        v[f'campo:{campo.slug}'] = _valor(extra.get(campo.slug, ''))
    return v


def payload_estandar(op, evento='', origen=''):
    from core import tenancy
    from apps.crm.models import Actividad, CampoPersonalizado
    c = op.contacto
    extra = c.datos_extra or {}
    campos = {cp.slug: {'etiqueta': cp.nombre, 'valor': _valor(extra.get(cp.slug))}
              for cp in CampoPersonalizado.objects.filter(activo=True) if cp.slug in extra}
    resumen_ia = (Actividad.objects.filter(oportunidad=op, texto__startswith='Agente IA derivó')
                  .order_by('-created_at').values_list('texto', flat=True).first() or '')
    base = tenancy.url_sitio()
    return {
        'evento': evento or 'automatizacion', 'origen': origen, 'fecha': timezone.localtime().isoformat(),
        'contacto': {'id': c.pk, 'nombre': c.nombre, 'telefono': c.telefono, 'email': c.email, 'dni': c.dni,
                     'fecha_nacimiento': _valor(c.fecha_nacimiento), 'localidad': c.localidad, 'provincia': c.provincia},
        'campos': campos,
        'oportunidad': {'id': op.pk, 'embudo': str(op.embudo), 'etapa': str(op.etapa), 'estado': op.estado,
                        'agente': (op.agente.get_full_name() or op.agente.username) if op.agente_id else '',
                        'origen': op.get_origen_display(), 'fuente': op.fuente,
                        'campana': str(op.pauta) if op.pauta_id else '', 'creada': _valor(op.created_at)},
        'agente_ia_resumen': resumen_ia,
        'links': {'ficha': f'{base}{op.get_absolute_url()}'},
        # El sistema externo avisa el resultado acá (POST {"evento": "...", "datos": {...}}): sin API key.
        'callback': {'url': url_evento(op), 'ejemplo': {'evento': 'validacion_ok', 'datos': {'nota': '...'}}},
    }


_VAR = re.compile(r'\{([a-z_]+(?::[a-z0-9_\-]+)?)\}')


def render_plantilla(plantilla, valores):
    """Reemplaza {variable} (escapada para ir dentro de un string JSON) y valida que quede un JSON."""
    def repl(m):
        clave = m.group(1)
        if clave not in valores:
            return m.group(0)
        v = valores[clave]
        return json.dumps('' if v is None else str(v), ensure_ascii=False)[1:-1]
    texto = _VAR.sub(repl, plantilla)
    try:
        return json.loads(texto)
    except ValueError as e:
        raise ErrorPlantilla(f'La plantilla no es un JSON válido: {e}')


def armar_payload(op, plantilla='', evento='', origen=''):
    if plantilla.strip():
        return render_plantilla(plantilla, variables(op))
    return payload_estandar(op, evento, origen)


# ── Envío ──────────────────────────────────────────────────────────────────

def encolar(destino, payload, oportunidad=None, origen=''):
    """Crea el registro y lo manda en segundo plano (con reintentos)."""
    from django.db import transaction
    from .models import EnvioWebhook
    from .tasks import enviar_webhook
    envio = EnvioWebhook.objects.create(destino=destino, oportunidad=oportunidad, origen=origen[:150], payload=payload)
    pk = envio.pk
    transaction.on_commit(lambda: enviar_webhook.delay(pk))
    return envio


def enviar_ahora(envio):
    """Un intento. Devuelve True si el destino respondió 2xx."""
    import requests
    d = envio.destino
    envio.intentos += 1
    try:
        resp = requests.post(d.url, json=envio.payload, headers=d.headers(), timeout=20)
        envio.respuesta_status, envio.respuesta = resp.status_code, resp.text[:2000]
        ok = 200 <= resp.status_code < 300
    except requests.RequestException as e:
        envio.respuesta_status, envio.respuesta, ok = None, str(e)[:2000], False
    if ok:
        envio.estado, envio.enviado_at = envio.OK, timezone.now()
    envio.save(update_fields=['intentos', 'respuesta_status', 'respuesta', 'estado', 'enviado_at'])
    return ok


def fallo_definitivo(envio):
    from django.db.models import Q
    from apps.users.models import User
    from apps.users.services import notificar
    envio.estado = envio.ERROR
    envio.save(update_fields=['estado'])
    for u in User.objects.filter(is_active=True).filter(Q(rol=User.ROL_ADMIN) | Q(is_superuser=True)):
        notificar(u, 'sistema', f'No se pudo enviar a "{envio.destino}"',
                  f'{envio.intentos} intentos. Última respuesta: {envio.respuesta_status or "sin respuesta"}',
                  '/integraciones/webhooks/')


def registrar_en_ficha(envio):
    from apps.crm.models import Actividad
    op = envio.oportunidad
    if op is None:
        return
    estado = 'enviado' if envio.estado == envio.OK else 'FALLÓ'
    Actividad.objects.create(contacto_id=op.contacto_id, oportunidad=op, tipo=Actividad.TIPO_SISTEMA,
                             texto=f'Datos {estado} a "{envio.destino}" ({envio.origen or "webhook"}) · '
                                   f'HTTP {envio.respuesta_status or "—"}')


# ── Evento entrante ────────────────────────────────────────────────────────

def recibir_evento(op, nombre, datos=None):
    """
    El sistema externo avisó algo (ej. "validacion_ok"). Queda en la ficha, se guardan los datos que coincidan con
    campos personalizados y se disparan las automatizaciones "Cuando llega un evento externo" con ese nombre.
    Devuelve cuántas automatizaciones se programaron.
    """
    from apps.automatizaciones.services import evento_externo
    from apps.crm.models import Actividad, CampoPersonalizado
    nombre = slug_evento(nombre)
    datos = datos if isinstance(datos, dict) else {}
    slugs = set(CampoPersonalizado.objects.filter(activo=True).values_list('slug', flat=True))
    campos = {k: v for k, v in datos.items() if k in slugs and isinstance(v, (str, int, float, bool))}
    if campos:
        c = op.contacto
        c.datos_extra = {**(c.datos_extra or {}), **campos}
        c.save(update_fields=['datos_extra', 'updated_at'])
    resumen = ', '.join(f'{k}: {v}' for k, v in list(datos.items())[:8])
    Actividad.objects.create(contacto_id=op.contacto_id, oportunidad=op, tipo=Actividad.TIPO_SISTEMA,
                             texto=f'Evento externo "{nombre}"' + (f' · {resumen}' if resumen else ''))
    return evento_externo(op, nombre)


def slug_evento(nombre):
    return re.sub(r'[^a-z0-9_]+', '_', str(nombre or '').strip().lower()).strip('_')[:60]
