import json

from django.core.management import call_command

from core.testing import TestCase
from apps.crm.models import Contacto, Embudo, Oportunidad

from apps.users.models import User

from .models import ApiKey


class ApiLeadsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command('setup_inicial', '--sin-admin', verbosity=0)
        cls.key, cls.clave = ApiKey.generar(nombre='Landing', embudo=Embudo.objects.get(), fuente='Landing DF')

    def post(self, data, clave=None):
        return self.client.post('/api/v1/leads/', json.dumps(data), content_type='application/json',
                                HTTP_X_API_KEY=clave or self.clave)

    def test_alta_y_duplicado(self):
        r = self.post({'nombre': 'Ana', 'telefono': '11 5555-0000', 'fecha_atencion': '15/09/2026', 'utm': 'google'})
        self.assertEqual(r.status_code, 201, r.content)
        self.assertTrue(r.json()['oportunidad_nueva'])
        c = Contacto.objects.get()
        self.assertEqual(c.datos_extra['utm'], 'google')
        self.assertEqual(Oportunidad.objects.get().fuente, 'Landing DF')
        r = self.post({'nombre': 'Ana', 'telefono': '+5491155550000'})
        self.assertEqual((r.status_code, r.json()['motivo']), (200, 'ya_activa'))

    def test_alta_con_etapa_y_vendedor(self):
        ana = User.objects.create_user('ana', password='x', email='ana@x.com')
        r = self.post({'nombre': 'Ana', 'telefono': '1155550000', 'etapa': 'contacto efectivo', 'agente': 'ana@x.com'})
        self.assertEqual(r.status_code, 201, r.content)
        op = Oportunidad.objects.get()
        self.assertEqual((op.etapa.nombre, op.agente), ('Contacto efectivo', ana))
        r = self.post({'telefono': '1155550000', 'etapa': 'Negociación'})  # el lead ya existe: se mueve
        self.assertTrue(r.json()['movida'])
        self.assertEqual(Oportunidad.objects.get().etapa.nombre, 'Negociación')
        r = self.post({'telefono': '1155550001', 'etapa': 'No existe'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('Negociación', r.json()['etapas'])

    def test_clave_invalida(self):
        self.assertEqual(self.post({'telefono': '1'}, clave='mala').status_code, 401)
        self.key.activa = False
        self.key.save()
        self.assertEqual(self.post({'telefono': '1155550000'}).status_code, 401)

    def test_buscar(self):
        self.post({'nombre': 'Ana', 'telefono': '1155550000'})
        r = self.client.get('/api/v1/leads/buscar/', {'telefono': '011 15 5555 0000'}, HTTP_X_API_KEY=self.clave)
        self.assertEqual(r.json()['contacto']['nombre'], 'Ana')

    def test_campos_personalizados_dinamicos_por_slug(self):
        # Cualquier campo personalizado activo se acepta por su slug; crear uno nuevo no necesita tocar el CRM.
        from apps.crm.models import CampoPersonalizado as CP
        CP.objects.create(nombre='Edad', tipo=CP.TIPO_NUMERO)
        CP.objects.create(nombre='Situación laboral', tipo=CP.TIPO_LISTA,
                          opciones=['Relación de dependencia', 'Monotributista / Autónomo'])
        r = self.post({'nombre': 'Juan', 'telefono': '1166660000', 'edad': 34,
                       'situacion_laboral': 'Monotributista / Autónomo', 'localidad': 'Quilmes'})
        self.assertEqual(r.status_code, 201, r.content)
        c = Contacto.objects.get(telefono='+5491166660000')
        self.assertEqual(c.datos_extra['edad'], 34)
        self.assertEqual(c.datos_extra['situacion_laboral'], 'Monotributista / Autónomo')
        self.assertEqual(c.localidad, 'Quilmes')  # localidad es campo core del contacto, no personalizado

    def test_slug_tolerante_y_valor_de_lista_invalido_no_frena_el_lead(self):
        from apps.crm.models import CampoPersonalizado as CP
        CP.objects.create(nombre='Situación laboral', tipo=CP.TIPO_LISTA, opciones=['Relación de dependencia'])
        # Clave con guion medio / mayúsculas igual matchea el slug; valor fuera de las opciones se ignora con aviso.
        r = self.post({'nombre': 'Pia', 'telefono': '1177770000', 'Situacion-Laboral': 'Monotributo'})
        self.assertEqual(r.status_code, 201, r.content)   # el lead igual se crea
        self.assertIn('advertencias', r.json())
        c = Contacto.objects.get(telefono='+5491177770000')
        self.assertNotIn('situacion_laboral', c.datos_extra or {})


class EmailYSMSDesdeLaFichaTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from django.core.management import call_command
        from apps.crm.models import Embudo
        call_command('setup_inicial', '--sin-admin', verbosity=0)
        cls.embudo = Embudo.objects.get()
        cls.ana = User.objects.create_user('ana3', password='x', email='ana@roisa.com')
        cls.embudo.agentes.set([cls.ana])

    def setUp(self):
        super().setUp()
        from apps.crm import services as crm
        self.op = crm.ingresar_prospecto({'telefono': '1155550101', 'nombre': 'Rosa Paz', 'email': 'rosa@x.com'},
                                         self.embudo, 'web').oportunidad
        self.client.force_login(self.ana)

    def test_email_manual(self):
        from django.core import mail
        from apps.automatizaciones.models import EmailEnviado
        r = self.client.post(f'/oportunidades/{self.op.pk}/email/', json.dumps({'asunto': 'Hola {primer_nombre}',
                             'cuerpo': 'Te paso la info'}), content_type='application/json')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((mail.outbox[-1].subject, mail.outbox[-1].reply_to), ('Hola Rosa', ['ana@roisa.com']))
        self.assertEqual(EmailEnviado.objects.get().enviado_por, self.ana)
        self.op.refresh_from_db()
        self.assertIsNotNone(self.op.primer_contacto_at)
        self.assertTrue(self.op.actividades.filter(tipo='email', usuario=self.ana).exists())

    def test_sms_ida_y_vuelta(self):
        from unittest import mock
        from .models import ConfigSMS
        c = ConfigSMS.get()
        c.activo, c.account_sid, c.auth_token, c.numero = True, 'AC1', 'tok', '+15550001111'
        c.save()
        resp = mock.Mock(status_code=201)
        resp.json.return_value = {'sid': 'SM1'}
        with mock.patch('apps.integraciones.sms.requests.post', return_value=resp) as post:
            r = self.client.post(f'/oportunidades/{self.op.pk}/sms/', json.dumps({'texto': 'Hola {primer_nombre}'}),
                                 content_type='application/json')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(post.call_args.kwargs['data'], {'To': '+5491155550101', 'Body': 'Hola Rosa', 'From': '+15550001111'})
        r = self.client.post(f'/integraciones/sms/webhook/{c.webhook_token}/', {'From': '+5491155550101', 'Body': 'sí!'})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(self.op.actividades.filter(tipo='sms', texto__contains='sí!').exists())
        self.assertEqual(self.client.post('/integraciones/sms/webhook/malo/', {'From': '1', 'Body': 'x'}).status_code, 403)


from django.test import override_settings  # noqa: E402


@override_settings(DEMO_LANDING=True)
class LandingDemoTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command('setup_inicial', '--sin-admin', verbosity=0)
        cls.embudo = Embudo.objects.get()
        cls.ana = User.objects.create_user('anal', password='x')
        cls.embudo.agentes.set([cls.ana])

    def test_formulario_crea_lead_asignado_y_avisa(self):
        from apps.users.models import NotificacionInterna
        self.assertContains(self.client.get('/demo/formulario/'), 'Landing Meta')
        r = self.client.post('/demo/formulario/', {'nombre': 'Lucía Prueba', 'telefono': '11 2345-6789',
                                                   'email': 'lu@x.com', 'consulta': 'plan familiar', 'origen': 'Landing Meta'})
        self.assertRedirects(r, '/demo/gracias/')
        op = Oportunidad.objects.get(contacto__telefono='+5491123456789')
        self.assertEqual((op.origen, op.origen_pauta, op.agente), ('web', 'Landing Meta', self.ana))
        self.assertTrue(op.actividades.filter(texto__contains='plan familiar').exists())
        self.assertTrue(NotificacionInterna.objects.filter(destinatario=self.ana, tipo='asignacion').exists())
        # Repetido: no duplica
        self.client.post('/demo/formulario/', {'nombre': 'Lucía', 'telefono': '+54 9 11 2345 6789', 'origen': 'Landing Meta'})
        self.assertEqual(Oportunidad.objects.filter(contacto__telefono='+5491123456789').count(), 1)

    def test_bot_y_telefono_invalido(self):
        self.client.post('/demo/formulario/', {'nombre': 'Bot', 'telefono': '1122223333', 'sitio': 'spam'})
        self.assertFalse(Contacto.objects.filter(telefono='+5491122223333').exists())
        self.assertContains(self.client.post('/demo/formulario/', {'nombre': 'X', 'telefono': 'abc'}), 'Revisá el número')

    @override_settings(DEMO_LANDING=False)
    def test_apagada_no_existe(self):
        self.assertEqual(self.client.get('/demo/formulario/').status_code, 404)


class ApiBotTests(TestCase):
    """Endpoints de control del bot n8n: /api/v1/whatsapp/bot/ y /handoff/."""
    @classmethod
    def setUpTestData(cls):
        from apps.whatsapp.models import Conversacion, LineaWhatsApp
        call_command('setup_inicial', '--sin-admin', verbosity=0)
        cls.key, cls.clave = ApiKey.generar(nombre='n8n', embudo=Embudo.objects.get())
        cls.linea = LineaWhatsApp.objects.create(nombre='Ventas', proveedor='evolution', evolution_instancia='v',
                                                 bot_n8n_activo=True, bot_n8n_url='https://n8n.test/bot')
        cls.conv = Conversacion.objects.create(linea=cls.linea, telefono='+5491155550000', bot_activo=True)

    def post(self, path, data, clave=None):
        return self.client.post(path, json.dumps(data), content_type='application/json',
                                 HTTP_X_API_KEY=clave or self.clave)

    def test_apagar_y_prender_bot(self):
        r = self.post('/api/v1/whatsapp/bot/', {'conversation_id': self.conv.pk, 'activo': False})
        self.assertEqual(r.status_code, 200, r.content)
        self.conv.refresh_from_db()
        self.assertFalse(self.conv.bot_activo)
        r = self.post('/api/v1/whatsapp/bot/', {'conversation_id': self.conv.pk, 'activo': True})
        self.conv.refresh_from_db()
        self.assertTrue(self.conv.bot_activo)

    def test_handoff_por_telefono_apaga_bot_y_deja_pendiente(self):
        from apps.whatsapp.models import Conversacion
        from apps.users.models import User
        self.conv.agente = User.objects.create_user('ana9', password='x')
        self.conv.estado = Conversacion.ESTADO_ABIERTA
        self.conv.save()
        r = self.post('/api/v1/whatsapp/handoff/', {'telefono': '011 15 5555 0000'})
        self.assertEqual(r.status_code, 200, r.content)
        self.conv.refresh_from_db()
        self.assertFalse(self.conv.bot_activo)
        self.assertEqual(self.conv.estado, Conversacion.ESTADO_PENDIENTE)

    def test_conversacion_inexistente_y_sin_clave(self):
        self.assertEqual(self.post('/api/v1/whatsapp/bot/', {'conversation_id': 999999, 'activo': True}).status_code, 404)
        self.assertEqual(self.post('/api/v1/whatsapp/bot/', {'conversation_id': self.conv.pk, 'activo': True},
                                   clave='mala').status_code, 401)

    def test_handoff_toma_el_agente_de_la_oportunidad_y_lo_asigna(self):
        from apps.crm.services import ingresar_prospecto
        from apps.whatsapp.models import Conversacion
        from apps.users.models import NotificacionInterna, User
        ag = User.objects.create_user('ag_ho', password='x')
        emb = Embudo.objects.get()
        emb.agentes.set([ag])
        op = ingresar_prospecto({'telefono': '5491188880000', 'nombre': 'Caro'}, emb, 'manual').oportunidad
        Oportunidad.objects.filter(pk=op.pk).update(agente=ag)  # garantizamos agente, sin depender del modo de asignación
        conv = Conversacion.objects.create(linea=self.linea, telefono=op.contacto.telefono,
                                           contacto=op.contacto, bot_activo=True)  # la conv todavía no tiene agente
        r = self.post('/api/v1/whatsapp/handoff/', {'conversation_id': conv.pk})
        self.assertEqual(r.status_code, 200, r.content)
        conv.refresh_from_db()
        self.assertEqual(conv.agente_id, ag.pk)
        self.assertFalse(conv.bot_activo)
        self.assertTrue(NotificacionInterna.objects.filter(destinatario=ag, tipo='whatsapp').exists())

    def test_handoff_sin_asignar_avisa_a_supervisores(self):
        from apps.whatsapp.models import Conversacion
        from apps.users.models import NotificacionInterna, User
        sup = User.objects.create_user('sup_ho', password='x', rol=User.ROL_SUPERVISOR)
        conv = Conversacion.objects.create(linea=self.linea, telefono='+5491199990000', bot_activo=True)  # sin contacto
        r = self.post('/api/v1/whatsapp/handoff/', {'conversation_id': conv.pk})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(NotificacionInterna.objects.filter(destinatario=sup, tipo='whatsapp').exists())


class ApiEnviarTests(TestCase):
    """POST /api/v1/whatsapp/enviar/ — respuesta del bot por conversation_id (sin depender de Contacto)."""
    @classmethod
    def setUpTestData(cls):
        from apps.whatsapp.models import Conversacion, LineaWhatsApp
        call_command('setup_inicial', '--sin-admin', verbosity=0)
        cls.key, cls.clave = ApiKey.generar(nombre='n8n', embudo=Embudo.objects.get())
        cls.linea = LineaWhatsApp.objects.create(nombre='Bot', proveedor='demo', bot_n8n_activo=True)
        # Conversación de un número que todavía NO tiene Contacto en el CRM.
        cls.conv = Conversacion.objects.create(linea=cls.linea, telefono='+5491166660000', bot_activo=True)

    def post(self, data):
        return self.client.post('/api/v1/whatsapp/enviar/', json.dumps(data), content_type='application/json',
                                 HTTP_X_API_KEY=self.clave)

    def test_responde_por_conversation_id_sin_contacto(self):
        from apps.whatsapp.models import Mensaje
        self.assertFalse(Contacto.objects.filter(telefono='+5491166660000').exists())
        r = self.post({'conversation_id': self.conv.pk, 'message': 'Hola, soy el bot'})
        self.assertEqual(r.status_code, 202, r.content)
        self.assertTrue(Mensaje.objects.filter(conversacion=self.conv, direccion=Mensaje.DIR_SALIENTE,
                                               contenido='Hola, soy el bot', automatico=True).exists())

    def test_responde_por_telefono_sin_mas_y_sin_contacto(self):
        # Como manda n8n: teléfono en dígitos (sin +) y sin Contacto creado todavía → responde en la conversación.
        from apps.whatsapp.models import Mensaje
        r = self.post({'telefono': '5491166660000', 'mensaje': 'Respuesta del bot', 'linea': 'COMPLETAR_LINEA'})
        self.assertEqual(r.status_code, 202, r.content)
        self.assertTrue(Mensaje.objects.filter(conversacion=self.conv, contenido='Respuesta del bot').exists())

    def test_conversation_id_inexistente(self):
        self.assertEqual(self.post({'conversation_id': 999999, 'message': 'x'}).status_code, 404)

    def test_telefono_sin_contacto_ni_conversacion(self):
        self.assertEqual(self.post({'telefono': '5491199999999', 'mensaje': 'x'}).status_code, 404)

    def test_respuesta_del_bot_saca_la_tarjeta_de_la_etapa_inicial(self):
        from apps.crm.services import ingresar_prospecto
        from apps.whatsapp.models import Conversacion
        emb = Embudo.objects.get()
        op = ingresar_prospecto({'telefono': '5491177770000', 'nombre': 'Tere'}, emb, 'manual').oportunidad
        inicial = emb.etapa_inicial
        self.assertEqual(op.etapa_id, inicial.pk)
        conv = Conversacion.objects.create(linea=self.linea, telefono=op.contacto.telefono,
                                           contacto=op.contacto, bot_activo=True)
        r = self.post({'conversation_id': conv.pk, 'message': 'Hola, soy el bot'})
        self.assertEqual(r.status_code, 202, r.content)
        op.refresh_from_db()
        self.assertNotEqual(op.etapa_id, inicial.pk)  # avanzó desde "Nuevo"
