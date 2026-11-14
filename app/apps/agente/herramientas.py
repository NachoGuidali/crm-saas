"""
Herramientas (tools) del agente: lo que puede HACER además de hablar. Cada una tiene su definición para Claude
(JSON schema estricto) y su ejecución contra el CRM. En el simulador nada toca la base: se registra en el contexto.
"""
import logging
import re
from datetime import date, datetime, timedelta

from django.db import transaction
from django.utils import timezone

from .models import CAMPOS_BASE_AGENTE

logger = logging.getLogger('apps.agente')

CAMPO_OBSERVACION = 'observacion'


def _tool(nombre, descripcion, propiedades, requeridos=None):
    return {
        'name': nombre, 'description': descripcion, 'strict': True,
        'input_schema': {'type': 'object', 'properties': propiedades,
                         'required': requeridos if requeridos is not None else list(propiedades),
                         'additionalProperties': False},
    }


def campos_disponibles(config):
    """[(clave, etiqueta, tipo, opciones)] de los datos que el agente puede guardar."""
    base = dict(CAMPOS_BASE_AGENTE)
    claves = ['nombre'] + [c for c in (config.campos_base or []) if c in base and c != 'nombre']
    campos = [(c, base[c], 'fecha' if c == 'fecha_nacimiento' else 'texto', []) for c in claves]
    campos += [(c.slug, c.nombre, c.tipo, c.opciones or []) for c in config.campos.filter(activo=True).order_by('nombre')]
    campos.append((CAMPO_OBSERVACION, 'Observación libre para el asesor', 'texto', []))
    return campos


def valor_actual(ctx, clave):
    """Valor que tiene hoy el lead en ese dato (ficha + lo guardado en este turno / en el simulador)."""
    if clave in ctx.datos:
        return ctx.datos[clave]
    c = ctx.contacto
    if c is None:
        return None
    if clave in dict(CAMPOS_BASE_AGENTE):
        return getattr(c, clave, None)
    return (c.datos_extra or {}).get(clave)


def faltantes(ctx):
    """Etiquetas de los datos obligatorios para derivar que todavía no tiene el lead."""
    obligatorios = set(ctx.config.campos_obligatorios or [])
    return [etiqueta for clave, etiqueta, _, _ in campos_disponibles(ctx.config)
            if clave in obligatorios and valor_actual(ctx, clave) in (None, '', [])]


def definiciones(config, documentos_enviables, formularios_firma):
    """Lista de tools para la API. Orden y contenido estables (no cambian entre mensajes: ayuda a la caché)."""
    claves = [c[0] for c in campos_disponibles(config)]
    tools = [
        _tool('guardar_datos',
              'Guarda en la ficha del lead los datos que te dio (apenas los sepas, sin esperar al final). '
              'Fechas en formato AAAA-MM-DD; Sí/No como "si" o "no".',
              {'datos': {'type': 'array', 'items': {
                  'type': 'object', 'additionalProperties': False, 'required': ['campo', 'valor'],
                  'properties': {'campo': {'type': 'string', 'enum': claves}, 'valor': {'type': 'string'}}}}}),
        _tool('consultar_precios',
              'Busca en la tabla de precios oficial las filas que contienen TODOS los términos (plan, zona, edad, '
              'grupo…). Usala siempre antes de dar un precio: nunca des precios de memoria.',
              {'terminos': {'type': 'array', 'items': {'type': 'string'},
                            'description': 'Ej: ["Plan 210", "CABA"]. Vacío = toda la tabla (si es corta).'}}),
        _tool('programar_seguimiento',
              'Agenda volver a escribirle al lead más tarde si no responde (ej. "mañana a la tarde"). '
              'Reemplaza cualquier seguimiento anterior de este chat.',
              {'en_horas': {'type': 'number', 'description': 'Dentro de cuántas horas (entre 1 y 336).'},
               'motivo': {'type': 'string', 'description': 'Qué le vas a decir o preguntar.'}}),
        _tool('derivar_a_asesor',
              'El lead CALIFICA según los criterios: lo pasás al asesor que cierra la venta. Después de usarla, '
              'despedite avisándole que un asesor lo va a contactar.',
              {'resumen': {'type': 'string', 'description': 'Resumen para el asesor: quién es, qué busca, qué le cotizaste, objeciones.'},
               'motivo': {'type': 'string', 'description': 'Por qué califica.'}}),
        _tool('marcar_pendiente',
              'Le interesa pero todavía no está listo (ej. "lo hablo con mi pareja", "escribime la semana que viene", '
              '"cobro a fin de mes"). Queda en espera y le volvés a escribir solo en la fecha indicada.',
              {'motivo': {'type': 'string', 'description': 'Por qué queda pendiente, en pocas palabras.'},
               'en_dias': {'type': 'number', 'description': 'En cuántos días recontactarlo (entre 1 y 90).'}}),
        _tool('descartar_lead',
              'El lead NO califica, no le interesa, o pide que no le escriban más. Despedite con amabilidad.',
              {'motivo': {'type': 'string'},
               'no_contactar': {'type': 'boolean', 'description': 'true si pidió expresamente que no le escriban más.'}}),
        _tool('pedir_ayuda_humano',
              'Pasá el chat a una persona del equipo: pide hablar con un humano, está enojado, es un tema que no '
              'podés resolver o no sabés la respuesta. Avisale que en breve lo atiende una persona.',
              {'motivo': {'type': 'string'}}),
    ]
    if documentos_enviables:
        tools.append(_tool('enviar_documento', 'Le manda al lead un archivo (folleto, cartilla…).',
                           {'documento': {'type': 'string', 'enum': [d.titulo for d in documentos_enviables]}}))
    if formularios_firma:
        tools.append(_tool('enviar_formulario_firma',
                           'Le manda al lead el link para completar y firmar un formulario (ej. solicitud de ingreso).',
                           {'formulario': {'type': 'string', 'enum': [f.nombre for f in formularios_firma]}}))
    return tools


# ── Ejecución ──────────────────────────────────────────────────────────────

def ejecutar(ctx, nombre, entrada):
    """Devuelve el texto del resultado para el modelo. Nunca levanta: un error vuelve como texto."""
    fn = {
        'guardar_datos': _guardar_datos, 'consultar_precios': _consultar_precios,
        'programar_seguimiento': _programar_seguimiento, 'derivar_a_asesor': _derivar,
        'descartar_lead': _descartar, 'pedir_ayuda_humano': _pedir_ayuda, 'marcar_pendiente': _pendiente,
        'enviar_documento': _enviar_documento, 'enviar_formulario_firma': _enviar_firma,
    }.get(nombre)
    if fn is None:
        return f'Herramienta desconocida: {nombre}', True
    try:
        return fn(ctx, entrada or {}), False
    except Exception as e:  # que una falla del CRM no corte la charla
        logger.exception('Herramienta %s falló', nombre)
        return f'No se pudo completar ({e}). Seguí la conversación sin esa acción.', True


def _normalizar(tipo, valor, opciones):
    v = (valor or '').strip()
    if tipo == 'numero':
        n = re.sub(r'[^\d,.\-]', '', v).replace(',', '.')
        float(n)
        return n
    if tipo == 'fecha':
        for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y'):
            try:
                return datetime.strptime(v, fmt).date().isoformat()
            except ValueError:
                pass
        raise ValueError('fecha inválida (usá AAAA-MM-DD)')
    if tipo == 'sino':
        return v.lower() in ('si', 'sí', 'true', '1', 'yes')
    if tipo == 'lista' and opciones:
        for o in opciones:
            if o.lower() == v.lower():
                return o
        raise ValueError(f'opción inválida (válidas: {", ".join(opciones)})')
    if tipo == 'email':
        if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', v):
            raise ValueError('email inválido')
    return v


def _guardar_datos(ctx, entrada):
    campos = {c[0]: c for c in campos_disponibles(ctx.config)}
    guardados, errores, extra, base, observaciones = [], [], {}, {}, []
    for item in entrada.get('datos') or []:
        clave, valor = item.get('campo'), item.get('valor', '')
        if clave not in campos:
            errores.append(f'{clave}: no existe')
            continue
        _, etiqueta, tipo, opciones = campos[clave]
        if clave == CAMPO_OBSERVACION:
            observaciones.append(valor.strip())
            guardados.append(etiqueta)
            continue
        try:
            v = _normalizar(tipo, valor, opciones)
        except (ValueError, TypeError) as e:
            errores.append(f'{etiqueta}: {e}')
            continue
        if clave == 'dni':
            v = re.sub(r'\D', '', v)
        (base if clave in dict(CAMPOS_BASE_AGENTE) else extra)[clave] = v
        guardados.append(f'{etiqueta}={v}')
    ctx.datos.update(base)
    ctx.datos.update(extra)
    if not ctx.simulado and ctx.contacto is not None:
        c = ctx.contacto
        cambios = []
        for k, v in base.items():
            if k == 'fecha_nacimiento':
                v = date.fromisoformat(v)
            if getattr(c, k) != v:
                setattr(c, k, v)
                cambios.append(k)
        if extra:
            c.datos_extra = {**(c.datos_extra or {}), **extra}
            cambios.append('datos_extra')
        if cambios:
            c.save(update_fields=cambios + ['updated_at'])
        if observaciones:
            _actividad(ctx, 'Agente IA anotó: ' + ' · '.join(observaciones))
    res = ('Guardado: ' + ', '.join(guardados)) if guardados else 'No se guardó nada.'
    return res + (f' Errores: {"; ".join(errores)}' if errores else '')


def _consultar_precios(ctx, entrada):
    from .conocimiento import MAX_RESULTADOS, buscar_precios
    cfg = ctx.config
    if not cfg.precios_filas:
        return 'No hay tabla de precios cargada: no des precios, decile que un asesor se lo confirma.'
    filas = buscar_precios(cfg, entrada.get('terminos'))
    if not filas:
        return ('Sin resultados para esos términos. Columnas de la tabla: ' + ' | '.join(cfg.precios_columnas)
                + '. Probá con menos términos o con otras palabras.')
    cab = ' | '.join(cfg.precios_columnas)
    cuerpo = '\n'.join(' | '.join(f) for f in filas[:MAX_RESULTADOS])
    extra = f'\n(Hay {len(filas)} filas: mostré {MAX_RESULTADOS}. Refiná la búsqueda.)' if len(filas) > MAX_RESULTADOS else ''
    return f'{cab}\n{cuerpo}{extra}'


def _programar_seguimiento(ctx, entrada):
    from .models import SeguimientoAgente
    horas = min(max(float(entrada.get('en_horas') or 24), 1), 336)
    cuando = timezone.now() + timedelta(hours=horas)
    motivo = (entrada.get('motivo') or '')[:300]
    ctx.acciones.append(f'Seguimiento en {horas:g} h: {motivo}')
    if not ctx.simulado and ctx.conv is not None:
        SeguimientoAgente.objects.filter(conversacion=ctx.conv, hecho=False).update(hecho=True, resultado='Reemplazado')
        SeguimientoAgente.objects.create(conversacion=ctx.conv, cuando=cuando, motivo=motivo)
    return f'Listo, le vuelvo a escribir el {timezone.localtime(cuando):%d/%m a las %H:%M} si no contesta antes.'


def _apagar_bot(ctx, derivada):
    from apps.whatsapp.models import Conversacion
    from .models import SeguimientoAgente
    Conversacion.objects.filter(pk=ctx.conv.pk).update(bot_activo=False, bot_derivada=derivada)
    SeguimientoAgente.objects.filter(conversacion=ctx.conv, hecho=False).update(hecho=True, resultado='Chat cerrado por el agente')


def _mover(ctx, etapa, tipificacion=None):
    from apps.crm import services as crm
    op = ctx.oportunidad
    if etapa is None or op is None or etapa.embudo_id != op.embudo_id or op.etapa_id == etapa.pk:
        return
    try:
        crm.mover_etapa(op, etapa, tipificacion=tipificacion, nota='Agente IA', automatico=True)
    except Exception as e:
        logger.warning('Agente IA: no se pudo mover %s a %s: %s', op.pk, etapa, e)


def _actividad(ctx, texto):
    from apps.crm.models import Actividad
    if ctx.contacto is None:
        return
    Actividad.objects.create(contacto=ctx.contacto, oportunidad=ctx.oportunidad, tipo=Actividad.TIPO_WHATSAPP,
                             texto=texto[:4000])


def _derivar(ctx, entrada):
    resumen, motivo = (entrada.get('resumen') or '').strip(), (entrada.get('motivo') or '').strip()
    faltan = faltantes(ctx)
    if faltan:
        ctx.acciones.append(f'Quiso derivar pero faltan: {", ".join(faltan)}')
        return ('Todavía NO se puede derivar: faltan estos datos obligatorios: ' + ', '.join(faltan)
                + '. Pedíselos al lead (de a uno, con naturalidad), guardalos con guardar_datos y después volvé a derivar.')
    ctx.resultado = 'derivado'
    ctx.acciones.append(f'Derivado al asesor: {motivo}')
    if ctx.simulado:
        return 'Derivado (simulación). Despedite avisando que un asesor lo contacta.'
    _apagar_bot(ctx, derivada=False)
    _mover(ctx, ctx.config.etapa_derivado)
    _actividad(ctx, f'Agente IA derivó al asesor. Motivo: {motivo}\n\nResumen: {resumen}')
    from apps.avisos.models import EV_DERIVADO_IA
    from apps.avisos.services import disparar
    disparar(EV_DERIVADO_IA, oportunidad=ctx.oportunidad, conv=ctx.conv, motivo=motivo)
    if ctx.config.derivar_destino_id and ctx.config.derivar_destino.activo:
        from apps.integraciones.webhooks import encolar
        from .tasks import payload_derivacion
        encolar(ctx.config.derivar_destino, payload_derivacion(ctx.conv, resumen, motivo), ctx.oportunidad,
                'Derivación del agente IA')
    elif ctx.config.derivar_url:
        from .tasks import enviar_derivacion
        conv_pk = ctx.conv.pk
        transaction.on_commit(lambda: enviar_derivacion.delay(conv_pk, resumen, motivo))
    guia = ctx.config.mensaje_derivacion or ''
    return 'Derivado. Despedite avisando que un asesor lo va a contactar' + (f' (por ejemplo: "{guia}").' if guia else '.')


def _pendiente(ctx, entrada):
    from .models import SeguimientoAgente
    motivo = (entrada.get('motivo') or '').strip()[:250]
    dias = int(min(max(float(entrada.get('en_dias') or 7), 1), 90))
    cuando = timezone.now() + timedelta(days=dias)
    ctx.resultado = 'pendiente'
    ctx.acciones.append(f'Pendiente {dias} días: {motivo}')
    if ctx.simulado:
        return f'Queda pendiente (simulación): le volvés a escribir en {dias} días. Despedite hasta entonces.'
    _mover(ctx, ctx.config.etapa_pendiente)
    SeguimientoAgente.objects.filter(conversacion=ctx.conv, hecho=False).update(hecho=True, resultado='Reemplazado')
    SeguimientoAgente.objects.create(conversacion=ctx.conv, cuando=cuando,
                                     motivo=f'Retomar: quedó pendiente porque {motivo}'[:300])
    _actividad(ctx, f'Agente IA dejó pendiente: {motivo} · recontacto el {timezone.localtime(cuando):%d/%m/%Y}')
    return (f'Listo: queda pendiente y le escribís el {timezone.localtime(cuando):%d/%m}. Despedite cordialmente '
            'diciéndole que le escribís entonces (si escribe antes, le respondés).')


def _descartar(ctx, entrada):
    motivo = (entrada.get('motivo') or '').strip()
    no_contactar = bool(entrada.get('no_contactar'))
    ctx.resultado = 'descartado'
    ctx.acciones.append(f'Descartado: {motivo}' + (' (no contactar)' if no_contactar else ''))
    if ctx.simulado:
        return 'Descartado (simulación). Despedite con amabilidad.'
    _apagar_bot(ctx, derivada=False)
    if no_contactar and ctx.contacto is not None and not ctx.contacto.no_contactar:
        ctx.contacto.no_contactar = True
        ctx.contacto.save(update_fields=['no_contactar', 'no_contactar_desde', 'updated_at'])
    _mover(ctx, ctx.config.etapa_descartado, ctx.config.tipificacion_descartado)
    _actividad(ctx, f'Agente IA descartó el lead: {motivo}' + (' · pidió no ser contactado' if no_contactar else ''))
    return 'Descartado. Despedite con amabilidad, sin insistir.'


def _pedir_ayuda(ctx, entrada):
    motivo = (entrada.get('motivo') or '').strip()
    ctx.resultado = 'humano'
    ctx.acciones.append(f'Pidió ayuda humana: {motivo}')
    if ctx.simulado:
        return 'Avisado al equipo (simulación). Decile que en breve lo atiende una persona.'
    from apps.users.models import User
    from apps.users.services import notificar
    _apagar_bot(ctx, derivada=True)
    _actividad(ctx, f'Agente IA pidió que lo atienda una persona: {motivo}')
    url = f'/whatsapp/?conv={ctx.conv.pk}'
    destinatarios = [ctx.conv.agente] if ctx.conv.agente_id else list(
        User.objects.filter(is_active=True).exclude(rol=User.ROL_AGENTE))
    for u in destinatarios:
        notificar(u, 'whatsapp', f'El agente IA necesita ayuda con {ctx.conv.nombre_mostrar}', motivo[:150], url)
    from apps.avisos.models import EV_HUMANO
    from apps.avisos.services import disparar
    disparar(EV_HUMANO, oportunidad=ctx.oportunidad, conv=ctx.conv, motivo=f'Agente IA: {motivo}')
    return 'Avisado al equipo. Decile que en breve lo atiende una persona.'


def _enviar_documento(ctx, entrada):
    from .models import DocumentoAgente
    doc = DocumentoAgente.objects.filter(titulo=entrada.get('documento'), enviable=True, activo=True).first()
    if doc is None or not doc.archivo:
        return 'Ese documento no está disponible.'
    ctx.acciones.append(f'Envió el documento "{doc.titulo}"')
    if not ctx.simulado:
        ctx.adjuntos.append(doc)
    return f'Listo, el archivo "{doc.titulo}" se le manda junto con tu mensaje.'


def _enviar_firma(ctx, entrada):
    from apps.firmas import services as firmas
    from apps.firmas.models import FormularioFirma
    form = FormularioFirma.objects.filter(nombre=entrada.get('formulario'), activo=True).first()
    if form is None:
        return 'Ese formulario no está disponible.'
    ctx.acciones.append(f'Envió el formulario "{form.nombre}"')
    if ctx.simulado:
        return 'Formulario enviado (simulación): le llega un link por WhatsApp.'
    if ctx.oportunidad is None:
        return 'No se puede: el chat no tiene una oportunidad en el CRM.'
    sol = firmas.crear_solicitud(form, ctx.oportunidad, origen='api')
    enviados, errores = firmas.enviar(sol, ['whatsapp'])
    return 'Listo: le llegó el link para completar y firmar.' if enviados else f'No se pudo enviar: {" ".join(errores)}'
