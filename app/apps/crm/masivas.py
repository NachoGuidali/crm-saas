"""
Acciones masivas sobre oportunidades (lista con filtros → seleccionar algunas o todas → aplicar).

Las mismas reglas que en la ficha: cada oportunidad pasa por los servicios del CRM (historial, tareas,
automatizaciones, datos obligatorios por etapa). Lotes chicos se aplican en el momento; los grandes, en segundo
plano (Celery) con aviso al terminar.
"""
import logging

from django.core.exceptions import PermissionDenied

from . import services as crm
from .models import Actividad, Etapa, Etiqueta, Oportunidad, Tipificacion

logger = logging.getLogger('apps.crm')

LIMITE_SINCRONICO = 300      # más que esto → segundo plano
LIMITE_TOTAL = 20000         # tope de seguridad por operación

ACCIONES = {
    'reasignar': 'Reasignar',
    'repartir': 'Repartir entre agentes',
    'mover': 'Mover de etapa',
    'cerrar': 'Cerrar / postergar con tipificación',
    'pausar': 'Pausar',
    'reanudar': 'Reactivar',
    'etiqueta': 'Agregar etiqueta',
    'tarea': 'Crear tarea',
    'prioridad': 'Marcar como prioridad',
    'campania': 'Cargar al discador',
    'eliminar': 'Eliminar',
}
PERMISOS = {'reasignar': 'reasignar', 'repartir': 'reasignar', 'campania': 'discador', 'eliminar': 'eliminar'}


def validar(user, accion, datos):
    """Valida permisos y datos antes de encolar/aplicar. Lanza ErrorNegocio / PermissionDenied."""
    if accion not in ACCIONES:
        raise crm.ErrorNegocio('Acción desconocida.')
    permiso = PERMISOS.get(accion)
    if permiso and not user.tiene_permiso(permiso):
        raise PermissionDenied
    if accion == 'reasignar' and not datos.get('destino_agente'):
        raise crm.ErrorNegocio('Elegí a quién reasignar.')
    if accion == 'repartir' and len(datos.get('destino_agentes') or []) < 1:
        raise crm.ErrorNegocio('Elegí al menos un agente para repartir.')
    if accion == 'mover' and not datos.get('destino_etapa'):
        raise crm.ErrorNegocio('Elegí la etapa.')
    if accion == 'cerrar':
        tip = Tipificacion.objects.filter(pk=datos.get('tipificacion') or 0, activa=True).first()
        if tip is None:
            raise crm.ErrorNegocio('Elegí la tipificación.')
        if tip.es_postergacion and not datos.get('fecha'):
            raise crm.ErrorNegocio('Para postergar indicá la fecha de recontacto.')
        if tip.requiere_nota and not (datos.get('nota') or '').strip():
            raise crm.ErrorNegocio(f'La tipificación "{tip}" requiere una nota.')
    if accion == 'etiqueta' and not datos.get('destino_etiqueta'):
        raise crm.ErrorNegocio('Elegí la etiqueta.')
    if accion == 'tarea' and not (datos.get('titulo') or '').strip():
        raise crm.ErrorNegocio('Escribí el título de la tarea (ej. "Llamar por la campaña Día de la Madre").')
    if accion == 'campania' and not datos.get('campania'):
        raise crm.ErrorNegocio('Elegí la campaña.')


def ejecutar(user, accion, ids, datos):
    """Aplica la acción. Devuelve {'hechos': n, 'omitidos': n, 'errores': [texto…]}."""
    from apps.users.models import User
    from .views import _fecha_local

    validar(user, accion, datos)
    ops = list(Oportunidad.objects.filter(pk__in=ids).select_related('contacto', 'embudo', 'etapa', 'agente')
               .order_by('pk')[:LIMITE_TOTAL])
    hechos, errores = 0, []

    def registrar_error(op, e):
        errores.append(f'{op.contacto}: {e}')

    if accion == 'reasignar':
        if datos['destino_agente'] == 'auto':
            for op in ops:
                if op.activa:
                    Oportunidad.objects.filter(pk=op.pk).update(agente=None)
                    op.agente = None
                    if crm.asignar_oportunidad(op, forzar_horario=True):
                        hechos += 1
        else:
            agente = User.objects.get(pk=datos['destino_agente'], is_active=True)
            for op in ops:
                if op.agente_id != agente.pk:
                    crm.reasignar(op, agente, user, nota=datos.get('nota', ''))
                    hechos += 1
    elif accion == 'repartir':
        agentes = list(User.objects.filter(pk__in=datos['destino_agentes'], is_active=True).order_by('pk'))
        if not agentes:
            raise crm.ErrorNegocio('Ninguno de los agentes elegidos está activo.')
        for i, op in enumerate(ops):
            agente = agentes[i % len(agentes)]  # en partes iguales, en orden
            if op.agente_id != agente.pk:
                crm.reasignar(op, agente, user, nota='reparto masivo')
            hechos += 1
    elif accion == 'mover':
        etapa = Etapa.objects.get(pk=datos['destino_etapa'], tipo=Etapa.TIPO_NORMAL)
        for op in ops:
            if op.embudo_id != etapa.embudo_id:
                registrar_error(op, 'es de otro embudo')
                continue
            try:
                if crm.mover_etapa(op, etapa, user, puede_reabrir=user.tiene_permiso('reabrir'), nota=datos.get('nota', '')):
                    hechos += 1
            except crm.ErrorNegocio as e:
                registrar_error(op, e)
    elif accion == 'cerrar':
        tip = Tipificacion.objects.get(pk=datos['tipificacion'])
        fecha = _fecha_local(datos.get('fecha'))
        for op in ops:
            try:
                if tip.es_postergacion:
                    crm.postergar(op, user, fecha, tip, datos.get('nota', ''))
                else:
                    etapa = op.embudo.etapa_ganado if tip.resultado == Tipificacion.RESULTADO_VENTA else op.embudo.etapa_perdido
                    if etapa is None or (tip.embudo_id and tip.embudo_id != op.embudo_id):
                        raise crm.ErrorNegocio('la tipificación no corresponde a su embudo')
                    crm.mover_etapa(op, etapa, user, tipificacion=tip, nota=datos.get('nota', ''),
                                    puede_reabrir=user.tiene_permiso('reabrir'))
                hechos += 1
            except crm.ErrorNegocio as e:
                registrar_error(op, e)
    elif accion == 'pausar':
        for op in ops:
            try:
                crm.pausar(op, user, motivo=datos.get('motivo') or 'Pausa masiva', hasta=_fecha_local(datos.get('hasta')))
                hechos += 1
            except crm.ErrorNegocio as e:
                registrar_error(op, e)
    elif accion == 'reanudar':
        hechos = sum(1 for op in ops if crm.reanudar(op, user))
    elif accion == 'etiqueta':
        etiqueta = Etiqueta.objects.get(pk=datos['destino_etiqueta'])
        ya = set(etiqueta.contactos.filter(pk__in={op.contacto_id for op in ops}).values_list('pk', flat=True))
        nuevos = {op.contacto_id: op for op in ops if op.contacto_id not in ya}
        etiqueta.contactos.add(*nuevos)
        Actividad.objects.bulk_create([Actividad(contacto_id=cid, oportunidad=op, tipo=Actividad.TIPO_CAMBIO, usuario=user,
                                                 texto=f'Etiqueta agregada: {etiqueta}')
                                       for cid, op in nuevos.items()], batch_size=500)
        hechos = len(ops)
    elif accion == 'tarea':
        from django.utils import timezone
        from .models import Tarea
        vence = _fecha_local(datos.get('vence')) or timezone.localtime().replace(hour=23, minute=0, second=0, microsecond=0)
        tipo = datos.get('tipo_tarea') if datos.get('tipo_tarea') in dict(Tarea.TIPO_CHOICES) else Tarea.TIPO_LLAMADA
        para_mi = datos.get('para') == 'yo'
        for op in ops:
            if not op.activa:
                registrar_error(op, 'está cerrada')
                continue
            crm.crear_tarea(user, user if para_mi else (op.agente or user), datos['titulo'].strip()[:200], vence,
                            oportunidad=op, tipo=tipo, descripcion=datos.get('nota', ''))
            hechos += 1
    elif accion == 'prioridad':
        for op in ops:
            if op.activa and crm.marcar_prioridad(op, datos.get('motivo') or f'Marcada por {user.display_name}',
                                                  usuario=user):
                hechos += 1
    elif accion == 'campania':
        from apps.telefonia.models import CampaniaDiscado
        from apps.telefonia.services import cargar_en_campania
        campania = CampaniaDiscado.objects.get(pk=datos['campania'])
        hechos, omitidos = cargar_en_campania(campania, Oportunidad.objects.filter(pk__in=[o.pk for o in ops]), usuario=user)
        if omitidos:
            errores.append(f'{omitidos} omitidos (ya estaban cargados, sin teléfono o "no contactar")')
    elif accion == 'eliminar':
        hechos = Oportunidad.objects.filter(pk__in=[o.pk for o in ops]).delete()[1].get('crm.Oportunidad', 0)

    omitidos = len(ops) - hechos if accion not in ('campania', 'eliminar', 'etiqueta') else 0
    logger.info('Acción masiva %s por %s: %d hechas, %d errores', accion, user, hechos, len(errores))
    return {'hechos': hechos, 'omitidos': max(omitidos, 0), 'errores': errores}


def resumen(accion, resultado):
    texto = f'{ACCIONES.get(accion, accion)}: {resultado["hechos"]} oportunidad{"es" if resultado["hechos"] != 1 else ""}'
    if resultado['errores']:
        texto += f' · {len(resultado["errores"])} no se pudieron'
    return texto
