"""Lógica de las firmas electrónicas: crear la solicitud, enviarla, recibir la firma, PDF y automatizaciones."""
import base64
import hashlib
import json
import logging
import re
from datetime import date
from io import BytesIO

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.validators import validate_email
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from core.tenancy import tiene_funcion

from .models import FUNCION, FormularioFirma as F, SolicitudFirma

logger = logging.getLogger('apps.firmas')

# Datos del lead con los que se puede precargar un campo (además de los personalizados: "cp:<slug>").
PRELLENAR_BASE = [
    ('nombre', 'Nombre y apellido'), ('dni', 'DNI'), ('email', 'Email'), ('telefono', 'Teléfono'),
    ('fecha_nacimiento', 'Fecha de nacimiento'), ('localidad', 'Localidad'), ('provincia', 'Provincia'),
]
MAX_FIRMA_BYTES = 1024 * 1024


class ErrorFirma(Exception):
    pass


def habilitado():
    return tiene_funcion(FUNCION)


def opciones_prellenar():
    from apps.crm.models import CampoPersonalizado
    return PRELLENAR_BASE + [(f'cp:{c.slug}', f'★ {c.nombre}') for c in CampoPersonalizado.activos()
                             if not c.es_archivo]


def contexto(contacto, op, extra=None):
    from apps.whatsapp.models import contexto_variables
    ctx = contexto_variables(contacto, op)
    ctx.update({
        'dni': contacto.dni or '', 'localidad': contacto.localidad or '', 'provincia': contacto.provincia or '',
        'fecha_nacimiento': contacto.fecha_nacimiento.strftime('%d/%m/%Y') if contacto.fecha_nacimiento else '',
        'hoy': timezone.localdate().strftime('%d/%m/%Y'),
    })
    ctx.update(extra or {})
    return ctx


def reemplazar(texto, ctx):
    return re.sub(r'\{(\w+)\}', lambda m: str(ctx.get(m.group(1), m.group(0))), texto or '')


def _valor_inicial(contacto, clave):
    if not clave:
        return ''
    if clave.startswith('cp:'):
        v = (contacto.datos_extra or {}).get(clave[3:])
        return '' if v in (None, []) else v
    v = getattr(contacto, clave, '')
    if isinstance(v, date):
        return v.isoformat()
    return v or ''


def normalizar_campos(campos):
    """Limpia lo que manda el editor: ids únicos, tipos válidos, opciones como lista."""
    validos = {t for t, _ in F.TIPOS_CAMPO}
    limpios, ids = [], set()
    for i, c in enumerate(campos or []):
        if not isinstance(c, dict) or c.get('tipo') not in validos:
            continue
        etiqueta = str(c.get('etiqueta') or '').strip()[:300]
        if not etiqueta and c['tipo'] != F.T_FIRMA:
            continue
        cid = re.sub(r'[^a-z0-9_]', '', str(c.get('id') or '').lower())[:30] or f'c{i + 1}'
        while cid in ids:
            cid += 'x'
        ids.add(cid)
        opciones = c.get('opciones') or []
        if isinstance(opciones, str):
            opciones = [o.strip() for o in opciones.split(',')]
        limpios.append({
            'id': cid, 'tipo': c['tipo'], 'etiqueta': etiqueta or 'Firma',
            'requerido': bool(c.get('requerido')) or c['tipo'] in (F.T_FIRMA, F.T_ACEPTO),
            'opciones': [str(o)[:120] for o in opciones if str(o).strip()][:50] if c['tipo'] == F.T_LISTA else [],
            'ayuda': str(c.get('ayuda') or '')[:300],
            'prellenar': str(c.get('prellenar') or '')[:80] if c['tipo'] not in (F.T_FIRMA, F.T_ACEPTO, F.T_TITULO) else '',
        })
    return limpios


# ── Crear y enviar ─────────────────────────────────────────────────────────

def crear_solicitud(formulario, op, usuario=None, origen='manual', accion=None):
    if not habilitado():
        raise ErrorFirma('El módulo de firmas no está habilitado.')
    if not formulario.activo:
        raise ErrorFirma('El formulario está desactivado.')
    if not formulario.campos_firma:
        raise ErrorFirma('El formulario no tiene ningún campo de firma.')
    from apps.crm.models import Actividad
    contacto = op.contacto
    ctx = contexto(contacto, op, {'formulario': formulario.titulo})
    snapshot = {
        'titulo': reemplazar(formulario.titulo, ctx), 'documento': reemplazar(formulario.documento, ctx),
        'campos': formulario.campos, 'logo': formulario.logo.name if formulario.logo else '',
        'color_fondo': formulario.color_fondo, 'color_principal': formulario.color_principal,
        'texto_final': formulario.texto_final,
        'iniciales': {c['id']: _valor_inicial(contacto, c.get('prellenar')) for c in formulario.campos
                      if c.get('prellenar')},
    }
    sol = SolicitudFirma.objects.create(formulario=formulario, oportunidad=op, contacto=contacto, snapshot=snapshot,
                                        origen=origen, creada_por=usuario, accion=accion)
    Actividad.objects.create(contacto=contacto, oportunidad=op, tipo=Actividad.TIPO_SISTEMA, usuario=usuario,
                             texto=f'Formulario para firmar generado: {formulario.nombre}')
    return sol


def _textos(sol):
    ctx = contexto(sol.contacto, sol.oportunidad, {'formulario': sol.snapshot.get('titulo', ''), 'link': sol.url})
    f = sol.formulario
    return reemplazar(f.asunto_email, ctx), reemplazar(f.texto_email, ctx), reemplazar(f.texto_whatsapp, ctx)


def enviar_email(sol, usuario=None):
    from apps.automatizaciones.services import enviar_email as enviar
    c = sol.contacto
    if not c.email:
        raise ErrorFirma('El contacto no tiene email.')
    if c.no_contactar:
        raise ErrorFirma('El contacto pidió no ser contactado.')
    asunto, cuerpo, _ = _textos(sol)
    enviar(c, sol.oportunidad, asunto, cuerpo, usuario=usuario, accion=sol.accion)
    SolicitudFirma.objects.filter(pk=sol.pk).update(enviada_email=True)
    return f'Email enviado a {c.email}'


def enviar_whatsapp(sol, usuario=None):
    from apps.whatsapp.services import (ErrorEnvio, conversacion_para_contacto, enviar_automatico, enviar_mensaje,
                                        linea_para)
    _, _, texto = _textos(sol)
    op = sol.oportunidad
    if usuario is None:
        msg, detalle = enviar_automatico(op, texto=texto, accion=sol.accion)
        if msg is None:
            raise ErrorFirma(detalle)
    else:
        if not op.contacto.puede_recibir_mensajes:
            raise ErrorFirma('El contacto no acepta mensajes o no tiene teléfono válido.')
        linea = linea_para(op.contacto, op, user=usuario)
        if linea is None:
            raise ErrorFirma('No hay ninguna línea de WhatsApp activa.')
        try:
            enviar_mensaje(conversacion_para_contacto(op.contacto, linea, agente=op.agente), usuario=usuario, texto=texto)
        except ErrorEnvio as e:
            raise ErrorFirma(str(e))
        detalle = f'WhatsApp enviado por {linea}'
    SolicitudFirma.objects.filter(pk=sol.pk).update(enviada_whatsapp=True)
    return detalle


def enviar(sol, canales, usuario=None):
    """Envía por los canales pedidos ('email', 'whatsapp'). Devuelve (enviados, errores) como textos."""
    enviados, errores = [], []
    for canal, fn in (('email', enviar_email), ('whatsapp', enviar_whatsapp)):
        if canal in canales:
            try:
                enviados.append(fn(sol, usuario))
            except ErrorFirma as e:
                errores.append(f'{canal.capitalize()}: {e}')
            except Exception as e:  # SMTP caído, etc.: no rompe el otro canal
                logger.exception('Error enviando firma %s por %s', sol.pk, canal)
                errores.append(f'{canal.capitalize()}: {e}')
    return enviados, errores


# ── Completar (lado del lead) ──────────────────────────────────────────────

def marcar_abierta(sol):
    if sol.estado == SolicitudFirma.ESTADO_PENDIENTE:
        SolicitudFirma.objects.filter(pk=sol.pk, estado=SolicitudFirma.ESTADO_PENDIENTE).update(
            estado=SolicitudFirma.ESTADO_ABIERTA, abierta_at=timezone.now())


def _png_de_data_url(data_url):
    m = re.match(r'^data:image/png;base64,([A-Za-z0-9+/=]+)$', (data_url or '').strip())
    if not m:
        return None
    try:
        crudo = base64.b64decode(m.group(1), validate=True)
    except ValueError:
        return None
    if not crudo or len(crudo) > MAX_FIRMA_BYTES:
        return None
    from PIL import Image
    try:
        img = Image.open(BytesIO(crudo))
        img.load()
    except Exception:
        return None
    # Firma en blanco: sin ningún trazo
    alfa = img.convert('RGBA').getchannel('A')
    if alfa.getbbox() is None:
        return None
    return crudo


def validar(sol, datos):
    """Valida lo que mandó el lead. Devuelve (respuestas, firmas_png, errores {id: texto})."""
    respuestas, firmas, errores = {}, {}, {}
    for c in sol.snapshot.get('campos', []):
        cid, tipo = c['id'], c['tipo']
        if tipo == F.T_TITULO:
            continue
        if tipo == F.T_FIRMA:
            png = _png_de_data_url(datos.get(f'f_{cid}'))
            if png is None:
                errores[cid] = 'Falta la firma.'
            else:
                firmas[cid] = png
            continue
        valor = (datos.get(f'f_{cid}') or '').strip()
        if tipo in (F.T_ACEPTO, F.T_SINO):
            marcado = valor in ('1', 'true', 'on', 'si')
            if tipo == F.T_ACEPTO and not marcado:
                errores[cid] = 'Tenés que aceptar para continuar.'
            elif tipo == F.T_SINO and valor not in ('true', 'false') and c.get('requerido'):
                errores[cid] = 'Elegí Sí o No.'
            respuestas[cid] = marcado if tipo == F.T_ACEPTO else ({'true': True, 'false': False}.get(valor))
            continue
        if not valor:
            if c.get('requerido'):
                errores[cid] = 'Este dato es obligatorio.'
            respuestas[cid] = ''
            continue
        valor = valor[:2000]
        if tipo == F.T_NUMERO:
            try:
                float(valor.replace(',', '.'))
            except ValueError:
                errores[cid] = 'Ingresá un número.'
        elif tipo == F.T_FECHA:
            try:
                date.fromisoformat(valor)
            except ValueError:
                errores[cid] = 'Fecha inválida.'
        elif tipo == F.T_EMAIL:
            try:
                validate_email(valor)
            except ValidationError:
                errores[cid] = 'Email inválido.'
        elif tipo == F.T_LISTA and valor not in c.get('opciones', []):
            errores[cid] = 'Elegí una opción de la lista.'
        respuestas[cid] = valor
    return respuestas, firmas, errores


def _huella(sol, respuestas, firmas):
    contenido = {
        'solicitud': sol.pk, 'token': sol.token, 'snapshot': sol.snapshot, 'respuestas': respuestas,
        'firmas': {k: hashlib.sha256(v).hexdigest() for k, v in sorted(firmas.items())},
    }
    return hashlib.sha256(json.dumps(contenido, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def completar(sol, datos, ip=None, dispositivo=''):
    """El lead envió el formulario. Devuelve la solicitud firmada o lanza ErrorFirma({id: error})."""
    from . import pdf as generador
    respuestas, firmas, errores = validar(sol, datos)
    if errores:
        raise ErrorFirma(errores)
    with transaction.atomic():
        sol = SolicitudFirma.objects.select_for_update().select_related('contacto', 'oportunidad').get(pk=sol.pk)
        if not sol.firmable:
            raise ErrorFirma({'_': 'Este formulario ya no se puede firmar.'})
        rutas = {cid: default_storage.save(f'firmas/{sol.pk}/firma_{cid}.png', ContentFile(png))
                 for cid, png in firmas.items()}
        sol.respuestas, sol.firmas = respuestas, rutas
        sol.ip, sol.dispositivo = ip or None, (dispositivo or '')[:300]
        sol.firmada_at = timezone.now()
        sol.estado = SolicitudFirma.ESTADO_FIRMADA
        sol.huella = _huella(sol, respuestas, firmas)
        contenido_pdf = generador.generar(sol)
        sol.pdf = default_storage.save(f'firmas/{sol.pk}/{_nombre_pdf(sol)}', ContentFile(contenido_pdf))
        sol.huella_pdf = hashlib.sha256(contenido_pdf).hexdigest()
        sol.save()
        transaction.on_commit(lambda: despues_de_firmar(sol.pk))
    return sol


def _nombre_pdf(sol):
    from django.utils.text import slugify
    return f'{slugify(sol.formulario.nombre)[:40] or "formulario"}_{slugify(sol.contacto.nombre)[:40] or sol.pk}.pdf'


def despues_de_firmar(sol_id):
    """Ficha, aviso al agente, copia al lead y automatizaciones "Cuando se recibe un formulario firmado"."""
    from apps.crm.models import Actividad
    from apps.crm.services import tocar
    from apps.users.services import notificar
    sol = SolicitudFirma.objects.select_related('formulario', 'contacto', 'oportunidad__agente').get(pk=sol_id)
    op = sol.oportunidad
    Actividad.objects.create(contacto=sol.contacto, oportunidad=op, tipo=Actividad.TIPO_SISTEMA,
                             texto=f'✍️ Firmó «{sol.formulario.nombre}». PDF disponible en la ficha (Firmas).')
    try:
        tocar(op)
    except Exception:  # pragma: no cover
        logger.exception('No se pudo actualizar la actividad de la oportunidad %s', op.pk)
    notificar(op.agente, 'sistema', f'{sol.contacto.nombre} firmó «{sol.formulario.nombre}»',
              'El PDF quedó en la ficha.', op.get_absolute_url())
    if sol.formulario.enviar_copia and sol.contacto.email and not sol.contacto.no_email:
        try:
            enviar_copia(sol)
        except Exception:
            logger.exception('No se pudo enviar la copia del PDF de la firma %s', sol.pk)
    from apps.automatizaciones.services import formulario_firmado
    formulario_firmado(sol)


def enviar_copia(sol):
    from django.core.mail import EmailMessage
    from apps.automatizaciones.services import conexion_email
    con, remitente = conexion_email()
    titulo = sol.snapshot.get('titulo') or sol.formulario.nombre
    mail = EmailMessage(f'Tu copia firmada: {titulo}',
                        f'Hola {sol.contacto.nombre.split()[0] if sol.contacto.nombre else ""}, te adjuntamos la copia '
                        f'del formulario «{titulo}» que firmaste el '
                        f'{timezone.localtime(sol.firmada_at):%d/%m/%Y a las %H:%M}.\n\nGracias.',
                        remitente, [sol.contacto.email], connection=con)
    with default_storage.open(sol.pdf, 'rb') as f:
        mail.attach(sol.pdf.rsplit('/', 1)[-1], f.read(), 'application/pdf')
    mail.send(fail_silently=False)


def anular(sol, usuario=None):
    from apps.crm.models import Actividad
    if sol.estado == SolicitudFirma.ESTADO_FIRMADA:
        raise ErrorFirma('No se puede anular un formulario ya firmado.')
    SolicitudFirma.objects.filter(pk=sol.pk).update(estado=SolicitudFirma.ESTADO_ANULADA)
    Actividad.objects.create(contacto=sol.contacto, oportunidad=sol.oportunidad, tipo=Actividad.TIPO_SISTEMA,
                             usuario=usuario, texto=f'Se anuló el link para firmar «{sol.formulario.nombre}».')
