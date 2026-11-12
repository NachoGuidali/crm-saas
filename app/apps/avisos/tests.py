import json
from datetime import time, timedelta
from unittest import mock

from django.core import mail
from django.core.management import call_command
from django.test import Client
from django.utils import timezone

from core.testing import TestCase
from apps.users.models import NotificacionInterna, User
from apps.whatsapp.models import Conversacion, LineaWhatsApp, Mensaje

from .models import EnvioAviso, ReglaAviso


class AvisosTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from apps.crm.models import Embudo
        call_command('setup_inicial', '--sin-admin', verbosity=0)
        cls.embudo = Embudo.objects.get()
        cls.admin = User.objects.create_superuser('root', 'r@x.com', 'x', rol=User.ROL_ADMIN)
        cls.asesora = User.objects.create_user('laura', email='laura@x.com', password='x', first_name='Laura',
                                               whatsapp='5491166667777')
        cls.linea_avisos = LineaWhatsApp.objects.create(nombre='Avisos', proveedor='demo')

    def setUp(self):
        super().setUp()
        from apps.crm import services as crm
        self.op = crm.ingresar_prospecto({'telefono': '1150000001', 'nombre': 'Ana Gómez'}, self.embudo, 'importacion').oportunidad
        self.linea_bot = LineaWhatsApp.objects.create(nombre='Bot', proveedor='demo')
        self.conv = Conversacion.objects.create(linea=self.linea_bot, telefono='5491150000001', contacto=self.op.contacto,
                                                bot_activo=True)
        mock.patch('apps.whatsapp.proveedores.demo.ProveedorDemo._quizas_responder').start()
        self.addCleanup(mock.patch.stopall)

    def _regla(self, **kw):
        r = ReglaAviso.objects.create(**{'nombre': 'Aviso', 'evento': 'humano', 'al_vendedor': False, 'por_campanita': True,
                                         'por_email': True, 'por_whatsapp': True, 'linea': self.linea_avisos, **kw})
        r.usuarios.set([self.asesora])
        return r

    def test_n8n_deriva_y_avisa_por_los_tres_canales(self):
        from apps.integraciones.models import ApiKey
        self._regla(nombre='Bot derivó', texto='Atendé a {nombre} ({telefono}): {motivo} {link}')
        _, clave = ApiKey.generar(nombre='n8n')
        with self.captureOnCommitCallbacks(execute=True):
            r = Client().post('/api/v1/whatsapp/handoff/', json.dumps({'conversation_id': self.conv.pk, 'motivo': 'Quiere hablar con alguien'}),
                              content_type='application/json', HTTP_X_API_KEY=clave)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(NotificacionInterna.objects.filter(destinatario=self.asesora, titulo__startswith='Atendé a Ana Gómez').exists())
        self.assertEqual(mail.outbox[-1].to, ['laura@x.com'])
        self.assertIn('Quiere hablar con alguien', mail.outbox[-1].body)
        # WhatsApp a la asesora por la línea de avisos, en un chat archivado y sin bot
        conv_aviso = Conversacion.objects.get(linea=self.linea_avisos, telefono__endswith="5491166667777")
        self.assertTrue(conv_aviso.archivada and not conv_aviso.bot_activo)
        self.assertIn(f'/whatsapp/?conv={self.conv.pk}', conv_aviso.mensajes.get().contenido)
        self.assertEqual(EnvioAviso.objects.filter(ok=True).count(), 2)

    def test_lead_nuevo_no_avisa_importaciones_y_filtra_embudo(self):
        from apps.crm import services as crm
        r = self._regla(evento='lead_nuevo', por_email=False, por_whatsapp=False)
        n0 = NotificacionInterna.objects.filter(destinatario=self.asesora).count()
        with self.captureOnCommitCallbacks(execute=True):
            crm.ingresar_prospecto({'telefono': '1150000002', 'nombre': 'Base'}, self.embudo, 'importacion')
            crm.ingresar_prospecto({'telefono': '1150000003', 'nombre': 'Web'}, self.embudo, 'web')
        notifs = NotificacionInterna.objects.filter(destinatario=self.asesora)
        self.assertEqual(notifs.count(), n0 + 1)
        self.assertIn('Web', notifs.order_by('-pk').first().titulo)

    def test_horario_solo_aplica_a_email_y_whatsapp(self):
        from . import services
        ahora = timezone.localtime()
        fuera = (ahora + timedelta(hours=3)).time()
        r = self._regla(desde=fuera, hasta=(ahora + timedelta(hours=4)).time())
        with self.captureOnCommitCallbacks(execute=True):
            services.disparar('humano', conv=self.conv, motivo='x')
        self.assertTrue(NotificacionInterna.objects.filter(destinatario=self.asesora).exists())
        self.assertEqual(len(mail.outbox), 0)
        r.desde, r.hasta = time(20, 0), time(8, 0)  # cruza la medianoche
        self.assertEqual(services.en_horario(r, timezone.make_aware(timezone.datetime(2026, 1, 1, 23, 0))), True)
        self.assertEqual(services.en_horario(r, timezone.make_aware(timezone.datetime(2026, 1, 1, 12, 0))), False)

    def test_chat_sin_atender_avisa_una_vez(self):
        from . import services
        self._regla(evento='sin_atender', minutos=15, por_email=False, por_whatsapp=False, al_vendedor=True)
        Conversacion.objects.filter(pk=self.conv.pk).update(bot_activo=False, estado='pendiente', no_leidos=2,
                                                            ultimo_entrante_at=timezone.now() - timedelta(minutes=20))
        self.assertEqual(services.revisar_sin_atender(), 1)
        self.assertEqual(services.revisar_sin_atender(), 0)  # no repite para el mismo mensaje
        self.assertTrue(NotificacionInterna.objects.filter(titulo__contains='lleva 20 min sin atender').exists())

    def test_venta_linea_caida_y_pantallas(self):
        from apps.agente.services import linea_cambio_estado
        from . import services
        self._regla(evento='linea_caida', por_email=True, por_whatsapp=False)
        with self.captureOnCommitCallbacks(execute=True):
            linea_cambio_estado(self.linea_bot, 'conectada', 'desconectada')
        self.assertIn('Se desconectó la línea de WhatsApp «Bot»', mail.outbox[-1].subject)
        web = Client()
        web.force_login(self.admin)
        self.assertEqual(web.get('/avisos/').status_code, 200)
        self.assertEqual(web.get('/avisos/nueva/').status_code, 200)
        r = web.post('/avisos/nueva/', {'nombre': 'X', 'evento': 'venta', 'por_whatsapp': 'on', 'al_vendedor': 'on', 'minutos': 15})
        self.assertContains(r, 'Elegí por qué línea')
        r = web.post('/avisos/nueva/', {'nombre': 'Ventas', 'evento': 'venta', 'por_campanita': 'on', 'al_vendedor': 'on',
                                        'minutos': 15, 'activa': 'on'})
        self.assertEqual(r.status_code, 302)
