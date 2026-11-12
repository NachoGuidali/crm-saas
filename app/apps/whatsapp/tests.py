import hashlib
import hmac
import json
from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.core.management import call_command
from django.test import Client
from django.utils import timezone

from core.testing import TestCase
from apps.crm.models import Contacto, Embudo, Oportunidad
from apps.users.models import User

from .models import Conversacion, LineaWhatsApp, Mensaje, Plantilla
from .services import ErrorEnvio, enviar_mensaje, reservar_turno


def payload_evolution(tel='5491155551234', wa_id='ABC1', texto='Hola, quiero info', nombre='Ana Test'):
    return {'event': 'messages.upsert', 'instance': 'x', 'data': {
        'key': {'remoteJid': f'{tel}@s.whatsapp.net', 'fromMe': False, 'id': wa_id},
        'pushName': nombre, 'messageType': 'conversation', 'message': {'conversation': texto},
        'messageTimestamp': int(timezone.now().timestamp())}}


class WhatsAppBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command('setup_inicial', '--sin-admin', verbosity=0)
        cls.embudo = Embudo.objects.get()
        cls.agente = User.objects.create_user('ana', password='Clave-segura-1', first_name='Ana')
        cls.embudo.agentes.set([cls.agente])
        cls.linea = LineaWhatsApp.objects.create(nombre='Ventas', proveedor='evolution', embudo=cls.embudo,
                                                 evolution_instancia='ventas')


class WebhookEvolutionTests(WhatsAppBase):
    def test_entrante_crea_contacto_y_oportunidad_asignada(self):
        url = self.linea.webhook_url.replace(settings.SITE_URL, '')
        r = self.client.post(url, json.dumps(payload_evolution()), content_type='application/json')
        self.assertEqual(r.status_code, 200)
        c = Contacto.objects.get(telefono='+5491155551234')
        self.assertEqual(c.nombre, 'Ana Test')
        op = c.oportunidades.get()
        self.assertEqual(op.origen, Oportunidad.ORIGEN_WHATSAPP)
        self.assertEqual(op.agente, self.agente)
        conv = Conversacion.objects.get()
        self.assertEqual((conv.no_leidos, conv.agente), (1, self.agente))

    def test_idempotente_ante_reintentos_del_proveedor(self):
        url = self.linea.webhook_url.replace(settings.SITE_URL, '')
        for _ in range(3):
            self.client.post(url, json.dumps(payload_evolution()), content_type='application/json')
        self.assertEqual(Mensaje.objects.count(), 1)
        self.assertEqual(Oportunidad.objects.count(), 1)

    def test_segundo_mensaje_no_duplica_oportunidad(self):
        url = self.linea.webhook_url.replace(settings.SITE_URL, '')
        self.client.post(url, json.dumps(payload_evolution(wa_id='1')), content_type='application/json')
        self.client.post(url, json.dumps(payload_evolution(wa_id='2', texto='¿Precio?')), content_type='application/json')
        self.assertEqual(Oportunidad.objects.count(), 1)
        self.assertEqual(Conversacion.objects.get().no_leidos, 2)

    def test_clave_incorrecta_404_y_grupos_ignorados(self):
        r = self.client.post('/whatsapp/webhook/evolution/otra-clave/', '{}', content_type='application/json')
        self.assertEqual(r.status_code, 404)
        p = payload_evolution()
        p['data']['key']['remoteJid'] = '123-456@g.us'
        self.client.post(self.linea.webhook_url.replace(settings.SITE_URL, ''), json.dumps(p),
                         content_type='application/json')
        self.assertEqual(Mensaje.objects.count(), 0)

    def test_mismo_cliente_en_dos_lineas_un_solo_contacto(self):
        otra = LineaWhatsApp.objects.create(nombre='Soporte', proveedor='evolution', evolution_instancia='soporte')
        for linea, wid in ((self.linea, 'a'), (otra, 'b')):
            self.client.post(linea.webhook_url.replace(settings.SITE_URL, ''),
                             json.dumps(payload_evolution(wa_id=wid)), content_type='application/json')
        self.assertEqual(Contacto.objects.count(), 1)
        self.assertEqual(Conversacion.objects.count(), 2)


class WebhookMetaTwilioTests(WhatsAppBase):
    def test_meta_handshake_y_firma(self):
        linea = LineaWhatsApp.objects.create(nombre='Oficial', proveedor='meta', meta_phone_number_id='1',
                                             meta_access_token='t', meta_verify_token='vt', meta_app_secret='sec',
                                             embudo=self.embudo)
        url = linea.webhook_url.replace(settings.SITE_URL, '')
        r = self.client.get(url, {'hub.mode': 'subscribe', 'hub.verify_token': 'vt', 'hub.challenge': '123'})
        self.assertEqual(r.content, b'123')
        self.assertEqual(self.client.get(url, {'hub.mode': 'subscribe', 'hub.verify_token': 'x'}).status_code, 403)
        body = json.dumps({'entry': [{'changes': [{'value': {
            'contacts': [{'wa_id': '5491166667777', 'profile': {'name': 'Pepe'}}],
            'messages': [{'from': '5491166667777', 'id': 'wamid.1', 'type': 'text', 'text': {'body': 'hola'},
                          'timestamp': str(int(timezone.now().timestamp()))}]}}]}]}).encode()
        r = self.client.post(url, body, content_type='application/json', HTTP_X_HUB_SIGNATURE_256='sha256=mala')
        self.assertEqual(r.status_code, 403)
        firma = 'sha256=' + hmac.new(b'sec', body, hashlib.sha256).hexdigest()
        r = self.client.post(url, body, content_type='application/json', HTTP_X_HUB_SIGNATURE_256=firma)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(Contacto.objects.filter(telefono='+5491166667777', nombre='Pepe').exists())

    def test_twilio_entrante_y_estado(self):
        linea = LineaWhatsApp.objects.create(nombre='Tw', proveedor='twilio', twilio_account_sid='AC', twilio_from='+1415')
        url = linea.webhook_url.replace(settings.SITE_URL, '')
        r = self.client.post(url, {'From': 'whatsapp:+5491177778888', 'Body': 'hola', 'MessageSid': 'SM1',
                                   'SmsStatus': 'received', 'NumMedia': '0', 'ProfileName': 'Tito'})
        self.assertEqual(r['Content-Type'], 'text/xml')
        self.assertTrue(Mensaje.objects.filter(wa_id='SM1').exists())
        Mensaje.objects.create(conversacion=Conversacion.objects.get(), direccion='out', wa_id='SM2', status='sent')
        self.client.post(url, {'MessageSid': 'SM2', 'MessageStatus': 'read'})
        self.assertEqual(Mensaje.objects.get(wa_id='SM2').status, 'read')


class EnvioTests(WhatsAppBase):
    def test_pausa_aleatoria_entre_min_y_max(self):
        from apps.whatsapp.services import _gap_aleatorio, demora_para
        fija = LineaWhatsApp.objects.create(nombre='Fija', proveedor='demo', min_segundos_entre_envios=5)
        self.assertEqual(_gap_aleatorio(fija), 5)  # sin máximo → pausa fija
        rnd = LineaWhatsApp.objects.create(nombre='Rnd', proveedor='demo',
                                           min_segundos_entre_envios=3, max_segundos_entre_envios=8)
        for _ in range(40):
            self.assertTrue(3 <= _gap_aleatorio(rnd) <= 8)
        self.assertTrue(3 <= demora_para(rnd) <= 8)  # línea ociosa: domina la pausa humanizada

    def test_ventana_24h_en_linea_oficial(self):
        linea = LineaWhatsApp.objects.create(nombre='Oficial', proveedor='meta', meta_phone_number_id='1',
                                             meta_access_token='t', meta_verify_token='v')
        c = Contacto.objects.create(nombre='X', telefono='1133334444')
        conv = Conversacion.objects.create(linea=linea, telefono=c.telefono, contacto=c,
                                           ultimo_entrante_at=timezone.now() - timedelta(hours=30))
        with self.assertRaises(ErrorEnvio):
            enviar_mensaje(conv, self.agente, texto='hola')
        p = Plantilla.objects.first()
        with self.assertRaises(ErrorEnvio):  # plantilla no aprobada en Meta
            enviar_mensaje(conv, self.agente, plantilla=p)
        p.meta_estado = Plantilla.ESTADO_APROBADA
        p.save()
        with mock.patch('apps.whatsapp.proveedores.meta.ProveedorMeta._enviar', return_value='wamid.X'), \
                self.captureOnCommitCallbacks(execute=True):
            msg = enviar_mensaje(conv, self.agente, plantilla=p)
        msg.refresh_from_db()
        self.assertEqual((msg.status, msg.wa_id), ('sent', 'wamid.X'))
        self.assertIn('X', msg.contenido)

    def test_envio_desde_ficha_crea_conversacion_y_avanza_etapa(self):
        demo = LineaWhatsApp.objects.create(nombre='Demo', proveedor='demo')
        from apps.crm.services import ingresar_prospecto
        op = ingresar_prospecto({'telefono': '1144445555', 'nombre': 'Rita'}, self.embudo, 'manual').oportunidad
        cl = Client()
        cl.force_login(self.agente)
        with mock.patch('apps.whatsapp.proveedores.demo.ProveedorDemo._quizas_responder'), \
                self.captureOnCommitCallbacks(execute=True):
            r = cl.post('/whatsapp/enviar/', {'contacto': op.contacto_id, 'linea': demo.pk, 'texto': 'Hola Rita'})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(Mensaje.objects.get().status, 'sent')
        op.refresh_from_db()
        self.assertEqual(op.etapa.nombre, 'En gestión')

    def test_ficha_elegir_linea_para_escribir(self):
        """Contacto con chat en la línea A: el agente puede elegir la B, ve el chat vacío y el envío abre otro chat."""
        a = LineaWhatsApp.objects.create(nombre='Linea A', proveedor='demo')
        b = LineaWhatsApp.objects.create(nombre='Linea B', proveedor='demo')
        ajena = LineaWhatsApp.objects.create(nombre='Ajena', proveedor='demo')
        ajena.agentes.set([User.objects.create_user('otro', password='Clave-segura-1')])
        from apps.crm.services import ingresar_prospecto
        op = ingresar_prospecto({'telefono': '1144446666', 'nombre': 'Rosa'}, self.embudo, 'manual').oportunidad
        conv_a = Conversacion.objects.create(linea=a, telefono=op.contacto.telefono, contacto=op.contacto,
                                             ultimo_mensaje_at=timezone.now())
        Mensaje.objects.create(conversacion=conv_a, direccion='in', tipo='text', contenido='mensaje viejo en A')
        cl = Client()
        cl.force_login(self.agente)
        url = op.get_absolute_url()
        r = cl.get(url)
        self.assertEqual(r.context['conv'], conv_a)
        self.assertContains(r, 'mensaje viejo en A')
        self.assertContains(r, 'id="selLineaChat"')
        nombres = [o['linea'].nombre for o in r.context['opciones_linea']]
        self.assertIn('Linea B', nombres)
        self.assertNotIn('Ajena', nombres)

        r = cl.get(url, {'linea': b.pk})
        self.assertIsNone(r.context['conv'])
        self.assertEqual(r.context['linea_actual'], b)
        self.assertNotContains(r, 'mensaje viejo en A')
        self.assertContains(r, f'name="linea" value="{b.pk}"')
        with mock.patch('apps.whatsapp.proveedores.demo.ProveedorDemo._quizas_responder'), \
                self.captureOnCommitCallbacks(execute=True):
            r = cl.post('/whatsapp/enviar/', {'contacto': op.contacto_id, 'linea': b.pk, 'texto': 'Hola desde B'})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(Conversacion.objects.filter(contacto=op.contacto).count(), 2)
        self.assertEqual(Conversacion.objects.get(pk=r.json()['conv']).linea, b)
        # Una línea que no tiene habilitada no le sirve para escribir
        r = cl.post('/whatsapp/enviar/', {'contacto': op.contacto_id, 'linea': ajena.pk, 'texto': 'x'})
        self.assertEqual(r.status_code, 404)

    def test_ficha_linea_oficial_sin_chat_pide_plantilla(self):
        oficial = LineaWhatsApp.objects.create(nombre='Oficial', proveedor='meta', meta_phone_number_id='1',
                                               meta_access_token='t', meta_verify_token='v')
        from apps.crm.services import ingresar_prospecto
        op = ingresar_prospecto({'telefono': '1133335555', 'nombre': 'Olga'}, self.embudo, 'manual').oportunidad
        cl = Client()
        cl.force_login(self.agente)
        r = cl.get(op.get_absolute_url(), {'linea': oficial.pk})
        self.assertTrue(r.context['ventana_cerrada'])
        self.assertNotContains(r, 'id="avisoVentana" style="display:none"')

    def test_ritmo_de_envio_espaciado(self):
        self.linea.min_segundos_entre_envios = 10
        esperas = [reservar_turno(self.linea) for _ in range(3)]
        self.assertAlmostEqual(esperas[0], 0, delta=1)
        self.assertAlmostEqual(esperas[2], 20, delta=1)


class NotaDeVozTests(WhatsAppBase):
    def test_nota_de_voz_se_convierte_a_ogg_opus(self):
        import shutil
        import subprocess
        import tempfile
        from unittest import skipIf
        if not shutil.which('ffmpeg'):
            self.skipTest('ffmpeg no instalado')
        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.core.files.storage import default_storage
        with tempfile.NamedTemporaryFile(suffix='.webm') as f:  # como graba Chrome: WebM/Opus
            subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1',
                            '-c:a', 'libopus', f.name], check=True)
            datos = open(f.name, 'rb').read()
        demo = LineaWhatsApp.objects.create(nombre='Demo voz', proveedor='demo')
        from apps.crm.services import ingresar_prospecto
        c = ingresar_prospecto({'telefono': '1144447777', 'nombre': 'Voz'}, self.embudo, 'manual').contacto
        cl = Client()
        cl.force_login(self.agente)
        with mock.patch('apps.whatsapp.proveedores.demo.ProveedorDemo._quizas_responder'), \
                self.captureOnCommitCallbacks(execute=True):
            r = cl.post('/whatsapp/enviar/', {'contacto': c.pk, 'linea': demo.pk, 'voz': '1',
                                              'archivo': SimpleUploadedFile('nota.webm', datos, 'audio/webm')})
        self.assertEqual(r.status_code, 200, r.content)
        m = Mensaje.objects.get(direccion='out')
        self.assertEqual((m.tipo, m.media_mime), ('audio', 'audio/ogg'))
        ruta = m.media_url.replace(settings.MEDIA_URL, '', 1)
        self.assertEqual(default_storage.open(ruta).read(4), b'OggS')


class CredencialesCifradasTests(TestCase):
    def test_los_secretos_se_guardan_cifrados_en_la_base(self):
        """C1: tokens y claves de integraciones cifrados en reposo (el modelo los ve en claro)."""
        from django.db import connection
        linea = LineaWhatsApp.objects.create(
            nombre='Meta', proveedor=LineaWhatsApp.PROV_META, meta_phone_number_id='1',
            meta_access_token='token-secreto-123', meta_app_secret='secreto-app', meta_verify_token='verif')
        self.assertEqual(LineaWhatsApp.objects.get(pk=linea.pk).meta_access_token, 'token-secreto-123')
        cur = connection.cursor()
        cur.execute('SELECT meta_access_token, meta_app_secret, meta_verify_token '
                    'FROM whatsapp_lineawhatsapp WHERE id = %s', [linea.pk])
        crudo = cur.fetchone()
        for valor in crudo:
            self.assertTrue(valor.startswith('fernet:'), f'sin cifrar en la base: {valor!r}')
        self.assertNotIn('token-secreto-123', crudo[0])


class MediaSalienteFirmadaTests(TestCase):
    """A4: los adjuntos salientes se sirven solo con firma válida (o sesión), no públicos permanentes."""

    def test_url_publica_firma_los_salientes(self):
        from apps.whatsapp.proveedores.base import url_publica
        pub = url_publica('/media/whatsapp/salientes/2026/01/abc.jpg')
        self.assertIn('?sig=', pub)
        # un archivo que no es saliente no se firma
        self.assertNotIn('?sig=', url_publica('/media/whatsapp/conv_1/foto.jpg'))

    def test_saliente_sin_firma_ni_sesion_da_404(self):
        c = Client()  # anónimo
        self.assertEqual(c.get('/media/whatsapp/salientes/2026/01/abc.jpg').status_code, 404)

    def test_saliente_con_firma_valida_entra(self):
        import os
        from django.conf import settings
        from core.media_firmada import firmar
        from core.storage import media_root
        ruta = 'whatsapp/salientes/2026/01/abc.txt'
        destino = os.path.join(media_root(), ruta)
        os.makedirs(os.path.dirname(destino), exist_ok=True)
        with open(destino, 'w') as f:
            f.write('hola')
        resp = Client().get(f'/media/{ruta}?sig={firmar(ruta)}')
        self.assertEqual(resp.status_code, 200)
        # firma de otra ruta no sirve
        self.assertEqual(Client().get(f'/media/{ruta}?sig={firmar("whatsapp/salientes/otra.txt")}').status_code, 404)


class BotN8nTests(WhatsAppBase):
    """Bot de n8n por línea: reenvío del entrante, toggle por conversación y liberar al reactivar."""
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.linea.bot_n8n_activo = True
        cls.linea.bot_n8n_url = 'https://n8n.test/webhook/bot'
        cls.linea.bot_n8n_liberar_url = 'https://n8n.test/webhook/liberar'
        cls.linea.save()

    def _entrante(self, **kw):
        url = self.linea.webhook_url.replace(settings.SITE_URL, '')
        return self.client.post(url, json.dumps(payload_evolution(**kw)), content_type='application/json')

    def _llamadas_bot(self, post):
        return [c for c in post.call_args_list if c.args and c.args[0] == 'https://n8n.test/webhook/bot']

    def test_entrante_se_reenvia_a_n8n_y_la_conv_arranca_con_bot(self):
        with mock.patch('requests.post') as post, self.captureOnCommitCallbacks(execute=True):
            self._entrante()
        conv = Conversacion.objects.get()
        self.assertTrue(conv.bot_activo)
        llamadas = self._llamadas_bot(post)
        self.assertEqual(len(llamadas), 1)
        payload = llamadas[0].kwargs['json']
        self.assertEqual(payload['conversation_id'], conv.pk)
        self.assertIn('5491155551234', payload['phone'])
        self.assertEqual(payload['message'], 'Hola, quiero info')
        self.assertIn('/api/v1/whatsapp/enviar/', payload['crm_reply_url'])
        self.assertIn('/api/v1/whatsapp/handoff/', payload['crm_handoff_url'])

    def test_conversacion_pausada_no_reenvia(self):
        with mock.patch('requests.post'), self.captureOnCommitCallbacks(execute=True):
            self._entrante(wa_id='1')
        Conversacion.objects.update(bot_activo=False)  # un agente tomó la conversación
        with mock.patch('requests.post') as post, self.captureOnCommitCallbacks(execute=True):
            self._entrante(wa_id='2', texto='otra')
        self.assertEqual(self._llamadas_bot(post), [])

    def test_linea_sin_bot_no_reenvia(self):
        self.linea.bot_n8n_activo = False
        self.linea.save()
        with mock.patch('requests.post') as post, self.captureOnCommitCallbacks(execute=True):
            self._entrante()
        self.assertFalse(Conversacion.objects.get().bot_activo)
        self.assertEqual(self._llamadas_bot(post), [])

    def test_toggle_bot_desde_el_inbox(self):
        with mock.patch('requests.post'), self.captureOnCommitCallbacks(execute=True):
            self._entrante()
        conv = Conversacion.objects.get()
        cl = Client()
        cl.force_login(self.agente)
        r = cl.post(f'/whatsapp/conversacion/{conv.pk}/bot_off/', content_type='application/json')
        self.assertEqual(r.status_code, 200, r.content)
        conv.refresh_from_db()
        self.assertFalse(conv.bot_activo)
        with mock.patch('apps.whatsapp.tasks.liberar_bot_n8n.delay') as lib:
            r = cl.post(f'/whatsapp/conversacion/{conv.pk}/bot_on/', content_type='application/json')
        self.assertEqual(r.status_code, 200, r.content)
        conv.refresh_from_db()
        self.assertTrue(conv.bot_activo)
        lib.assert_called_once_with(conv.pk)

    def test_tomar_apaga_la_marca_de_bot_derivada(self):
        conv = Conversacion.objects.create(linea=self.linea, telefono='+5491155551111',
                                           bot_activo=False, bot_derivada=True,
                                           estado=Conversacion.ESTADO_PENDIENTE)
        cl = Client()
        cl.force_login(self.agente)
        r = cl.post(f'/whatsapp/conversacion/{conv.pk}/tomar/', content_type='application/json')
        self.assertEqual(r.status_code, 200, r.content)
        conv.refresh_from_db()
        self.assertFalse(conv.bot_derivada)
        self.assertEqual(conv.agente_id, self.agente.pk)


class EliminarParaTodosTests(WhatsAppBase):
    def setUp(self):
        super().setUp()
        from apps.crm.services import ingresar_prospecto
        self.op = ingresar_prospecto({'telefono': '1144445555', 'nombre': 'Rita'}, self.embudo, 'manual').oportunidad
        self.conv = Conversacion.objects.create(linea=self.linea, telefono='5491144445555', contacto=self.op.contacto,
                                                agente=self.agente)
        self.msg = Mensaje.objects.create(conversacion=self.conv, direccion='out', contenido='Precio equivocado',
                                          wa_id='3EB0ABC', status='delivered', enviado_por=self.agente)
        self.cl = Client()
        self.cl.force_login(self.agente)

    def _resp(self, status=200, data=None):
        r = mock.Mock(status_code=status, ok=status < 400, text=json.dumps(data or {}))
        r.json.return_value = data or {}
        return r

    def test_elimina_en_evolution_con_la_clave_real_y_queda_tachado(self):
        encontrado = {'messages': {'records': [{'key': {'id': '3EB0ABC', 'remoteJid': '123456@lid', 'fromMe': True}}]}}
        with mock.patch('requests.request', side_effect=[self._resp(200, encontrado), self._resp(200)]) as req:
            r = self.cl.post(f'/whatsapp/mensaje/{self.msg.pk}/eliminar/')
        self.assertEqual(r.status_code, 200, r.content)
        metodo, url = req.call_args_list[1].args[:2]
        self.assertEqual(metodo, 'DELETE')
        self.assertIn('/chat/deleteMessageForEveryone/ventas', url)
        self.assertEqual(req.call_args_list[1].kwargs['json'],
                         {'id': '3EB0ABC', 'remoteJid': '123456@lid', 'fromMe': True})
        self.msg.refresh_from_db()
        self.assertEqual(self.msg.eliminado_por, self.agente)
        self.assertContains(r, 'Mensaje borrado')
        self.assertTrue(self.op.contacto.actividades.filter(texto__contains='Precio equivocado').exists())
        # El chat lo muestra tachado y sin botón de eliminar
        r = self.cl.get(f'/whatsapp/conversacion/{self.conv.pk}/mensajes/')
        self.assertIn('eliminado', r.json()['html'])
        self.assertNotIn('eliminar-msg', r.json()['html'])

    def test_rechazos(self):
        # Entrantes no
        entrante = Mensaje.objects.create(conversacion=self.conv, direccion='in', contenido='hola', wa_id='IN1')
        self.assertEqual(self.cl.post(f'/whatsapp/mensaje/{entrante.pk}/eliminar/').status_code, 400)
        # Más de 48 h no
        Mensaje.objects.filter(pk=self.msg.pk).update(timestamp=timezone.now() - timedelta(hours=49))
        r = self.cl.post(f'/whatsapp/mensaje/{self.msg.pk}/eliminar/')
        self.assertContains(r, '48 horas', status_code=400)
        # Líneas oficiales (Meta / Twilio) no lo permiten
        meta = LineaWhatsApp.objects.create(nombre='Oficial', proveedor='meta', meta_phone_number_id='1')
        conv = Conversacion.objects.create(linea=meta, telefono='5491100000000', agente=self.agente)
        m = Mensaje.objects.create(conversacion=conv, direccion='out', contenido='x', wa_id='wamid.1', status='sent')
        self.assertFalse(m.eliminable)
        r = self.cl.post(f'/whatsapp/mensaje/{m.pk}/eliminar/')
        self.assertContains(r, 'no permite eliminar', status_code=400)
        # Chat ajeno: 404
        otro = User.objects.create_user('beto', password='x')
        cl = Client()
        cl.force_login(otro)
        self.assertEqual(cl.post(f'/whatsapp/mensaje/{self.msg.pk}/eliminar/').status_code, 404)
        self.msg.refresh_from_db()
        self.assertIsNone(self.msg.eliminado_at)

    def test_boton_solo_en_salientes_recientes(self):
        html = self.cl.get(f'/whatsapp/conversacion/{self.conv.pk}/mensajes/').json()['html']
        self.assertIn(f'data-msg="{self.msg.pk}"', html)
        self.assertIn('eliminar-msg', html)


class ReactivarBotSoloTests(WhatsAppBase):
    def test_vuelve_al_bot_tras_n_dias_sin_mensajes(self):
        from apps.crm.services import ingresar_prospecto
        from .services import reactivar_bots_inactivos
        linea = LineaWhatsApp.objects.create(nombre='Bot', proveedor='demo', bot_n8n_activo=True, bot_reactivar_dias=3,
                                             bot_n8n_liberar_url='https://n8n.test/webhook/multi-reiniciar')
        hace = timezone.now() - timedelta(days=4)
        def chat(tel, **kw):
            op = ingresar_prospecto({'telefono': tel, 'nombre': tel}, self.embudo, 'manual').oportunidad
            c = Conversacion.objects.create(linea=linea, telefono='549' + tel, contacto=op.contacto, bot_activo=False,
                                            bot_derivada=True, ultimo_mensaje_at=kw.get('ultimo', hace))
            return c, op
        viejo, op = chat('1150000001')
        reciente, _ = chat('1150000002', ultimo=timezone.now() - timedelta(days=1))
        baja, op_baja = chat('1150000003')
        op_baja.contacto.no_contactar = True
        op_baja.contacto.save()
        with mock.patch('requests.post') as post, self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(reactivar_bots_inactivos(), 1)
        viejo.refresh_from_db()
        self.assertTrue(viejo.bot_activo and not viejo.bot_derivada)
        self.assertEqual(post.call_args.kwargs['json']['phone'], '5491150000001')
        self.assertTrue(op.actividades.filter(texto__startswith='Bot reactivado solo').exists())
        for c in (reciente, baja):
            c.refresh_from_db()
            self.assertFalse(c.bot_activo)


class AbrirChatPorTelefonoTests(WhatsAppBase):
    def test_link_por_telefono_abre_el_chat(self):
        conv = Conversacion.objects.create(linea=self.linea, telefono='+5491163589975', agente=self.agente)
        cl = Client()
        cl.force_login(self.agente)
        r = cl.get('/whatsapp/', {'tel': '5491163589975'})
        self.assertEqual(r.context['conv'], conv)
        self.assertIsNone(cl.get('/whatsapp/', {'tel': '5491100000000'}).context['conv'])
