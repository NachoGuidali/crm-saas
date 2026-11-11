import json
from unittest import mock

from django.core.management import call_command
from django.test import Client
from django.utils import timezone

from core.testing import TestCase
from apps.users.models import User


class WebhooksTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from apps.crm.models import CampoPersonalizado, Embudo
        call_command('setup_inicial', '--sin-admin', verbosity=0)
        cls.embudo = Embudo.objects.get()
        cls.etapas = list(cls.embudo.etapas.filter(tipo='normal').order_by('orden'))
        cls.admin = User.objects.create_superuser('root', 'r@x.com', 'x', rol=User.ROL_ADMIN)
        cls.sueldo = CampoPersonalizado.objects.create(nombre='Sueldo bruto', tipo='numero')
        cls.validado = CampoPersonalizado.objects.create(nombre='Resultado validacion', tipo='texto')

    def setUp(self):
        super().setUp()
        from apps.crm import services as crm
        from .models import WebhookDestino
        self.destino = WebhookDestino.objects.create(nombre='Videollamada', url='https://video.test/hook', token='tok123')
        self.op = crm.ingresar_prospecto({'telefono': '1150000001', 'nombre': 'Ana Gómez', 'email': 'ana@x.com',
                                          'datos_extra': {self.sueldo.slug: '850000'}}, self.embudo, 'web').oportunidad
        self.web = Client()
        self.web.force_login(self.admin)

    def _automatizacion(self, **kw):
        from apps.automatizaciones.models import AccionEtapa
        return AccionEtapa.objects.create(embudo=self.embudo, solo_en_horario=False, **kw)

    def test_circuito_videollamada_de_validacion(self):
        from apps.crm import services as crm
        datos_completos, validado = self.etapas[1], self.etapas[2]
        self._automatizacion(etapa=datos_completos, nombre='A validación', tipo='webhook', webhook_destino=self.destino)
        self._automatizacion(etapa=datos_completos, nombre='Validado OK', disparador='evento', evento_nombre='validacion_ok',
                             tipo='etapa', mover_a=validado)
        with mock.patch('requests.post', return_value=mock.Mock(status_code=200, text='{"ok":true}')) as post, \
                self.captureOnCommitCallbacks(execute=True):
            crm.mover_etapa(self.op, datos_completos)
        kw = post.call_args.kwargs
        self.assertEqual(post.call_args.args[0], 'https://video.test/hook')
        self.assertEqual(kw['headers']['Authorization'], 'Bearer tok123')
        p = kw['json']
        self.assertEqual(p['contacto']['nombre'], 'Ana Gómez')
        self.assertEqual(p['campos'][self.sueldo.slug], {'etiqueta': 'Sueldo bruto', 'valor': '850000'})
        self.assertEqual(p['oportunidad']['etapa'], str(datos_completos))
        callback = p['callback']['url']
        self.assertIn('/api/v1/eventos/', callback)
        from .models import EnvioWebhook
        self.assertEqual(EnvioWebhook.objects.get().estado, 'ok')
        self.assertTrue(self.op.actividades.filter(texto__startswith='Datos enviado a "Videollamada"').exists())
        # El sistema de videollamada avisa que terminó (sin API key: la URL está firmada)
        ruta = callback.split('://', 1)[1].split('/', 1)[1]
        with self.captureOnCommitCallbacks(execute=True):
            r = Client().post('/' + ruta, json.dumps({'evento': 'Validacion OK', 'datos': {self.validado.slug: 'aprobado',
                                                                                             'otro': 'x'}}),
                              content_type='application/json')
        self.assertEqual(r.json(), {'ok': True, 'oportunidad_id': self.op.pk, 'evento': 'validacion_ok', 'automatizaciones': 1})
        self.op.refresh_from_db()
        self.assertEqual(self.op.etapa, validado)
        self.op.contacto.refresh_from_db()
        self.assertEqual(self.op.contacto.datos_extra[self.validado.slug], 'aprobado')
        self.assertNotIn('otro', self.op.contacto.datos_extra)
        self.assertTrue(self.op.actividades.filter(texto__startswith='Evento externo "validacion_ok"').exists())

    def test_token_invalido_y_api_con_key(self):
        from .models import ApiKey
        self.assertEqual(Client().post('/api/v1/eventos/inventado/', '{"evento":"x"}', content_type='application/json').status_code, 404)
        _, clave = ApiKey.generar(nombre='video')
        r = Client().post('/api/v1/eventos/', json.dumps({'telefono': '11 5000-0001', 'evento': 'validacion_ok'}),
                          content_type='application/json', HTTP_X_API_KEY=clave)
        self.assertEqual(r.json()['oportunidad_id'], self.op.pk)

    def test_plantilla_propia(self):
        from . import webhooks
        p = webhooks.armar_payload(self.op, '{"name": "{nombre}", "phone": "{telefono}", "income": "{campo:%s}", '
                                            '"note": "dijo \\"hola\\""}' % self.sueldo.slug)
        self.assertEqual(p, {'name': 'Ana Gómez', 'phone': self.op.contacto.telefono, 'income': '850000', 'note': 'dijo "hola"'})
        with self.assertRaises(webhooks.ErrorPlantilla):
            webhooks.armar_payload(self.op, '{"name": {nombre}}')

    def test_falla_reintenta_y_avisa(self):
        from apps.users.models import NotificacionInterna
        from . import webhooks
        from .models import EnvioWebhook
        envio = EnvioWebhook.objects.create(destino=self.destino, oportunidad=self.op, payload={'a': 1})
        with mock.patch('requests.post', return_value=mock.Mock(status_code=500, text='boom')):
            self.assertFalse(webhooks.enviar_ahora(envio))
        self.assertEqual((envio.estado, envio.intentos, envio.respuesta_status), ('pendiente', 1, 500))
        webhooks.fallo_definitivo(envio)
        self.assertEqual(envio.estado, 'error')
        self.assertTrue(NotificacionInterna.objects.filter(titulo__contains='No se pudo enviar a "Videollamada"').exists())

    def test_pantalla_probar_y_formulario_de_automatizacion(self):
        self.assertEqual(self.web.get('/integraciones/webhooks/').status_code, 200)
        with mock.patch('requests.post', return_value=mock.Mock(status_code=200, text='ok')):
            r = self.web.post('/integraciones/webhooks/', {'accion': 'probar', 'destino': self.destino.pk}, follow=True)
        self.assertContains(r, 'HTTP 200')
        # Guardar sin token mantiene el anterior
        self.web.post('/integraciones/webhooks/', {'accion': 'guardar', 'destino': self.destino.pk, 'nombre': 'Video',
                                                   'url': 'https://video.test/v2', 'autenticacion': 'bearer', 'activo': 'on'})
        self.destino.refresh_from_db()
        self.assertEqual((self.destino.url, self.destino.token), ('https://video.test/v2', 'tok123'))
