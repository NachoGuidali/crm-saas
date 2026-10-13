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
