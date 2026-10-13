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
