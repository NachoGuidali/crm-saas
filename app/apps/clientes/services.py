"""Alta, suspensión y configuración inicial de clientes del SaaS."""
import logging
import re
import secrets

from django.core.exceptions import ValidationError
from django.db import transaction
from django_tenants.utils import tenant_context

from core.tenancy import dominio_para

from .models import SUBDOMINIOS_RESERVADOS, Cliente, Dominio

logger = logging.getLogger('apps.clientes')

_SUBDOMINIO_RE = re.compile(r'^[a-z][a-z0-9-]{1,38}[a-z0-9]$')


def validar_subdominio(subdominio):
    subdominio = (subdominio or '').strip().lower()
    if not _SUBDOMINIO_RE.match(subdominio) or '--' in subdominio:
        raise ValidationError('El subdominio debe tener entre 3 y 40 caracteres: letras minúsculas, números y '
                              'guiones (empieza con letra, no termina en guion).')
    if subdominio in SUBDOMINIOS_RESERVADOS:
        raise ValidationError(f'"{subdominio}" está reservado para la plataforma.')
    if Cliente.objects.filter(schema_name=schema_para(subdominio)).exists() or \
            Dominio.objects.filter(domain=dominio_para(subdominio)).exists():
        raise ValidationError(f'Ya existe un cliente con el subdominio "{subdominio}".')
    return subdominio


def schema_para(subdominio):
    # Los schemas de Postgres no admiten guiones sin comillas: acme-sa → c_acme_sa
    return 'c_' + subdominio.replace('-', '_')


def crear_cliente(*, nombre, subdominio, admin_email, admin_nombre='', admin_usuario='admin',
                  admin_password=None, plan=None, estado=Cliente.ESTADO_PRUEBA, **extra):
    """
    Crea el cliente con su schema (crea y migra todas las tablas del CRM), su subdominio,
    la configuración inicial y el usuario administrador.

    Devuelve (cliente, password). Si no se pasó contraseña se genera una temporal que se pide
    cambiar al primer ingreso.
    """
    subdominio = validar_subdominio(subdominio)
    with transaction.atomic():
        cliente = Cliente(schema_name=schema_para(subdominio), nombre=nombre, plan=plan, estado=estado,
                          contacto_email=extra.pop('contacto_email', '') or admin_email, **extra)
        cliente.save()  # acá django-tenants crea el schema y corre las migraciones
        Dominio.objects.create(tenant=cliente, domain=dominio_para(subdominio), is_primary=True)

    password_temporal = not admin_password
    password = admin_password or secrets.token_urlsafe(9)
    with tenant_context(cliente):
        cargar_configuracion_inicial()
        from apps.users.models import User
        nombre_partes = (admin_nombre or 'Administrador').split(' ', 1)
        User.objects.create_user(
            username=admin_usuario, email=admin_email, password=password, rol=User.ROL_ADMIN,
            first_name=nombre_partes[0], last_name=nombre_partes[1] if len(nombre_partes) > 1 else '',
            debe_cambiar_password=password_temporal,
        )
    logger.info('Cliente creado: %s (%s)', cliente, cliente.schema_name)
    return cliente, password


# ── Configuración inicial (genérica, para cualquier rubro) ─────────────────

ETAPAS = [
    # nombre, color, tipo, marca_contacto_efectivo, descripción
    ('Nuevo', '#3B7EF6', 'normal', False, 'Ingresó y todavía no se contactó.'),
    ('En gestión', '#06B6D4', 'normal', False, 'Se está intentando contactar por llamada y/o WhatsApp.'),
    ('Contactado', '#7C5CFC', 'normal', True, 'Se logró hablar y se presentó la propuesta.'),
    ('Propuesta enviada', '#F59E0B', 'normal', True, 'Recibió la información o cotización y está evaluando.'),
    ('Negociación', '#F97316', 'normal', True, 'Manejo de objeciones y ajuste de condiciones.'),
    ('Ganado', '#22C97A', 'ganado', True, 'Venta concretada.'),
    ('Perdido', '#EF4444', 'perdido', False, 'Cierre con tipificación obligatoria del motivo.'),
]

TIPIFICACIONES = [
    # resultado, categoría, nombre, descripción, acción, requiere_nota
    ('venta', 'Venta', 'Venta directa', 'Cierre logrado en el primer contacto efectivo.', '', False),
    ('venta', 'Venta', 'Venta con seguimiento', 'Cierre logrado luego de más de un contacto.', '', False),
    ('venta', 'Venta', 'Venta con condición especial', 'Cierre con descuento o condiciones especiales.', '', True),
    ('no_venta', 'Sin contacto efectivo', 'Sin respuesta', 'No atiende ni responde tras los intentos estándar.', '', False),
    ('no_venta', 'Sin contacto efectivo', 'Dato de contacto erróneo', 'Teléfono o email inválido o inexistente.',
     'dato_erroneo', False),
    ('no_venta', 'Sin contacto efectivo', 'Solicita no ser contactado', 'Pide no recibir más comunicaciones.',
     'no_contactar', False),
    ('no_venta', 'Sin interés', 'No le interesa', 'Manifiesta desinterés sin otro motivo específico.', '', False),
    ('no_venta', 'Sin interés', 'Eligió a la competencia', 'Contrató otro proveedor.', '', False),
    ('no_venta', 'Motivo económico', 'Precio fuera de presupuesto', 'El valor no se ajusta a lo que puede pagar.', '', False),
    ('no_venta', 'Postergado (no definitivo)', 'Pide ser recontactado más adelante',
     'No se pierde: se pausa y se agenda una tarea de reactivación para la fecha indicada.', 'postergar', False),
]

ROLES = [
    ('Agente de ventas', 'Trabaja solo sus prospectos asignados.', []),
    ('Supervisor de ventas', 'Ve todo el embudo, reasigna, importa bases y gestiona el discador.',
     ['ver_todo', 'supervision', 'reportes', 'reasignar', 'importar', 'exportar', 'reabrir', 'discador']),
    ('Comercial / Marketing', 'Mide resultados y administra mensajes y automatizaciones.',
     ['ver_todo', 'reportes', 'exportar', 'plantillas', 'automatizaciones', 'embudos', 'campos', 'pautas', 'difusiones']),
]

RESPUESTAS_RAPIDAS = [
    ('saludo', 'Saludo', '¡Hola {primer_nombre}! Soy {agente}. ¿Cómo estás?'),
    ('llamar', 'Pedir horario para llamar', '{primer_nombre}, ¿en qué horario te queda cómodo que te llame?'),
    ('gracias', 'Cierre cordial', '¡Gracias {primer_nombre}! Cualquier consulta, escribime por acá.'),
]


@transaction.atomic
def cargar_configuracion_inicial():
    """Embudo de ventas base, tipificaciones, roles y respuestas rápidas. Idempotente. Corre en el schema actual."""
    from apps.crm.models import Embudo, Etapa, Tipificacion
    from apps.telefonia.models import ConfigAnura
    from apps.users.models import RolPersonalizado
    from apps.whatsapp.models import RespuestaRapida

    embudo, _ = Embudo.objects.get_or_create(
        nombre='Ventas', defaults={'descripcion': 'Embudo de ventas principal.', 'color': '#3B7EF6',
                                   'dias_habiles': [0, 1, 2, 3, 4]},
    )
    for orden, (nombre, color, tipo, efectivo, desc) in enumerate(ETAPAS, start=1):
        Etapa.objects.get_or_create(embudo=embudo, nombre=nombre, defaults={
            'orden': orden, 'color': color, 'tipo': tipo, 'marca_contacto_efectivo': efectivo, 'descripcion': desc,
        })
    for orden, (resultado, cat, nombre, desc, accion, nota) in enumerate(TIPIFICACIONES, start=1):
        Tipificacion.objects.get_or_create(embudo=embudo, nombre=nombre, defaults={
            'resultado': resultado, 'categoria': cat, 'descripcion': desc, 'accion': accion,
            'requiere_nota': nota, 'orden': orden,
        })
    for nombre, desc, permisos in ROLES:
        RolPersonalizado.objects.get_or_create(nombre=nombre, defaults={'descripcion': desc, 'permisos': permisos})
    for atajo, titulo, texto in RESPUESTAS_RAPIDAS:
        RespuestaRapida.objects.get_or_create(atajo=atajo, defaults={'titulo': titulo, 'texto': texto})
    config, _ = ConfigAnura.objects.get_or_create(pk=1)
    if not config.embudo_entrantes_id:
        config.embudo_entrantes = embudo
        config.save()
    return embudo
