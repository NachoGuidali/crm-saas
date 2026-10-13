"""
Carga la configuración inicial del CRM (idempotente: se puede correr varias veces).

Incluye el circuito "Doctor Flex – Segmento Prospecto" tal cual la propuesta de Comercial:
etapas, tipificaciones de Venta / No Venta y los mensajes automáticos por etapa
(cargados DESACTIVADOS hasta que Marketing apruebe los textos).
"""
import os
import secrets

from django.core.management.base import BaseCommand
from django.db import transaction

ETAPAS_DOCTOR_FLEX = [
    # nombre, color, tipo, marca_contacto_efectivo, descripción
    ('Prospecto (Nuevo)', '#3B7EF6', 'normal', False, 'Ingresó desde la base propia del centro médico.'),
    ('En gestión', '#06B6D4', 'normal', False, 'El agente intenta contactar por llamada y/o WhatsApp.'),
    ('Contacto efectivo', '#7C5CFC', 'normal', True, 'Se logró hablar y se le presentó Doctor Flex.'),
    ('Propuesta enviada', '#F59E0B', 'normal', True, 'Recibió información/cotización y está evaluando.'),
    ('Negociación', '#F97316', 'normal', True, 'Manejo de objeciones y ajuste de condiciones.'),
    ('Venta (Ganado)', '#22C97A', 'ganado', True, 'Primer pago acreditado: se dispara el alta del cápita.'),
    ('No Venta (Perdido)', '#EF4444', 'perdido', False, 'Cierre con tipificación obligatoria del motivo.'),
]

TIPIFICACIONES = [
    # resultado, categoría, nombre, descripción, acción, requiere_nota
    ('venta', 'Venta', 'Venta directa', 'Cierre logrado en el primer contacto efectivo, sin objeciones relevantes.', '', False),
    ('venta', 'Venta', 'Venta con seguimiento', 'Cierre logrado luego de más de un contacto (llamada y/o WhatsApp).', '', False),
    ('venta', 'Venta', 'Venta con condición especial', 'Cierre con descuento, promoción o ajuste de condiciones aprobado.', '', True),
    ('no_venta', 'Sin contacto efectivo', 'Sin respuesta', 'No atiende llamadas ni responde WhatsApp tras los intentos definidos como estándar.', '', False),
    ('no_venta', 'Sin contacto efectivo', 'Dato de contacto erróneo', 'Teléfono o email inválido, dado de baja o inexistente.', 'dato_erroneo', False),
    ('no_venta', 'Sin contacto efectivo', 'Solicita no ser contactado', 'El prospecto pide expresamente no recibir más comunicaciones.', 'no_contactar', False),
    ('no_venta', 'Sin interés', 'No le interesa el producto', 'Manifiesta desinterés en Doctor Flex sin otro motivo específico.', '', False),
    ('no_venta', 'Sin interés', 'Ya tiene otra cobertura', 'Cuenta con obra social o prepaga y no evalúa un cambio.', '', False),
    ('no_venta', 'Sin interés', 'Prefiere pago por consulta', 'Prefiere seguir atendiéndose de forma particular, sin plan.', '', False),
    ('no_venta', 'Motivo económico', 'Cuota fuera de presupuesto', 'El valor del plan no se ajusta a lo que puede pagar.', '', False),
    ('no_venta', 'Motivo económico', 'No puede afrontar la modalidad de pago', 'El medio o la frecuencia de pago ofrecida no le resulta viable.', '', False),
    ('no_venta', 'No elegible', 'Fuera de zona de cobertura', 'Reside en una zona no cubierta por Doctor Flex.', '', False),
    ('no_venta', 'No elegible', 'Preexistencia no cubierta', 'Presenta una condición que el plan no cubre.', '', False),
    ('no_venta', 'No elegible', 'Ya es cliente DoctoRed', 'Registro duplicado: ya cuenta con un plan activo.', '', False),
    ('no_venta', 'Postergado (no definitivo)', 'Pide ser recontactado más adelante',
     'No se pierde: se pausa y se agenda una tarea de reactivación para la fecha indicada.', 'postergar', False),
]

PLANTILLAS = [
    # nombre, cuerpo, variables
    ('doctorflex_bienvenida',
     '¡Hola {{1}}! 👋 Te escribe {{2}} de DoctoRed. Como te atendiste en nuestro centro médico, queremos '
     'contarte sobre Doctor Flex, nuestro plan de salud pensado para vos. ¿Te puedo llamar para contarte los detalles?',
     ['primer_nombre', 'agente']),
    ('doctorflex_seguimiento',
     'Hola {{1}}, ¿pudiste ver la propuesta de Doctor Flex que te enviamos? Cualquier duda sobre coberturas '
     'o cuotas, respondé este mensaje y {{2}} te ayuda.',
     ['primer_nombre', 'agente']),
    ('doctorflex_confirmacion',
     '¡Bienvenido/a a Doctor Flex, {{1}}! 🎉 Ya registramos tu primer pago. En los próximos días te llega la '
     'información de tu alta y cómo usar tu plan. Ante cualquier consulta, escribinos por acá.',
     ['primer_nombre']),
    ('doctorflex_despedida',
     'Gracias por tu tiempo, {{1}}. Si más adelante querés conocer Doctor Flex, escribinos por este medio: '
     'vamos a estar para ayudarte. ¡Que estés muy bien!',
     ['primer_nombre']),
]

AUTOMATIZACIONES = [
    # etapa, nombre, plantilla, demora
    ('Prospecto (Nuevo)', 'Mensaje de bienvenida', 'doctorflex_bienvenida', 0),
    ('Propuesta enviada', 'Seguimiento de la propuesta', 'doctorflex_seguimiento', 0),
    ('Venta (Ganado)', 'Confirmación y próximos pasos', 'doctorflex_confirmacion', 0),
    ('No Venta (Perdido)', 'Despedida cordial', 'doctorflex_despedida', 0),
]

ROLES = [
    ('Agente de ventas', 'Trabaja solo sus prospectos asignados.', []),
    ('Supervisor de ventas', 'Ve todo el embudo, reasigna, importa bases y gestiona el discador.',
     ['ver_todo', 'supervision', 'reportes', 'reasignar', 'importar', 'exportar', 'reabrir', 'discador']),
    ('Comercial / Marketing', 'Mide resultados y administra mensajes y automatizaciones.',
     ['ver_todo', 'reportes', 'exportar', 'plantillas', 'automatizaciones', 'embudos', 'campos', 'pautas', 'difusiones']),
]

RESPUESTAS_RAPIDAS = [
    ('saludo', 'Saludo', '¡Hola {primer_nombre}! Soy {agente}, de DoctoRed. ¿Cómo estás?'),
    ('llamar', 'Pedir horario para llamar', '{primer_nombre}, ¿en qué horario te queda cómodo que te llame?'),
    ('gracias', 'Cierre cordial', '¡Gracias {primer_nombre}! Cualquier consulta, escribime por acá.'),
]


class Command(BaseCommand):
    help = 'Crea la configuración inicial: embudo Doctor Flex, tipificaciones, automatizaciones, roles y admin.'

    def add_arguments(self, parser):
        parser.add_argument('--sin-admin', action='store_true', help='No crear el usuario administrador.')

    @transaction.atomic
    def handle(self, *args, **opts):
        from apps.automatizaciones.models import AccionEtapa
        from apps.crm.models import Embudo, Etapa, Tipificacion
        from apps.telefonia.models import ConfigAnura
        from apps.users.models import RolPersonalizado, User
        from apps.whatsapp.models import Plantilla, RespuestaRapida

        embudo, creado = Embudo.objects.get_or_create(
            nombre='Doctor Flex — Prospectos',
            defaults={'descripcion': 'Segmento Prospecto: personas que ya se atendieron en DoctoRed.',
                      'color': '#22C97A', 'dias_habiles': [0, 1, 2, 3, 4]},
        )
        for orden, (nombre, color, tipo, efectivo, desc) in enumerate(ETAPAS_DOCTOR_FLEX, start=1):
            Etapa.objects.get_or_create(embudo=embudo, nombre=nombre, defaults={
                'orden': orden, 'color': color, 'tipo': tipo, 'marca_contacto_efectivo': efectivo, 'descripcion': desc,
            })
        for orden, (resultado, cat, nombre, desc, accion, nota) in enumerate(TIPIFICACIONES, start=1):
            Tipificacion.objects.get_or_create(embudo=embudo, nombre=nombre, defaults={
                'resultado': resultado, 'categoria': cat, 'descripcion': desc, 'accion': accion,
                'requiere_nota': nota, 'orden': orden,
            })
        plantillas = {}
        for nombre, cuerpo, variables in PLANTILLAS:
            plantillas[nombre], _ = Plantilla.objects.get_or_create(nombre=nombre, defaults={
                'cuerpo': cuerpo, 'variables': variables, 'categoria': 'MARKETING' if 'bienvenida' in nombre else 'UTILITY',
            })
        for orden, (etapa_nombre, nombre, plantilla, demora) in enumerate(AUTOMATIZACIONES):
            etapa = Etapa.objects.get(embudo=embudo, nombre=etapa_nombre)
            AccionEtapa.objects.get_or_create(embudo=embudo, etapa=etapa, nombre=nombre, defaults={
                'tipo': AccionEtapa.TIPO_WHATSAPP, 'plantilla': plantillas[plantilla], 'demora_minutos': demora,
                'orden': orden,
                # Desactivadas: el texto final lo tiene que aprobar Marketing (y revisar protección de datos).
                'activa': False,
            })
        for nombre, desc, permisos in ROLES:
            RolPersonalizado.objects.get_or_create(nombre=nombre, defaults={'descripcion': desc, 'permisos': permisos})
        for atajo, titulo, texto in RESPUESTAS_RAPIDAS:
            RespuestaRapida.objects.get_or_create(atajo=atajo, defaults={'titulo': titulo, 'texto': texto})
        config, _ = ConfigAnura.objects.get_or_create(pk=1)
        if not config.embudo_entrantes_id:
            config.embudo_entrantes = embudo
            config.save()

        self.stdout.write(self.style.SUCCESS(f'Embudo "{embudo}" {"creado" if creado else "verificado"}.'))

        if not opts['sin_admin'] and not User.objects.filter(rol=User.ROL_ADMIN).exists() \
                and not User.objects.filter(is_superuser=True).exists():
            password = os.environ.get('ADMIN_PASSWORD') or secrets.token_urlsafe(10)
            User.objects.create_superuser(
                username=os.environ.get('ADMIN_USERNAME', 'admin'), email=os.environ.get('ADMIN_EMAIL', ''),
                password=password, rol=User.ROL_ADMIN, first_name='Administrador',
                debe_cambiar_password=not os.environ.get('ADMIN_PASSWORD'),
            )
            self.stdout.write(self.style.WARNING(
                f'Usuario administrador creado → usuario: {os.environ.get("ADMIN_USERNAME", "admin")}  '
                f'contraseña: {password}' + ('' if os.environ.get('ADMIN_PASSWORD') else '  (se pide cambiarla al ingresar)')
            ))
