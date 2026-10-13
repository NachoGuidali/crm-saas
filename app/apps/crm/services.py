"""
Lógica de negocio del CRM. Todos los canales (importación, API, WhatsApp, Anura,
carga manual) usan estas funciones: así la deduplicación, la asignación y las
automatizaciones se comportan igual sin importar por dónde entra el dato.
"""
import logging
from dataclasses import dataclass
from datetime import timedelta

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import Count, F, Q
from django.utils import timezone

from core.phone import normalizar_telefono, variantes_telefono

from .models import (
    Actividad, Contacto, Embudo, Etapa, HistorialEtapa, Oportunidad, Tarea, Tipificacion,
)

logger = logging.getLogger('apps.crm')


class ErrorNegocio(Exception):
    """Error de validación de negocio: el mensaje se muestra tal cual al usuario."""


class FaltanCampos(ErrorNegocio):
    """Faltan datos obligatorios para pasar a una etapa. `faltan` = [{clave, nombre, tipo, opciones}]."""

    def __init__(self, etapa, faltan):
        self.etapa, self.faltan = etapa, faltan
        super().__init__(f'Para pasar a "{etapa}" falta completar: ' + ', '.join(f['nombre'] for f in faltan) + '.')


def requisitos_de_etapa(etapa):
    """Claves obligatorias al entrar a `etapa`: las de esa etapa y de todas las anteriores del embudo.
    Cerrar como No venta no exige datos."""
    if etapa.es_perdido:
        return []
    claves = []
    for e in etapa.embudo.etapas.filter(orden__lte=etapa.orden).exclude(tipo=Etapa.TIPO_PERDIDO):
        for clave in e.campos_requeridos or []:
            if clave not in claves:
                claves.append(clave)
    return claves


def descripcion_campo(clave):
    """{clave, nombre, tipo, opciones} para una clave de requisito (campo fijo o 'cp:<slug>')."""
    from .models import CAMPOS_FIJOS_REQUERIBLES, CampoPersonalizado
    if clave.startswith('cp:'):
        c = CampoPersonalizado.objects.filter(slug=clave[3:], activo=True).first()
        if c is None:
            return None
        return {'clave': clave, 'nombre': c.nombre, 'tipo': c.tipo, 'opciones': c.opciones}
    fijo = next((f for f in CAMPOS_FIJOS_REQUERIBLES if f[0] == clave), None)
    if fijo is None:
        return None
    return {'clave': clave, 'nombre': fijo[1], 'tipo': fijo[3], 'opciones': []}


def valor_actual(op, clave):
    if clave.startswith('cp:'):
        return (op.contacto.datos_extra or {}).get(clave[3:])
    if clave == 'valor':
        return op.valor
    return getattr(op.contacto, clave, None)


def campos_faltantes(op, etapa, valor=None):
    faltan = []
    for clave in requisitos_de_etapa(etapa):
        if clave == 'valor' and valor not in (None, ''):
            continue
        desc = descripcion_campo(clave)
        if desc and valor_actual(op, clave) in (None, '', []):
            faltan.append(desc)
    return faltan


# ═══════════════════════════════════════════════════════════════════════════
# Contactos: alta/actualización sin duplicados
# ═══════════════════════════════════════════════════════════════════════════

CAMPOS_CONTACTO = ('nombre', 'email', 'dni', 'localidad', 'provincia', 'fecha_atencion',
                   'especialidad_atencion', 'fecha_nacimiento', 'telefono_alternativo')


def buscar_contacto(telefono='', email='', dni=''):
    """Busca un contacto existente por teléfono (clave principal); si no hay teléfono, por email o DNI."""
    tel = normalizar_telefono(telefono)
    if tel:
        encontrado = (Contacto.objects.filter(telefono__in=variantes_telefono(tel)).first()
                      or Contacto.objects.filter(telefono_alternativo=tel).first())
        if encontrado:
            return encontrado
        return None
    if email:
        encontrado = Contacto.objects.filter(email__iexact=email.strip()).first()
        if encontrado:
            return encontrado
    if dni:
        return Contacto.objects.filter(dni=str(dni).strip()).first()
    return None


def upsert_contacto(datos: dict, usuario=None, sobrescribir=False):
    """
    Crea el contacto o, si ya existe (mismo teléfono), completa los datos que le faltan.
    Devuelve (contacto, creado). Es seguro ante altas concurrentes del mismo número:
    la constraint única de teléfono + el reintento evitan duplicados.
    """
    telefono = normalizar_telefono(datos.get('telefono', ''))
    email = (datos.get('email') or '').strip().lower()
    dni = str(datos.get('dni') or '').strip()
    if not telefono and not email and not dni:
        raise ErrorNegocio('El contacto necesita al menos teléfono, email o DNI.')

    contacto = buscar_contacto(telefono, email, dni)
    if contacto:
        _completar_contacto(contacto, datos, telefono, sobrescribir)
        return contacto, False

    nuevo = Contacto(
        telefono=telefono,
        creado_por=usuario,
        nombre=(datos.get('nombre') or '').strip()[:200] or (telefono or email or dni),
        datos_extra=dict(datos.get('datos_extra') or {}),
    )
    for campo in CAMPOS_CONTACTO:
        if campo != 'nombre' and datos.get(campo) not in (None, ''):
            setattr(nuevo, campo, datos[campo])
    try:
        with transaction.atomic():
            nuevo.save()
        if datos.get('etiquetas'):
            nuevo.etiquetas.add(*datos['etiquetas'])
        return nuevo, True
    except IntegrityError:
        # Otro proceso creó el mismo teléfono en paralelo: usamos ese.
        existente = buscar_contacto(telefono, email, dni)
        if existente is None:
            raise
        _completar_contacto(existente, datos, telefono, sobrescribir)
        return existente, False


def _completar_contacto(contacto, datos, telefono, sobrescribir):
    cambios = []
    for campo in CAMPOS_CONTACTO:
        valor = datos.get(campo)
        if valor in (None, ''):
            continue
        actual = getattr(contacto, campo)
        # Nombres "de relleno" (el teléfono, "WhatsApp +549…") se reemplazan por el nombre real cuando llega.
        es_relleno = campo == 'nombre' and (not actual or actual == contacto.telefono
                                            or str(actual).startswith(('WhatsApp ', 'Llamada ', '+')))
        if sobrescribir or not actual or es_relleno:
            if actual != valor:
                setattr(contacto, campo, valor)
                cambios.append(campo)
    if telefono and not contacto.telefono:
        contacto.telefono = telefono
        cambios.append('telefono')
    extra = datos.get('datos_extra') or {}
    if extra:
        merged = {**extra, **(contacto.datos_extra or {})} if not sobrescribir else {**(contacto.datos_extra or {}), **extra}
        if merged != contacto.datos_extra:
            contacto.datos_extra = merged
            cambios.append('datos_extra')
    if cambios:
        contacto.save(update_fields=cambios + ['updated_at'])
    if datos.get('etiquetas'):
        contacto.etiquetas.add(*datos['etiquetas'])


# ═══════════════════════════════════════════════════════════════════════════
# Ingreso unificado de prospectos
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class ResultadoIngreso:
    contacto: Contacto
    oportunidad: Oportunidad | None
    contacto_nuevo: bool
    oportunidad_nueva: bool
    motivo: str = ''            # por qué no se creó una oportunidad nueva


def ingresar_prospecto(datos: dict, embudo: Embudo, origen: str, fuente='', usuario=None, etapa=None,
                       lote=None, agente=None, asignar=True, disparar_automatizaciones=True,
                       valor=None, origen_pauta='', pauta=None) -> ResultadoIngreso:
    """
    Punto de entrada único de un dato nuevo al CRM.

    - El contacto se deduplica por teléfono.
    - Si ya tiene una oportunidad activa en el embudo, NO se crea otra: se registra el reingreso.
    - Si ya compró (venta) tampoco: queda la marca de reingreso para que el agente lo vea.
    - Si se perdió antes, se abre una nueva (configurable por embudo).
    """
    contacto, contacto_nuevo = upsert_contacto(datos, usuario=usuario)
    texto_origen = dict(Oportunidad.ORIGEN_CHOICES).get(origen, origen)
    origen_pauta = (origen_pauta or '').strip()[:200]
    if pauta is None and origen_pauta:
        from apps.pautas.services import resolver_pauta
        pauta = resolver_pauta(origen_pauta)
    if pauta is not None and not origen_pauta:
        origen_pauta = pauta.nombre
    if pauta is not None or origen_pauta:
        texto_origen += f' · pauta "{pauta or origen_pauta}"'
    datos_reingreso = {'pauta_id': pauta.pk} if pauta is not None else {}

    activa = oportunidad_activa_de(contacto, embudo)
    if activa:
        _registrar_reingreso(activa, contacto, f'Reingresó por {texto_origen}{f" ({fuente})" if fuente else ""}. '
                                               'No se duplicó: ya tenía una oportunidad en curso.', usuario,
                             datos_reingreso)
        return ResultadoIngreso(contacto, activa, contacto_nuevo, False, 'ya_activa')

    ultima = contacto.oportunidades.filter(embudo=embudo).order_by('-created_at').first()
    if not (ultima and ultima.estado == Oportunidad.ESTADO_GANADA) and not embudo.clientes_de_otros:
        venta = contacto.oportunidades.filter(estado=Oportunidad.ESTADO_GANADA).order_by('-cerrada_at').first()
        if venta is not None:
            ultima = venta  # ya es cliente en otro embudo: no se abre ni se reparte
    if ultima and ultima.estado == Oportunidad.ESTADO_GANADA:
        _registrar_reingreso(ultima, contacto, f'Reingresó por {texto_origen}, pero ya es cliente (venta cerrada).', usuario,
                             datos_reingreso)
        return ResultadoIngreso(contacto, ultima, contacto_nuevo, False, 'ya_cliente')
    if ultima and ultima.estado == Oportunidad.ESTADO_PERDIDA and embudo.reingreso_perdidos == Embudo.REINGRESO_IGNORAR:
        _registrar_reingreso(ultima, contacto, f'Reingresó por {texto_origen} (se había cerrado como No venta).', usuario,
                             datos_reingreso)
        return ResultadoIngreso(contacto, ultima, contacto_nuevo, False, 'perdida_previa')
    if contacto.no_contactar:
        Actividad.objects.create(contacto=contacto, tipo=Actividad.TIPO_REINGRESO, usuario=usuario, datos=datos_reingreso,
                                 texto=f'Reingresó por {texto_origen}, pero está marcado como "no contactar".')
        return ResultadoIngreso(contacto, None, contacto_nuevo, False, 'no_contactar')

    etapa = etapa or embudo.etapa_inicial
    if etapa is None:
        raise ErrorNegocio(f'El embudo "{embudo}" no tiene etapas configuradas.')

    ahora = timezone.now()
    try:
        with transaction.atomic():
            op = Oportunidad.objects.create(
                contacto=contacto, embudo=embudo, etapa=etapa, origen=origen, fuente=fuente[:150],
                lote=lote, agente=agente, asignada_at=ahora if agente else None, creado_por=usuario,
                valor=valor, ultima_actividad_at=ahora, etapa_desde=ahora, pauta=pauta, origen_pauta=origen_pauta,
            )
    except IntegrityError:
        # Carrera: otro proceso creó la oportunidad activa para este contacto+embudo en paralelo.
        existente = oportunidad_activa_de(contacto, embudo)
        if existente is None:
            raise
        return ResultadoIngreso(contacto, existente, contacto_nuevo, False, 'ya_activa')

    historial = HistorialEtapa.objects.create(
        oportunidad=op, etapa_nueva=etapa, estado_nuevo=op.estado, usuario=usuario, nota=f'Ingreso: {texto_origen}',
    )
    Actividad.objects.create(
        contacto=contacto, oportunidad=op, tipo=Actividad.TIPO_SISTEMA, usuario=usuario,
        texto=f'Ingresó al embudo {embudo} por {texto_origen}{f" — {fuente}" if fuente else ""}.',
    )
    if agente:
        _post_asignacion(op, agente, usuario=usuario, notificar_agente=agente != usuario)
    elif asignar:
        asignar_oportunidad(op)

    if disparar_automatizaciones:
        _disparar_entrada_etapa(op, historial)
    from .puntaje import recalcular
    transaction.on_commit(lambda: recalcular(op))
    return ResultadoIngreso(contacto, op, contacto_nuevo, True)


def _registrar_reingreso(op, contacto, texto, usuario, datos=None):
    Actividad.objects.create(contacto=contacto, oportunidad=op, tipo=Actividad.TIPO_REINGRESO, texto=texto,
                             usuario=usuario, datos=datos or {})
    Oportunidad.objects.filter(pk=op.pk).update(ingresos=F('ingresos') + 1)
    from .puntaje import recalcular
    transaction.on_commit(lambda: recalcular(op))
    if op.activa:
        tocar(op)
    from apps.automatizaciones.services import lead_reingreso
    transaction.on_commit(lambda: lead_reingreso(op))


def marcar_prioridad(op, motivo='', usuario=None, prioritaria=True):
    """Marca (o quita) la prioridad de la oportunidad: aparece primero en Mi día, el tablero y el discador."""
    motivo = (motivo or 'Prioridad')[:150] if prioritaria else ''
    if not Oportunidad.objects.filter(pk=op.pk).exclude(prioritaria=prioritaria, prioridad_motivo=motivo) \
            .update(prioritaria=prioritaria, prioridad_motivo=motivo):
        return False
    op.prioritaria, op.prioridad_motivo = prioritaria, motivo
    Actividad.objects.create(contacto_id=op.contacto_id, oportunidad=op, tipo=Actividad.TIPO_SISTEMA, usuario=usuario,
                             texto=f'🔥 Marcada como prioridad: {motivo}' if prioritaria else 'Se quitó la prioridad.')
    if prioritaria:
        from apps.telefonia.models import CampaniaContacto
        CampaniaContacto.objects.filter(oportunidad=op, estado__in=[CampaniaContacto.ESTADO_PENDIENTE,
                                                                     CampaniaContacto.ESTADO_REINTENTAR],
                                         prioridad__lt=100).update(prioridad=F('prioridad') + 100)
    return True


def responsable_cliente(contacto):
    """Si la persona ya es cliente (venta cerrada): quién la atiende según el embudo de esa venta (o None)."""
    if contacto is None:
        return None
    venta = (contacto.oportunidades.filter(estado=Oportunidad.ESTADO_GANADA).select_related('embudo', 'agente')
             .order_by('-cerrada_at').first())
    return venta.embudo.responsable_de_cliente(venta) if venta else None


def oportunidad_activa_de(contacto, embudo=None):
    qs = contacto.oportunidades.filter(estado__in=Oportunidad.ESTADOS_ACTIVOS)
    if embudo is not None:
        return qs.filter(embudo=embudo).first()
    return qs.order_by('-ultima_actividad_at').first()


def tocar(op, momento=None):
    """Marca actividad reciente sin disparar señales ni pisar otros campos (update directo)."""
    Oportunidad.objects.filter(pk=op.pk).update(ultima_actividad_at=momento or timezone.now())


# ═══════════════════════════════════════════════════════════════════════════
# Asignación automática
# ═══════════════════════════════════════════════════════════════════════════

def _candidatos(agentes_qs, entre, excluir=(), vacio_si_nadie_conectado=True):
    """Agentes activos y disponibles; si se reparte entre conectados, solo los que tienen el CRM abierto."""
    from core import presencia
    base = list(agentes_qs.filter(is_active=True, disponible=True).exclude(pk__in=[u.pk for u in excluir]).order_by('pk'))
    if entre != Embudo.ENTRE_CONECTADOS or not base:
        return base
    online = presencia.conectados(u.pk for u in base)
    en_linea = [u for u in base if u.pk in online]
    return en_linea if en_linea or vacio_si_nadie_conectado else base


def _elegir(candidatos, modo, ultimo_id):
    if not candidatos:
        return None
    if modo == Embudo.ASIG_MENOR_CARGA:
        cargas = dict(
            Oportunidad.objects.filter(agente__in=candidatos, estado=Oportunidad.ESTADO_ABIERTA)
            .values_list('agente').annotate(n=Count('pk')).values_list('agente', 'n')
        )
        return min(candidatos, key=lambda u: (cargas.get(u.pk, 0), u.pk))
    # Round robin: el siguiente al último asignado (por pk), dando la vuelta.
    if ultimo_id:
        for u in candidatos:
            if u.pk > ultimo_id:
                return u
    return candidatos[0]


def elegir_agente(embudo: Embudo, excluir=(), modo=None):
    """Elige el próximo agente según la regla general del embudo. Debe llamarse dentro de una transacción."""
    candidatos = _candidatos(embudo.agentes.all(), embudo.asignar_entre, excluir,
                             vacio_si_nadie_conectado=embudo.sin_conectados == Embudo.SIN_CONECTADOS_ENCOLAR)
    modo = modo or embudo.modo_asignacion
    if modo == Embudo.ASIG_MANUAL:
        modo = Embudo.ASIG_ROUND_ROBIN
    return _elegir(candidatos, modo, embudo.ultimo_asignado_id)


def regla_para(op: Oportunidad):
    """Primera regla de asignación activa del embudo que coincide con el origen del lead (o None)."""
    from .models import ReglaAsignacion
    for regla in ReglaAsignacion.objects.filter(embudo_id=op.embudo_id, activa=True).prefetch_related('pautas'):
        if regla.coincide(op):
            return regla
    return None


def _elegir_por_regla(regla, embudo):
    """(agente, seguir_con_embudo). Debe llamarse dentro de una transacción con la regla bloqueada."""
    entre = embudo.asignar_entre if regla.asignar_entre == regla.ENTRE_EMBUDO else regla.asignar_entre
    candidatos = _candidatos(regla.agentes.all(), entre,
                             vacio_si_nadie_conectado=regla.si_no_hay != regla.SI_NO_HAY_TODOS)
    modo = embudo.modo_asignacion if embudo.modo_asignacion != Embudo.ASIG_MANUAL else Embudo.ASIG_ROUND_ROBIN
    agente = _elegir(candidatos, modo, regla.ultimo_asignado_id)
    return agente, agente is None and regla.si_no_hay == regla.SI_NO_HAY_EMBUDO


def _dejar_en_cola(op, motivo):
    Oportunidad.objects.filter(pk=op.pk).update(pendiente_asignacion=True)
    op.pendiente_asignacion = True
    logger.info('Oportunidad #%s queda en cola de asignación: %s', op.pk, motivo)


def asignar_oportunidad(op: Oportunidad, forzar_horario=False):
    """
    Asigna la oportunidad: primero las reglas por origen del embudo, después su regla general.
    Bloquea la fila del embudo (o de la regla) para que dos ingresos simultáneos no le den el mismo turno
    al mismo agente.
    """
    from .models import ReglaAsignacion
    if op.agente_id:
        return op.agente
    embudo = op.embudo
    regla = regla_para(op)
    if regla and regla.accion == ReglaAsignacion.ACCION_SIN_ASIGNAR:
        if op.pendiente_asignacion:
            Oportunidad.objects.filter(pk=op.pk).update(pendiente_asignacion=False)
            op.pendiente_asignacion = False
        if not op.actividades.filter(tipo=Actividad.TIPO_ASIGNACION, datos__regla=regla.pk).exists():
            Actividad.objects.create(contacto_id=op.contacto_id, oportunidad=op, tipo=Actividad.TIPO_ASIGNACION,
                                     datos={'regla': regla.pk},
                                     texto=f'Queda sin asignar por la regla "{regla}" (asignación manual).')
        return None
    if regla is None and embudo.modo_asignacion == Embudo.ASIG_MANUAL:
        return None
    if not forzar_horario and not embudo.en_horario() and embudo.fuera_de_horario == Embudo.FUERA_HORARIO_ENCOLAR:
        _dejar_en_cola(op, 'fuera de horario')
        return None

    texto = None
    with transaction.atomic():
        agente = None
        if regla:
            regla_lock = ReglaAsignacion.objects.select_for_update().get(pk=regla.pk)
            agente, seguir = _elegir_por_regla(regla_lock, embudo)
            if agente:
                regla_lock.ultimo_asignado = agente
                regla_lock.save(update_fields=['ultimo_asignado'])
                texto = f'Asignada a {agente.display_name} (automática · regla "{regla}")'
            elif not seguir or embudo.modo_asignacion == Embudo.ASIG_MANUAL:
                _dejar_en_cola(op, f'regla "{regla}" sin agentes que puedan recibir')
                return None
        if agente is None:
            embudo_lock = Embudo.objects.select_for_update().get(pk=embudo.pk)
            agente = elegir_agente(embudo_lock)
            if agente is None:
                _dejar_en_cola(op, f'embudo {embudo} sin agentes que puedan recibir')
                return None
            embudo_lock.ultimo_asignado = agente
            embudo_lock.save(update_fields=['ultimo_asignado'])
        actualizadas = Oportunidad.objects.filter(pk=op.pk, agente__isnull=True).update(
            agente=agente, asignada_at=timezone.now(), pendiente_asignacion=False,
        )
    if not actualizadas:
        op.refresh_from_db(fields=['agente'])
        return op.agente
    op.agente, op.pendiente_asignacion = agente, False
    _post_asignacion(op, agente, usuario=None, texto=texto)
    return agente


def _post_asignacion(op, agente, usuario=None, notificar_agente=True, texto=None):
    from apps.users.services import notificar
    Actividad.objects.create(
        contacto_id=op.contacto_id, oportunidad=op, tipo=Actividad.TIPO_ASIGNACION, usuario=usuario,
        texto=texto or f'Asignada a {agente.display_name}' + (' (automática)' if usuario is None else ''),
    )
    if op.embudo.crear_tarea_al_asignar and op.estado == Oportunidad.ESTADO_ABIERTA:
        if not op.tareas.filter(estado=Tarea.ESTADO_PENDIENTE, asignado_a=agente).exists():
            Tarea.objects.create(
                oportunidad=op, contacto_id=op.contacto_id, asignado_a=agente, tipo=Tarea.TIPO_LLAMADA,
                titulo=f'Contactar a {op.contacto.nombre}', vence_at=timezone.now() + timedelta(hours=2),
                automatica=True,
            )
            invalidar_tareas(agente)
    if notificar_agente:
        notificar(agente, 'asignacion', f'Nuevo prospecto: {op.contacto.nombre}',
                  f'{op.embudo} · {op.contacto.telefono}', op.get_absolute_url())


def elegir_y_reasignar(op: Oportunidad, excluir=(), usuario=None, modo=None, nota='redistribución'):
    """Redistribución: elige el próximo agente (según la regla del embudo o `modo`) excluyendo algunos, y reasigna."""
    with transaction.atomic():
        embudo_lock = Embudo.objects.select_for_update().get(pk=op.embudo_id)
        agente = elegir_agente(embudo_lock, excluir=excluir, modo=modo)
        if agente is None:
            return None
        embudo_lock.ultimo_asignado = agente
        embudo_lock.save(update_fields=['ultimo_asignado'])
    reasignar(op, agente, usuario, nota=nota)
    return agente


def reasignar(op: Oportunidad, agente, usuario, nota=''):
    anterior = op.agente
    if anterior == agente:
        return
    Oportunidad.objects.filter(pk=op.pk).update(agente=agente, asignada_at=timezone.now(), pendiente_asignacion=False,
                                                vencida_at=None)
    op.agente = agente
    # Las tareas pendientes del agente anterior pasan al nuevo.
    op.tareas.filter(estado=Tarea.ESTADO_PENDIENTE).update(asignado_a=agente)
    if anterior:
        invalidar_tareas(anterior)
    if agente:
        invalidar_tareas(agente)
        _post_asignacion(op, agente, usuario=usuario, notificar_agente=agente != usuario,
                         texto=f'Reasignada de {anterior.display_name if anterior else "sin asignar"} '
                               f'a {agente.display_name}' + (f' — {nota}' if nota else ''))
    # La conversación de WhatsApp sigue al dueño de la oportunidad.
    from apps.whatsapp.models import Conversacion
    Conversacion.objects.filter(contacto_id=op.contacto_id).update(agente=agente)


# ═══════════════════════════════════════════════════════════════════════════
# Movimiento en el embudo
# ═══════════════════════════════════════════════════════════════════════════

def mover_etapa(op: Oportunidad, etapa: Etapa, usuario=None, tipificacion: Tipificacion = None, nota='',
                valor=None, puede_reabrir=False, automatico=False):
    """
    Mueve la oportunidad a otra etapa. Los cierres (Venta / No venta) exigen tipificación.
    Devuelve el HistorialEtapa creado (o None si no hubo cambio).
    """
    if etapa.embudo_id != op.embudo_id:
        raise ErrorNegocio('La etapa no pertenece al embudo de la oportunidad.')

    faltan = campos_faltantes(op, etapa, valor)
    if faltan:
        if automatico:
            return None  # los avances automáticos no saltean datos obligatorios: la etapa queda como está
        raise FaltanCampos(etapa, faltan)

    if etapa.es_cierre:
        esperado = Tipificacion.RESULTADO_VENTA if etapa.es_ganado else Tipificacion.RESULTADO_NO_VENTA
        if tipificacion is None:
            raise ErrorNegocio('Para cerrar hay que elegir una tipificación.')
        if tipificacion.resultado != esperado:
            raise ErrorNegocio('La tipificación no corresponde al tipo de cierre.')
        if tipificacion.embudo_id and tipificacion.embudo_id != op.embudo_id:
            raise ErrorNegocio('La tipificación no pertenece a este embudo.')
        if tipificacion.es_postergacion:
            raise ErrorNegocio('Esta tipificación posterga el prospecto: usá "Postergar".')
        if tipificacion.requiere_nota and not nota.strip():
            raise ErrorNegocio(f'La tipificación "{tipificacion}" requiere una nota.')

    with transaction.atomic():
        op = Oportunidad.objects.select_for_update(of=('self',)).select_related('contacto', 'embudo', 'etapa').get(pk=op.pk)
        if op.etapa_id == etapa.pk and (etapa.es_cierre or op.estado == Oportunidad.ESTADO_ABIERTA):
            return None
        cerrada = op.estado in (Oportunidad.ESTADO_GANADA, Oportunidad.ESTADO_PERDIDA)
        if cerrada and not puede_reabrir:
            raise ErrorNegocio('La oportunidad está cerrada. Solo un supervisor puede reabrirla.')

        ahora = timezone.now()
        anterior, estado_anterior = op.etapa, op.estado
        segundos = int((ahora - op.etapa_desde).total_seconds()) if op.etapa_desde else None
        op.etapa, op.etapa_desde, op.ultima_actividad_at = etapa, ahora, ahora
        campos = ['etapa', 'etapa_desde', 'ultima_actividad_at', 'estado', 'updated_at']

        if etapa.es_cierre:
            op.estado = Oportunidad.ESTADO_GANADA if etapa.es_ganado else Oportunidad.ESTADO_PERDIDA
            op.tipificacion, op.nota_cierre = tipificacion, nota.strip()
            op.cerrada_at, op.cerrada_por = ahora, usuario
            op.proximo_contacto_at, op.motivo_pausa = None, ''
            campos += ['tipificacion', 'nota_cierre', 'cerrada_at', 'cerrada_por', 'proximo_contacto_at', 'motivo_pausa']
            if valor is not None:
                op.valor = valor
                campos.append('valor')
        else:
            op.estado = Oportunidad.ESTADO_ABIERTA
            if cerrada:  # reapertura
                op.tipificacion, op.nota_cierre, op.cerrada_at, op.cerrada_por = None, '', None, None
                campos += ['tipificacion', 'nota_cierre', 'cerrada_at', 'cerrada_por']
            if etapa.marca_contacto_efectivo and not op.contacto_efectivo_at:
                op.contacto_efectivo_at = ahora
                campos.append('contacto_efectivo_at')
            if op.proximo_contacto_at:
                op.proximo_contacto_at, op.motivo_pausa = None, ''
                campos += ['proximo_contacto_at', 'motivo_pausa']
        # Un cierre ganado/perdido también cuenta como contacto efectivo si hubo venta
        if etapa.es_ganado and not op.contacto_efectivo_at:
            op.contacto_efectivo_at = ahora
            campos.append('contacto_efectivo_at')
        # La vendedora ya la trabajó (o se cerró): deja de ser prioridad
        if op.prioritaria and (etapa.es_cierre or (usuario is not None and not automatico)):
            op.prioritaria, op.prioridad_motivo = False, ''
            campos += ['prioritaria', 'prioridad_motivo']
        op.save(update_fields=list(dict.fromkeys(campos)))
        if etapa.es_ganado and op.embudo.etiqueta_venta_id:
            op.contacto.etiquetas.add(op.embudo.etiqueta_venta_id)

        historial = HistorialEtapa.objects.create(
            oportunidad=op, etapa_anterior=anterior, etapa_nueva=etapa, estado_anterior=estado_anterior,
            estado_nuevo=op.estado, usuario=usuario, nota=(nota or '')[:300], segundos_en_etapa_anterior=segundos,
        )
        if usuario is not None and not automatico:
            marcar_primer_contacto(op, ahora)
        texto = f'{anterior} → {etapa}'
        if tipificacion:
            texto += f' · {tipificacion.categoria + ": " if tipificacion.categoria else ""}{tipificacion}'
        if nota:
            texto += f'\n{nota}'
        Actividad.objects.create(
            contacto=op.contacto, oportunidad=op, tipo=Actividad.TIPO_CIERRE if etapa.es_cierre else Actividad.TIPO_ETAPA,
            usuario=usuario, texto=texto, datos={'automatico': automatico},
        )

        if etapa.es_cierre:
            _aplicar_accion_tipificacion(op, tipificacion, usuario)
            pendientes = op.tareas.filter(estado=Tarea.ESTADO_PENDIENTE)
            if pendientes.exists():
                pendientes.update(estado=Tarea.ESTADO_CANCELADA, resultado='Cancelada: la oportunidad se cerró.')
                if op.agente:
                    invalidar_tareas(op.agente)
            _sacar_de_discador(op)

    if op.estado == Oportunidad.ESTADO_GANADA and op.embudo.notificar_venta_supervisores:
        from apps.users.services import notificar_varios, supervisores_de
        notificar_varios(
            supervisores_de(op.embudo), 'venta', f'¡Venta! {op.contacto.nombre}',
            f'{op.embudo} · {tipificacion} · {op.agente.display_name if op.agente else "sin agente"}',
            op.get_absolute_url(),
        )
    _disparar_entrada_etapa(op, historial)
    from .puntaje import recalcular
    transaction.on_commit(lambda: recalcular(op))
    return historial


# ═══════════════════════════════════════════════════════════════════════════
# Pase entre embudos
# ═══════════════════════════════════════════════════════════════════════════

ASIGNAR_MISMO = 'mismo'
ASIGNAR_EMBUDO = 'embudo'
ASIGNAR_USUARIO = 'usuario'


def _etapa_destino(embudo, etapa):
    if etapa is not None and etapa.embudo_id != embudo.pk:
        raise ErrorNegocio('La etapa de destino no pertenece al embudo de destino.')
    etapa = etapa or embudo.etapa_inicial
    if etapa is None or etapa.es_cierre:
        raise ErrorNegocio(f'El embudo "{embudo}" no tiene una etapa de destino válida.')
    return etapa


def _asignar_tras_pase(op, asignar, usuario_destino, agente_anterior):
    from apps.users.models import User
    if asignar == ASIGNAR_USUARIO and usuario_destino is not None and usuario_destino.is_active:
        if op.agente_id != usuario_destino.pk:
            Oportunidad.objects.filter(pk=op.pk).update(agente=usuario_destino, asignada_at=timezone.now())
            op.agente = usuario_destino
            _post_asignacion(op, usuario_destino, usuario=None)
    elif asignar == ASIGNAR_EMBUDO:
        Oportunidad.objects.filter(pk=op.pk).update(agente=None)
        op.agente = None
        asignar_oportunidad(op)
    elif agente_anterior is not None and isinstance(agente_anterior, User) and op.agente_id != agente_anterior.pk:
        Oportunidad.objects.filter(pk=op.pk).update(agente=agente_anterior, asignada_at=timezone.now())
        op.agente = agente_anterior


def pasar_a_embudo(op, embudo, etapa=None, modo='mover', asignar=ASIGNAR_MISMO, usuario_destino=None, usuario=None):
    """
    Pasa una oportunidad a otro embudo.
    - modo 'crear': abre una oportunidad NUEVA en el destino (la original queda como está; ej. Venta → Clientes).
    - modo 'mover': la MISMA tarjeta cambia de embudo (ej. "En verificación" → embudo Verificaciones); recuerda de
      dónde vino para poder volver.
    Devuelve la oportunidad que quedó en el destino.
    """
    if embudo.pk == op.embudo_id:
        raise ErrorNegocio('La oportunidad ya está en ese embudo.')
    etapa = _etapa_destino(embudo, etapa)
    if Oportunidad.objects.filter(contacto_id=op.contacto_id, embudo=embudo,
                                  estado__in=Oportunidad.ESTADOS_ACTIVOS).exclude(pk=op.pk).exists():
        raise ErrorNegocio(f'El contacto ya tiene una oportunidad en curso en "{embudo}".')
    faltan = campos_faltantes(op, etapa)
    if faltan:
        raise ErrorNegocio(f'Faltan datos obligatorios para "{etapa}": ' + ', '.join(f['nombre'] for f in faltan))
    ahora = timezone.now()

    if modo == 'crear':
        try:
            with transaction.atomic():
                nueva = Oportunidad.objects.create(
                    contacto_id=op.contacto_id, embudo=embudo, etapa=etapa, origen=op.origen,
                    fuente=f'Desde {op.embudo}'[:150], creado_por=usuario, ultima_actividad_at=ahora, etapa_desde=ahora,
                    oportunidad_origen=op, agente=op.agente if asignar == ASIGNAR_MISMO else None,
                    asignada_at=ahora if asignar == ASIGNAR_MISMO and op.agente_id else None,
                )
                historial = HistorialEtapa.objects.create(oportunidad=nueva, etapa_nueva=etapa, estado_nuevo=nueva.estado,
                                                          usuario=usuario, nota=f'Desde {op.embudo} · {op.etapa}')
        except IntegrityError:
            raise ErrorNegocio(f'El contacto ya tiene una oportunidad en curso en "{embudo}".')
        Actividad.objects.create(contacto_id=op.contacto_id, oportunidad=nueva, tipo=Actividad.TIPO_SISTEMA,
                                 usuario=usuario, texto=f'Ingresó al embudo {embudo} ({etapa}) desde la oportunidad '
                                                        f'#{op.pk} de {op.embudo} ({op.etapa}).')
        Actividad.objects.create(contacto_id=op.contacto_id, oportunidad=op, tipo=Actividad.TIPO_SISTEMA, usuario=usuario,
                                 texto=f'Se abrió la oportunidad #{nueva.pk} en el embudo {embudo} ({etapa}).')
        if asignar == ASIGNAR_MISMO and nueva.agente_id:
            _post_asignacion(nueva, nueva.agente, usuario=None, notificar_agente=False,
                             texto=f'Asignada a {nueva.agente.display_name} (misma persona que en {op.embudo})')
        else:
            _asignar_tras_pase(nueva, asignar, usuario_destino, None)
        _disparar_entrada_etapa(nueva, historial)
        return nueva

    # modo 'mover'
    agente_anterior = op.agente
    try:
        with transaction.atomic():
            op = Oportunidad.objects.select_for_update(of=('self',)).select_related('embudo', 'etapa', 'contacto').get(pk=op.pk)
            if not op.activa:
                raise ErrorNegocio('Solo se pueden mover tarjetas en curso: para una cerrada, abrí una nueva.')
            embudo_ant, etapa_ant = op.embudo, op.etapa
            segundos = int((ahora - op.etapa_desde).total_seconds()) if op.etapa_desde else None
            op.embudo_previo, op.etapa_previa = embudo_ant, etapa_ant
            op.embudo, op.etapa, op.etapa_desde, op.ultima_actividad_at = embudo, etapa, ahora, ahora
            op.estado, op.proximo_contacto_at, op.motivo_pausa = Oportunidad.ESTADO_ABIERTA, None, ''
            op.save(update_fields=['embudo_previo', 'etapa_previa', 'embudo', 'etapa', 'etapa_desde',
                                   'ultima_actividad_at', 'estado', 'proximo_contacto_at', 'motivo_pausa', 'updated_at'])
            historial = HistorialEtapa.objects.create(
                oportunidad=op, etapa_anterior=etapa_ant, etapa_nueva=etapa, estado_anterior=Oportunidad.ESTADO_ABIERTA,
                estado_nuevo=op.estado, usuario=usuario, nota=f'Pase de embudo: {embudo_ant} → {embudo}',
                segundos_en_etapa_anterior=segundos,
            )
            Actividad.objects.create(contacto=op.contacto, oportunidad=op, tipo=Actividad.TIPO_ETAPA, usuario=usuario,
                                     texto=f'Pasó del embudo {embudo_ant} ({etapa_ant}) al embudo {embudo} ({etapa}).',
                                     datos={'pase_embudo': True})
    except IntegrityError:
        raise ErrorNegocio(f'El contacto ya tiene una oportunidad en curso en "{embudo}".')
    _asignar_tras_pase(op, asignar, usuario_destino, agente_anterior)
    invalidar_tareas(op.agente) if op.agente else None
    _disparar_entrada_etapa(op, historial)
    return op


def volver_a_embudo_previo(op, a_la_siguiente=False, asignar=ASIGNAR_MISMO, usuario_destino=None, usuario=None):
    """Devuelve la tarjeta al embudo del que vino (a la etapa donde estaba, o a la siguiente)."""
    op.refresh_from_db()
    if op.embudo_previo_id is None:
        raise ErrorNegocio('La oportunidad no vino de otro embudo.')
    embudo = op.embudo_previo
    etapa = op.etapa_previa if op.etapa_previa_id and op.etapa_previa.embudo_id == embudo.pk else None
    if etapa is not None and a_la_siguiente:
        etapa = (embudo.etapas.filter(tipo=Etapa.TIPO_NORMAL, orden__gt=etapa.orden).order_by('orden', 'pk').first()
                 or etapa)
    if etapa is not None and etapa.es_cierre:
        etapa = None
    return pasar_a_embudo(op, embudo, etapa, modo='mover', asignar=asignar, usuario_destino=usuario_destino,
                          usuario=usuario)


def _aplicar_accion_tipificacion(op, tipificacion, usuario):
    if not tipificacion:
        return
    contacto = op.contacto
    if tipificacion.accion == Tipificacion.ACCION_NO_CONTACTAR and not contacto.no_contactar:
        contacto.no_contactar = True
        contacto.save(update_fields=['no_contactar', 'no_contactar_desde', 'updated_at'])
        Actividad.objects.create(contacto=contacto, oportunidad=op, tipo=Actividad.TIPO_SISTEMA, usuario=usuario,
                                 texto='Contacto marcado como "no contactar": se bloquean mensajes y discador.')
    elif tipificacion.accion == Tipificacion.ACCION_DATO_ERRONEO and not contacto.telefono_invalido:
        contacto.telefono_invalido = True
        contacto.save(update_fields=['telefono_invalido', 'updated_at'])


def _sacar_de_discador(op):
    from apps.telefonia.models import CampaniaContacto
    CampaniaContacto.objects.filter(
        oportunidad=op, estado__in=[CampaniaContacto.ESTADO_PENDIENTE, CampaniaContacto.ESTADO_REINTENTAR],
    ).update(estado=CampaniaContacto.ESTADO_DESCARTADO, detalle='Oportunidad cerrada')


def _disparar_entrada_etapa(op, historial):
    if historial is None:
        return
    from apps.automatizaciones.services import programar_acciones_de_etapa
    op_id, hist_id = op.pk, historial.pk
    transaction.on_commit(lambda: programar_acciones_de_etapa(op_id, hist_id))


def siguiente_etapa(op):
    return (op.embudo.etapas.filter(tipo=Etapa.TIPO_NORMAL, orden__gt=op.etapa.orden)
            .order_by('orden', 'pk').first())


# ═══════════════════════════════════════════════════════════════════════════
# Pausas, postergaciones e intentos de contacto
# ═══════════════════════════════════════════════════════════════════════════

def pausar(op, usuario, motivo='', hasta=None):
    """'Frenar' un prospecto: sale de automatizaciones, discador y recordatorios hasta que se reanude."""
    if op.estado != Oportunidad.ESTADO_ABIERTA:
        raise ErrorNegocio('Solo se pueden pausar oportunidades en curso.')
    op.estado, op.motivo_pausa, op.proximo_contacto_at = Oportunidad.ESTADO_PAUSADA, motivo[:200], hasta
    op.ultima_actividad_at = timezone.now()
    op.save(update_fields=['estado', 'motivo_pausa', 'proximo_contacto_at', 'ultima_actividad_at', 'updated_at'])
    cuando = f' hasta el {timezone.localtime(hasta):%d/%m/%Y %H:%M}' if hasta else ''
    Actividad.objects.create(contacto_id=op.contacto_id, oportunidad=op, tipo=Actividad.TIPO_PAUSA, usuario=usuario,
                             texto=f'Pausada{cuando}.' + (f' Motivo: {motivo}' if motivo else ''))
    _sacar_de_discador(op)


def postergar(op, usuario, fecha, tipificacion=None, nota=''):
    """Tipificación 'Pide ser recontactado': no se pierde, se pausa y se agenda la reactivación."""
    if fecha is None or fecha <= timezone.now():
        raise ErrorNegocio('Indicá una fecha futura para recontactar.')
    motivo = str(tipificacion) if tipificacion else 'Pidió ser recontactado'
    if nota:
        motivo = f'{motivo}: {nota}'
    pausar(op, usuario, motivo=motivo, hasta=fecha)
    Tarea.objects.create(
        oportunidad=op, contacto_id=op.contacto_id, asignado_a=op.agente or usuario, creada_por=usuario,
        tipo=Tarea.TIPO_REACTIVACION, titulo=f'Recontactar a {op.contacto.nombre}', descripcion=nota,
        vence_at=fecha, prioridad=Tarea.PRIORIDAD_ALTA, automatica=True,
    )
    invalidar_tareas(op.agente or usuario)


def reanudar(op, usuario=None, automatico=False):
    if op.estado != Oportunidad.ESTADO_PAUSADA:
        return False
    Oportunidad.objects.filter(pk=op.pk).update(
        estado=Oportunidad.ESTADO_ABIERTA, motivo_pausa='', proximo_contacto_at=None, ultima_actividad_at=timezone.now(),
    )
    op.estado = Oportunidad.ESTADO_ABIERTA
    Actividad.objects.create(contacto_id=op.contacto_id, oportunidad=op, tipo=Actividad.TIPO_PAUSA, usuario=usuario,
                             texto='Reactivada automáticamente (llegó la fecha de recontacto).' if automatico
                             else 'Reactivada.')
    if automatico and op.agente:
        from apps.users.services import notificar
        notificar(op.agente, 'tarea', f'Hoy toca recontactar a {op.contacto.nombre}', str(op.embudo),
                  op.get_absolute_url())
    return True


def q_sla_vencido(ahora=None):
    """Leads abiertos, asignados y sin primera gestión cuyo SLA ya pasó (según el SLA de su embudo)."""
    ahora = ahora or timezone.now()
    q = Q(pk__in=[])
    for emb_id, minutos in Embudo.objects.filter(sla_minutos__gt=0).values_list('pk', 'sla_minutos'):
        q |= Q(embudo_id=emb_id, asignada_at__lt=ahora - timedelta(minutes=minutos))
    return q & Q(estado=Oportunidad.ESTADO_ABIERTA, agente__isnull=False, primer_contacto_at__isnull=True)


def revisar_sla():
    """Cada 2 min: avisa (y si el embudo lo pide, reasigna) los leads que pasaron el SLA sin primera gestión."""
    from apps.users.services import notificar, notificar_varios, supervisores_de
    ahora = timezone.now()
    vencidos = (Oportunidad.objects.filter(q_sla_vencido(ahora))
                .filter(Q(sla_alerta_at__isnull=True) | Q(sla_alerta_at__lt=F('asignada_at')))
                .select_related('embudo', 'agente', 'contacto')[:500])
    por_embudo, reasignadas = {}, 0
    for op in vencidos:
        embudo = op.embudo
        if not embudo.en_horario():
            continue  # fuera de horario no se reasigna ni se avisa: se revisa al abrir
        Oportunidad.objects.filter(pk=op.pk).update(sla_alerta_at=ahora)
        anterior = op.agente
        notificar(anterior, 'sla', f'SLA vencido: {op.contacto.nombre}',
                  f'Pasaron {embudo.sla_minutos} min sin contactarlo.', op.get_absolute_url())
        texto = f'SLA de primer contacto vencido ({embudo.sla_minutos} min) con {anterior.display_name}.'
        if embudo.sla_accion == Embudo.SLA_REASIGNAR and op.reasignaciones_sla < embudo.sla_max_reasignaciones:
            nuevo = elegir_y_reasignar(op, excluir=[anterior], usuario=None)
            if nuevo:
                Oportunidad.objects.filter(pk=op.pk).update(reasignaciones_sla=F('reasignaciones_sla') + 1)
                texto += f' Se reasignó a {nuevo.display_name}.'
                reasignadas += 1
        Actividad.objects.create(contacto_id=op.contacto_id, oportunidad=op, tipo=Actividad.TIPO_ASIGNACION, texto=texto,
                                 datos={'sla': True})
        por_embudo.setdefault(embudo, []).append(op)
    for embudo, ops in por_embudo.items():
        notificar_varios(supervisores_de(embudo), 'sla',
                         f'{len(ops)} lead{"s" if len(ops) > 1 else ""} fuera de SLA en {embudo}',
                         f'Sin contactar a los {embudo.sla_minutos} min de asignados.',
                         f'/oportunidades/?embudo={embudo.pk}&sla=vencido')
    return {'vencidos': sum(len(v) for v in por_embudo.values()), 'reasignadas': reasignadas}


def q_vencibles(embudo, ahora=None):
    """Fichas del embudo que ya pasaron su plazo de vencimiento y todavía no se procesaron."""
    ahora = ahora or timezone.now()
    q = Q(embudo=embudo, estado=Oportunidad.ESTADO_ABIERTA, agente__isnull=False, vencida_at__isnull=True,
          etapa__tipo=Etapa.TIPO_NORMAL, asignada_at__lt=ahora - timedelta(days=embudo.vence_dias))
    if embudo.vence_hasta_etapa_id:
        q &= Q(etapa__orden__lt=embudo.vence_hasta_etapa.orden)
    return q


def revisar_vencimientos():
    """Cada 15 min: fichas que pasaron N días con el mismo vendedor sin cerrarse vuelven al reparto
    (o quedan sin asignar, o solo se avisa). Desde la etapa protegida en adelante no vencen."""
    from apps.users.services import notificar, notificar_varios, supervisores_de
    ahora, total = timezone.now(), 0
    for embudo in Embudo.objects.filter(activo=True, vence_dias__gt=0).select_related('vence_hasta_etapa'):
        if not embudo.en_horario():
            continue  # fuera de horario no se mueve nada: se revisa al abrir
        ops = list(Oportunidad.objects.filter(q_vencibles(embudo, ahora))
                   .select_related('agente', 'contacto', 'embudo')[:300])
        for op in ops:
            anterior = op.agente
            texto = f'Ficha vencida: {embudo.vence_dias} días con {anterior.display_name} sin cerrarse.'
            if embudo.vence_accion == Embudo.VENCE_REASIGNAR:
                nuevo = elegir_y_reasignar(op, excluir=[anterior], modo=Embudo.ASIG_MENOR_CARGA,
                                           nota=f'ficha vencida ({embudo.vence_dias} días)')
                if nuevo:
                    texto += f' Se reasignó a {nuevo.display_name}.'
                else:
                    texto += ' No había otro vendedor disponible: sigue con el mismo.'
                    Oportunidad.objects.filter(pk=op.pk).update(vencida_at=ahora)
            elif embudo.vence_accion == Embudo.VENCE_LIBERAR:
                Oportunidad.objects.filter(pk=op.pk).update(agente=None, vencida_at=ahora, asignada_at=None)
                op.tareas.filter(estado=Tarea.ESTADO_PENDIENTE).update(asignado_a=None)
                invalidar_tareas(anterior)
                from apps.whatsapp.models import Conversacion
                Conversacion.objects.filter(contacto_id=op.contacto_id, agente=anterior).update(agente=None)
                texto += ' Quedó sin asignar para que supervisión la reparta.'
            else:
                Oportunidad.objects.filter(pk=op.pk).update(vencida_at=ahora)
            notificar(anterior, 'sistema', f'Ficha vencida: {op.contacto.nombre}', texto, op.get_absolute_url())
            Actividad.objects.create(contacto_id=op.contacto_id, oportunidad=op, tipo=Actividad.TIPO_ASIGNACION,
                                     texto=texto, datos={'vencimiento': True})
        if ops:
            total += len(ops)
            notificar_varios(supervisores_de(embudo), 'sistema',
                             f'{len(ops)} ficha{"s" if len(ops) > 1 else ""} vencida{"s" if len(ops) > 1 else ""} en {embudo}',
                             f'{embudo.vence_dias} días sin cerrarse: {embudo.get_vence_accion_display().lower()}.',
                             f'/oportunidades/?embudo={embudo.pk}')
    return total


def marcar_primer_contacto(op, momento=None):
    """Primera gestión del vendedor sobre el lead (para el SLA). Solo la primera vez."""
    if op is None:
        return
    Oportunidad.objects.filter(pk=op.pk, primer_contacto_at__isnull=True).update(
        primer_contacto_at=momento or timezone.now())


def registrar_intento(op, usuario, canal='llamada', resultado='sin_respuesta', nota='', crear_actividad=True):
    """
    Registra un intento de contacto. Si el prospecto estaba en la primera etapa pasa solo a la
    siguiente ("En gestión"). Devuelve True si se llegó al máximo de intentos del embudo.
    """
    ahora = timezone.now()
    Oportunidad.objects.filter(pk=op.pk).update(
        intentos_contacto=F('intentos_contacto') + 1, ultimo_intento_at=ahora, ultima_actividad_at=ahora,
    )
    op.refresh_from_db(fields=['intentos_contacto', 'ultimo_intento_at', 'ultima_actividad_at'])
    marcar_primer_contacto(op, ahora)
    etiquetas = {'sin_respuesta': 'sin respuesta', 'ocupado': 'ocupado', 'buzon': 'buzón de voz',
                 'numero_erroneo': 'número erróneo', 'contactado': 'contactado'}
    if crear_actividad:
        Actividad.objects.create(
            contacto_id=op.contacto_id, oportunidad=op, tipo=Actividad.TIPO_INTENTO, usuario=usuario,
            texto=f'Intento #{op.intentos_contacto} por {canal}: {etiquetas.get(resultado, resultado)}.'
                  + (f'\n{nota}' if nota else ''),
            datos={'canal': canal, 'resultado': resultado},
        )
    avanzar_desde_inicial(op, usuario)
    return op.llego_max_intentos


def avanzar_desde_inicial(op, usuario=None):
    """Primer gestión sobre un prospecto nuevo → pasa automáticamente a la segunda etapa."""
    op.refresh_from_db(fields=['etapa', 'estado'])
    if op.estado != Oportunidad.ESTADO_ABIERTA:
        return None
    inicial = op.embudo.etapa_inicial
    if inicial and op.etapa_id == inicial.pk:
        sig = siguiente_etapa(op)
        if sig:
            return mover_etapa(op, sig, usuario=usuario, nota='Avance automático: primera gestión', automatico=True)
    return None


def agregar_nota(op_o_contacto, usuario, texto):
    from apps.users.models import User
    from apps.users.services import notificar
    import re
    op = op_o_contacto if isinstance(op_o_contacto, Oportunidad) else None
    contacto = op.contacto if op else op_o_contacto
    act = Actividad.objects.create(contacto=contacto, oportunidad=op, tipo=Actividad.TIPO_NOTA, usuario=usuario,
                                   texto=texto.strip())
    if op:
        tocar(op)
    menciones = set(re.findall(r'@([\w.\-]+)', texto))
    if menciones:
        url = op.get_absolute_url() if op else contacto.get_absolute_url()
        for u in User.objects.filter(username__in=menciones, is_active=True).exclude(pk=usuario.pk):
            notificar(u, 'mencion', f'{usuario.display_name} te mencionó', texto[:200], url)
    return act


# ═══════════════════════════════════════════════════════════════════════════
# Tareas
# ═══════════════════════════════════════════════════════════════════════════

def _clave_tareas(user_id):
    return f'tareas_hoy_{user_id}'


def invalidar_tareas(user):
    if user is not None:
        cache.delete(_clave_tareas(user.pk if hasattr(user, 'pk') else user))


def contar_tareas_hoy(user):
    key = _clave_tareas(user.pk)
    n = cache.get(key)
    if n is None:
        fin_dia = timezone.localtime().replace(hour=23, minute=59, second=59)
        n = Tarea.objects.filter(asignado_a=user, estado=Tarea.ESTADO_PENDIENTE, vence_at__lte=fin_dia).count()
        cache.set(key, n, 120)
    return n


def completar_tarea(tarea, usuario, resultado=''):
    tarea.estado, tarea.completada_at, tarea.resultado = Tarea.ESTADO_COMPLETADA, timezone.now(), resultado
    tarea.save(update_fields=['estado', 'completada_at', 'resultado'])
    if tarea.contacto_id:
        Actividad.objects.create(
            contacto_id=tarea.contacto_id, oportunidad=tarea.oportunidad, tipo=Actividad.TIPO_TAREA, usuario=usuario,
            texto=f'Tarea completada: {tarea.titulo}' + (f'\n{resultado}' if resultado else ''),
        )
    if tarea.oportunidad_id:
        tocar(tarea.oportunidad)
    invalidar_tareas(tarea.asignado_a)


def crear_tarea(usuario, asignado_a, titulo, vence_at, oportunidad=None, contacto=None, tipo=Tarea.TIPO_LLAMADA,
                descripcion='', prioridad=Tarea.PRIORIDAD_NORMAL):
    if oportunidad and not contacto:
        contacto = oportunidad.contacto
    tarea = Tarea.objects.create(
        oportunidad=oportunidad, contacto=contacto, asignado_a=asignado_a or usuario, creada_por=usuario,
        tipo=tipo, titulo=titulo, descripcion=descripcion, vence_at=vence_at, prioridad=prioridad,
    )
    invalidar_tareas(tarea.asignado_a)
    if contacto:
        responsable = asignado_a or usuario
        para = f' · para {responsable.display_name}' if responsable and responsable != usuario else ''
        registrar_tarea(tarea, usuario, f'Tarea agendada: {titulo}{para} · vence {timezone.localtime(vence_at):%d/%m %H:%M}')
    return tarea


def registrar_tarea(tarea, usuario, texto):
    if tarea.contacto_id:
        Actividad.objects.create(contacto_id=tarea.contacto_id, oportunidad=tarea.oportunidad, tipo=Actividad.TIPO_TAREA,
                                 usuario=usuario, texto=texto)


# ═══════════════════════════════════════════════════════════════════════════
# Cola de trabajo del agente ("¿a quién llamo ahora?")
# ═══════════════════════════════════════════════════════════════════════════

def cola_de_trabajo(user, limite=30):
    """Prioriza: tareas vencidas / de hoy → prospectos nuevos sin gestionar → sin actividad hace días."""
    ahora = timezone.now()
    fin_dia = timezone.localtime().replace(hour=23, minute=59, second=59)
    tareas = list(
        Tarea.objects.filter(asignado_a=user, estado=Tarea.ESTADO_PENDIENTE, vence_at__lte=fin_dia)
        .select_related('oportunidad__contacto', 'oportunidad__etapa', 'contacto').order_by('vence_at')[:limite]
    )
    con_tarea = {t.oportunidad_id for t in tareas if t.oportunidad_id}
    base = (Oportunidad.objects.filter(agente=user, estado=Oportunidad.ESTADO_ABIERTA)
            .exclude(pk__in=con_tarea).select_related('contacto', 'etapa', 'embudo'))
    # Dentro de cada grupo, primero los de mayor puntaje (lead scoring)
    prioritarias = list(base.filter(prioritaria=True).order_by('-ultima_actividad_at')[:limite])
    base = base.exclude(prioritaria=True)
    nuevos = list(base.filter(intentos_contacto=0).order_by('-puntaje', 'asignada_at', 'created_at')[:limite])
    frios = list(base.filter(intentos_contacto__gt=0, ultima_actividad_at__lt=ahora - timedelta(days=1))
                 .order_by('-puntaje', 'ultima_actividad_at')[:limite])
    return {'tareas': tareas, 'prioritarias': prioritarias, 'nuevos': nuevos, 'frios': frios}


def siguiente_prospecto(user):
    """El próximo prospecto a trabajar (para el botón 'Siguiente')."""
    cola = cola_de_trabajo(user, limite=1)
    if cola['prioritarias']:
        return cola['prioritarias'][0]
    if cola['tareas'] and cola['tareas'][0].oportunidad:
        return cola['tareas'][0].oportunidad
    if cola['nuevos']:
        return cola['nuevos'][0]
    if cola['frios']:
        return cola['frios'][0]
    return None


def filtro_busqueda_contacto(q, prefijo=''):
    """Búsqueda libre por nombre, email, DNI o teléfono (acepta cualquier formato de número)."""
    digitos = ''.join(c for c in q if c.isdigit())
    filtro = Q(**{f'{prefijo}nombre__icontains': q}) | Q(**{f'{prefijo}email__icontains': q})
    if digitos:
        filtro |= Q(**{f'{prefijo}dni': digitos})
    if len(digitos) >= 6:
        filtro |= Q(**{f'{prefijo}telefono__in': variantes_telefono(q)})
        filtro |= Q(**{f'{prefijo}telefono__endswith': digitos[-8:]})
    return filtro


CAMPOS_FECHA_FILTRO = [
    ('ingreso', 'Ingreso', 'created_at'),
    ('etapa', 'Entró a la etapa actual', 'etapa_desde'),
    ('actividad', 'Última actividad', 'ultima_actividad_at'),
    ('asignacion', 'Asignación', 'asignada_at'),
    ('cierre', 'Cierre', 'cerrada_at'),
    ('recontacto', 'Recontacto / reactivación', 'proximo_contacto_at'),
]


def valores_filtro(params, clave):
    """Valores de un filtro que admite varios (QueryDict.getlist o dict simple), sin vacíos."""
    if hasattr(params, 'getlist'):
        valores = params.getlist(clave)
    else:
        v = params.get(clave)
        valores = v if isinstance(v, (list, tuple)) else [v]
    return [str(v) for v in valores if v not in (None, '')]


def filtrar_oportunidades(qs, params, user):
    """Filtros compartidos por tablero, lista, exportación y acciones masivas."""
    q = (params.get('q') or '').strip()
    if q:
        qs = qs.filter(filtro_busqueda_contacto(q, prefijo='contacto__'))
    agentes = valores_filtro(params, 'agente')
    if agentes:
        filtro = Q(agente_id__in=[a for a in agentes if a.isdigit()])
        if 'ninguno' in agentes:
            filtro |= Q(agente__isnull=True)
        qs = qs.filter(filtro)
    pautas = valores_filtro(params, 'pauta')
    if pautas:
        filtro = Q(pauta_id__in=[p for p in pautas if p.isdigit()])
        if 'ninguna' in pautas:
            filtro |= Q(pauta__isnull=True)
        qs = qs.filter(filtro)
    if params.get('origen_pauta'):
        qs = qs.filter(origen_pauta=params['origen_pauta'])
    for clave, campo in (('etapa', 'etapa_id__in'), ('estado', 'estado__in'), ('origen', 'origen__in'),
                         ('tipificacion', 'tipificacion_id__in'), ('lote', 'lote_id__in')):
        valores = valores_filtro(params, clave)
        if valores:
            qs = qs.filter(**{campo: valores})
    etiquetas = valores_filtro(params, 'etiqueta')
    if etiquetas:
        qs = qs.filter(contacto__etiquetas__id__in=etiquetas).distinct()
    campo_fecha = dict((k, c) for k, _, c in CAMPOS_FECHA_FILTRO).get(params.get('fecha') or 'ingreso', 'created_at')
    if params.get('desde'):
        qs = qs.filter(**{f'{campo_fecha}__date__gte': params['desde']})
    if params.get('hasta'):
        qs = qs.filter(**{f'{campo_fecha}__date__lte': params['hasta']})
    for clave, lookup in (('intentos_min', 'intentos_contacto__gte'), ('intentos_max', 'intentos_contacto__lte'),
                          ('ingresos_min', 'ingresos__gte'), ('puntaje_min', 'puntaje__gte')):
        if str(params.get(clave) or '').isdigit():
            qs = qs.filter(**{lookup: int(params[clave])})
    if params.get('sla') == 'vencido':
        qs = qs.filter(q_sla_vencido())
    if params.get('sin_actividad'):
        try:
            dias = int(params['sin_actividad'])
            qs = qs.filter(ultima_actividad_at__lt=timezone.now() - timedelta(days=dias))
        except ValueError:
            pass
    if params.get('mias') and user:
        qs = qs.filter(agente=user)
    if params.get('prioritaria'):
        qs = qs.filter(prioritaria=True)
    if params.get('vencida'):
        qs = qs.filter(vencida_at__isnull=False)
    if params.get('sin_calificar'):
        from django.db.models import Exists, OuterRef
        from apps.telefonia.models import Llamada
        from apps.telefonia.services import q_sin_calificar
        qs = qs.filter(Exists(Llamada.objects.filter(q_sin_calificar(), oportunidad=OuterRef('pk'))))
    filtros_cp = {k[3:]: v for k, v in params.items() if k.startswith('cp_') and v not in (None, '')}
    if filtros_cp:
        from .models import CampoPersonalizado
        validos = set(CampoPersonalizado.objects.filter(slug__in=filtros_cp).values_list('slug', flat=True))
    for slug, valor in filtros_cp.items():
        if slug in validos:
            if valor in ('true', 'false'):
                qs = qs.filter(**{f'contacto__datos_extra__{slug}': valor == 'true'})
            else:
                qs = qs.filter(**{f'contacto__datos_extra__{slug}__icontains': valor})
    return qs
