"""
Datos de demostración para presentar el CRM: equipo de ventas, una línea de WhatsApp simulada,
Anura en modo demo y ~300 prospectos con historia (gestiones, llamadas, chats, ventas y no ventas).

    python manage.py datos_demo            # crea todo
    python manage.py datos_demo --borrar   # borra los prospectos demo antes de crear
"""
import random
from datetime import timedelta

from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

NOMBRES = ['María', 'Juan', 'Lucía', 'Carlos', 'Sofía', 'Martín', 'Valentina', 'Diego', 'Camila', 'Jorge', 'Florencia',
           'Pablo', 'Agustina', 'Nicolás', 'Julieta', 'Federico', 'Paula', 'Gustavo', 'Romina', 'Alejandro', 'Graciela',
           'Hernán', 'Natalia', 'Sergio', 'Carolina', 'Marcelo', 'Silvina', 'Ricardo', 'Mariana', 'Fernando']
APELLIDOS = ['González', 'Rodríguez', 'Gómez', 'Fernández', 'López', 'Díaz', 'Martínez', 'Pérez', 'García', 'Sánchez',
             'Romero', 'Sosa', 'Álvarez', 'Torres', 'Ruiz', 'Ramírez', 'Flores', 'Benítez', 'Acosta', 'Medina',
             'Herrera', 'Suárez', 'Aguirre', 'Giménez', 'Gutiérrez', 'Pereyra', 'Rojas', 'Molina', 'Castro', 'Ortiz']
ESPECIALIDADES = ['Clínica médica', 'Pediatría', 'Ginecología', 'Cardiología', 'Traumatología', 'Dermatología',
                  'Oftalmología', 'Guardia', 'Laboratorio', 'Diagnóstico por imágenes']
EQUIPO = [('laura', 'Laura', 'Benítez', '101'), ('martin', 'Martín', 'Ríos', '102'), ('sofia', 'Sofía', 'Luna', '103'),
          ('diego', 'Diego', 'Paz', '104')]
MENSAJES_IN = ['Hola, ¿de qué se trata?', 'Sí, me interesa, ¿cuánto sale?', '¿Qué cubre el plan?', 'Ahora no puedo hablar',
               '¿Me pasás la info por acá?', 'Gracias, lo charlo en casa y te aviso', 'Ok 👍']
MENSAJES_OUT = ['¡Hola {n}! Te escribo de DoctoRed por Doctor Flex, ¿tenés un minuto?',
                'Te paso el detalle del plan y las cuotas 👇', '¿Te queda cómodo que te llame mañana a la tarde?',
                'Cualquier duda escribime por acá.']


class Command(BaseCommand):
    help = 'Carga datos de demostración (equipo, líneas y ~300 prospectos con historia).'

    def add_arguments(self, parser):
        parser.add_argument('--cantidad', type=int, default=300)
        parser.add_argument('--borrar', action='store_true')

    def handle(self, *args, **opts):
        from apps.automatizaciones.models import AccionEtapa
        from apps.crm.models import Contacto, Embudo
        from apps.telefonia.models import ConfigAnura, InternoAnura
        from apps.users.models import RolPersonalizado, User
        from apps.whatsapp.models import LineaWhatsApp

        call_command('setup_inicial', '--sin-admin', verbosity=0)
        random.seed(42)
        embudo = Embudo.objects.get(slug__startswith='doctor-flex')

        if opts['borrar']:
            Contacto.objects.filter(datos_extra__demo=True).delete()

        # Equipo
        rol_agente = RolPersonalizado.objects.filter(nombre='Agente de ventas').first()
        agentes = []
        for username, nombre, apellido, interno in EQUIPO:
            u, creado = User.objects.get_or_create(username=username, defaults={
                'first_name': nombre, 'last_name': apellido, 'email': f'{username}@demo.local',
                'rol': User.ROL_AGENTE, 'rol_custom': rol_agente,
            })
            if creado:
                u.set_password('demo1234')
                u.save()
            InternoAnura.objects.get_or_create(usuario=u, defaults={'interno': interno})
            agentes.append(u)
        sup, creado = User.objects.get_or_create(username='carla', defaults={
            'first_name': 'Carla', 'last_name': 'Méndez', 'email': 'carla@demo.local', 'rol': User.ROL_SUPERVISOR})
        if creado:
            sup.set_password('demo1234')
            sup.save()
        InternoAnura.objects.get_or_create(usuario=sup, defaults={'interno': '100'})
        for admin in User.objects.filter(is_superuser=True):
            if not InternoAnura.objects.filter(usuario=admin).exists():
                InternoAnura.objects.create(usuario=admin, interno=f'19{admin.pk}')
        embudo.agentes.set(agentes)
        embudo.supervisores.set([sup])

        linea, _ = LineaWhatsApp.objects.get_or_create(nombre='Ventas Doctor Flex (demo)', defaults={
            'proveedor': LineaWhatsApp.PROV_DEMO, 'telefono': '+54 9 11 4000-0000', 'embudo': embudo,
            'estado': LineaWhatsApp.ESTADO_CONECTADA, 'min_segundos_entre_envios': 2})
        embudo.linea_whatsapp = linea
        embudo.save()
        config, _ = ConfigAnura.objects.get_or_create(pk=1)
        config.activo, config.modo_demo = True, True
        config.webphone_url = config.webphone_url or 'https://panel.anura.com.ar'
        config.save()
        AccionEtapa.objects.filter(embudo=embudo).update(activa=True)
        crear_campos_ejemplo()

        with transaction.atomic():
            n = self._prospectos(embudo, agentes, linea, opts['cantidad'])
            crear_pautas_demo()
        self.stdout.write(self.style.SUCCESS(
            f'{n} prospectos demo creados. Usuarios: laura / martin / sofia / diego (agentes) y carla (supervisora), '
            'contraseña demo1234. Anura en modo demo y línea de WhatsApp simulada.'))

    def _prospectos(self, embudo, agentes, linea, cantidad):
        from apps.crm import services as crm
        from apps.crm.models import Actividad, HistorialEtapa, Oportunidad, Tarea, Tipificacion
        from apps.telefonia.models import Llamada
        from apps.whatsapp.models import Conversacion, Mensaje

        ahora = timezone.now()
        etapas = list(embudo.etapas.order_by('orden'))
        normales = [e for e in etapas if e.tipo == 'normal']
        ganado, perdido = embudo.etapa_ganado, embudo.etapa_perdido
        tip_venta = list(Tipificacion.objects.filter(embudo=embudo, resultado='venta'))
        tip_no = [t for t in Tipificacion.objects.filter(embudo=embudo, resultado='no_venta') if t.accion != 'postergar']
        pesos_no = [5 if t.nombre in ('Sin respuesta', 'Ya tiene otra cobertura', 'Cuota fuera de presupuesto') else 2
                    for t in tip_no]
        creados = 0
        for i in range(cantidad):
            nombre = f'{random.choice(NOMBRES)} {random.choice(APELLIDOS)}'
            tel = f'11{random.randint(20000000, 69999999)}'
            hace = timedelta(days=random.randint(0, 34), hours=random.randint(0, 10), minutes=random.randint(0, 59))
            creado_at = ahora - hace
            datos = {'nombre': nombre, 'telefono': tel, 'email': f'{nombre.split()[0].lower()}{i}@correo.com',
                     'fecha_atencion': (creado_at - timedelta(days=random.randint(10, 200))).date(),
                     'especialidad_atencion': random.choice(ESPECIALIDADES), 'datos_extra': {'demo': True}}
            agente = agentes[i % len(agentes)]
            res = crm.ingresar_prospecto(datos, embudo, random.choice(['importacion'] * 6 + ['whatsapp', 'llamada_entrante', 'api']),
                                         fuente='Base centro médico (demo)', agente=agente,
                                         disparar_automatizaciones=False)
            op = res.oportunidad
            if not res.oportunidad_nueva:
                continue
            creados += 1
            destino = self._destino(normales)
            intentos = random.randint(0, 5) if destino == normales[1] else random.randint(1, 3)
            if destino == normales[0]:
                intentos = 0
            avance = creado_at
            for et in normales[1:normales.index(destino) + 1] if destino in normales else normales[1:4]:
                avance += timedelta(hours=random.randint(2, 40))
                HistorialEtapa.objects.create(oportunidad=op, etapa_nueva=et, usuario=agente, nota='demo')
            cierre = None
            if destino in ('venta', 'no_venta'):
                cierre = ganado if destino == 'venta' else perdido
                tip = random.choice(tip_venta) if destino == 'venta' else random.choices(tip_no, weights=pesos_no)[0]
                estado = Oportunidad.ESTADO_GANADA if destino == 'venta' else Oportunidad.ESTADO_PERDIDA
                cerrada_at = min(avance + timedelta(hours=random.randint(4, 72)), ahora)
                Oportunidad.objects.filter(pk=op.pk).update(
                    etapa=cierre, estado=estado, tipificacion=tip, cerrada_at=cerrada_at, cerrada_por=agente,
                    valor=random.choice([18500, 21900, 26400]) if destino == 'venta' else None,
                    contacto_efectivo_at=creado_at + timedelta(hours=random.randint(1, 30)) if destino == 'venta' or random.random() < .5 else None)
                op.tareas.update(estado=Tarea.ESTADO_COMPLETADA)
                Actividad.objects.create(contacto=op.contacto, oportunidad=op, tipo=Actividad.TIPO_CIERRE, usuario=agente,
                                         texto=f'→ {cierre} · {tip.categoria}: {tip}', created_at=cerrada_at)
            else:
                efectivo = destino.marca_contacto_efectivo
                Oportunidad.objects.filter(pk=op.pk).update(
                    etapa=destino, etapa_desde=min(avance, ahora),
                    contacto_efectivo_at=creado_at + timedelta(hours=random.randint(2, 48)) if efectivo else None)
                if random.random() < .08 and destino != normales[0]:
                    Oportunidad.objects.filter(pk=op.pk).update(
                        estado=Oportunidad.ESTADO_PAUSADA, motivo_pausa='Pidió ser recontactado más adelante',
                        proximo_contacto_at=ahora + timedelta(days=random.randint(3, 40)))
            Oportunidad.objects.filter(pk=op.pk).update(
                created_at=creado_at, asignada_at=creado_at, intentos_contacto=intentos,
                ultima_actividad_at=min(avance, ahora) if destino in normales else creado_at + timedelta(days=1))
            for k in range(intentos):
                momento = min(creado_at + timedelta(hours=6 * (k + 1)), ahora)
                atendida = k == intentos - 1 and destino not in (normales[0], normales[1])
                ll = Llamada.objects.create(
                    call_id=f'demo-{op.pk}-{k}', direccion='OUT', estado='atendida' if atendida else 'no_atendida',
                    numero=op.contacto.telefono, agente=agente, contacto=op.contacto, oportunidad=op, inicio_at=momento,
                    duracion_seg=random.randint(60, 540) if atendida else 0, procesada=True, origen_registro='demo',
                    interno=agente.interno_anura.interno)
                Actividad.objects.create(contacto=op.contacto, oportunidad=op, tipo=Actividad.TIPO_LLAMADA, llamada=ll,
                                         usuario=agente, created_at=momento,
                                         texto=f'Llamada saliente: {ll.get_estado_display().lower()}')
            if random.random() < .45:
                conv = Conversacion.objects.create(linea=linea, telefono=op.contacto.telefono, contacto=op.contacto,
                                                   agente=agente, estado=Conversacion.ESTADO_ABIERTA)
                t = creado_at + timedelta(hours=1)
                ult = ''
                for j in range(random.randint(2, 6)):
                    salida = j % 2 == 0
                    texto = (random.choice(MENSAJES_OUT).format(n=op.contacto.nombre.split()[0]) if salida
                             else random.choice(MENSAJES_IN))
                    Mensaje.objects.create(conversacion=conv, direccion='out' if salida else 'in', contenido=texto,
                                           status='read' if salida else 'delivered', enviado_por=agente if salida else None,
                                           timestamp=min(t, ahora), wa_id=f'demo-{conv.pk}-{j}')
                    ult, t = texto, t + timedelta(minutes=random.randint(3, 300))
                no_leidos = 1 if random.random() < .15 else 0
                Conversacion.objects.filter(pk=conv.pk).update(ultimo_mensaje_at=min(t, ahora), ultimo_entrante_at=min(t, ahora),
                                                               ultimo_mensaje_texto=ult[:200], no_leidos=no_leidos)
            if destino in normales and random.random() < .3:
                Tarea.objects.create(oportunidad=op, contacto=op.contacto, asignado_a=agente, tipo=Tarea.TIPO_LLAMADA,
                                     titulo=f'Volver a llamar a {op.contacto.nombre}',
                                     vence_at=ahora + timedelta(hours=random.randint(-30, 48)))
        return creados

    @staticmethod
    def _destino(normales):
        r = random.random()
        if r < .16:
            return 'venta'
        if r < .44:
            return 'no_venta'
        return random.choices(normales, weights=[18, 30, 20, 14, 8][:len(normales)])[0]


def crear_campos_ejemplo():
    """Campos personalizados de ejemplo (se pueden editar o borrar desde Configuración → Campos personalizados)."""
    from apps.crm.models import CampoPersonalizado as CP
    CP.objects.get_or_create(nombre='Plan de interés', defaults={
        'tipo': CP.TIPO_LISTA, 'opciones': ['Doctor Flex Individual', 'Doctor Flex Familiar'],
        'mostrar_en_tarjeta': True, 'mostrar_en_lista': True, 'filtrable': True, 'orden': 1})
    CP.objects.get_or_create(nombre='Integrantes del grupo', defaults={'tipo': CP.TIPO_NUMERO, 'orden': 2,
                                                                       'mostrar_en_lista': True})
    CP.objects.get_or_create(nombre='Tiene otra cobertura', defaults={'tipo': CP.TIPO_SINO, 'orden': 3, 'filtrable': True})
    # Ejemplo de obligatorio por etapa: para enviar la propuesta hay que saber qué plan le interesa.
    from apps.crm.models import Etapa
    for etapa in Etapa.objects.filter(nombre='Propuesta enviada'):
        if 'cp:plan_de_interes' not in (etapa.campos_requeridos or []):
            etapa.campos_requeridos = list(etapa.campos_requeridos or []) + ['cp:plan_de_interes']
            etapa.save(update_fields=['campos_requeridos'])


def crear_pautas_demo():
    """Pautas de ejemplo con inversión, y reparte los prospectos demo entre ellas."""
    import random
    from datetime import timedelta
    from decimal import Decimal
    from django.utils import timezone
    from apps.crm.models import Oportunidad
    from apps.pautas.models import InversionPauta, Pauta
    hoy = timezone.localdate()
    definiciones = [
        ('Pauta Instagram', 'meta', ['ig_doctorflex', 'instagram doctor flex'], [180000, 150000]),
        ('Pauta Facebook Leads', 'meta', ['fb_leads'], [120000, 110000]),
        ('Google Búsqueda', 'google', ['google_search'], [90000, 95000]),
        ('Referidos', 'offline', [], []),
    ]
    pautas = []
    for nombre, plataforma, claves, montos in definiciones:
        p, creada = Pauta.objects.get_or_create(nombre=nombre, defaults={'plataforma': plataforma, 'claves': claves,
                                                                         'inicio': hoy - timedelta(days=60)})
        if creada:
            for i, monto in enumerate(montos):
                InversionPauta.objects.create(pauta=p, fecha=(hoy.replace(day=1) - timedelta(days=30 * i)).replace(day=1),
                                              monto=Decimal(monto), nota='Carga demo')
        pautas.append(p)
    rnd = random.Random(7)
    pesos = [40, 25, 20, 8]
    ops = list(Oportunidad.objects.filter(contacto__datos_extra__demo=True, pauta__isnull=True, origen_pauta=''))
    for op in ops:
        if rnd.random() < 0.07:
            Oportunidad.objects.filter(pk=op.pk).update(origen_pauta='promo tiktok septiembre')  # origen sin pauta
            continue
        if rnd.random() < 0.15:
            continue  # sin pauta (carga manual / base)
        p = rnd.choices(pautas, weights=pesos)[0]
        Oportunidad.objects.filter(pk=op.pk).update(pauta=p, origen_pauta=p.claves[0] if p.claves else p.nombre)
    return len(ops)
