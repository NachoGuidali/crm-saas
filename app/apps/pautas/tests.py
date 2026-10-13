import json
from datetime import timedelta
from decimal import Decimal

from django.core.management import call_command
from django.utils import timezone

from core.testing import TestCase
from apps.crm import services as crm
from apps.crm.models import Embudo, Oportunidad, Tipificacion
from apps.integraciones.models import ApiKey
from apps.users.models import User

from . import services
from .models import InversionPauta, Pauta


class PautasTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command('setup_inicial', '--sin-admin', verbosity=0)
        cls.embudo = Embudo.objects.get()
        cls.agente = User.objects.create_user('ana', password='x')
        cls.embudo.agentes.set([cls.agente])
        cls.mkt = User.objects.create_user('mkt', password='x', rol=User.ROL_SUPERVISOR)

    def lead(self, tel, **kw):
        return crm.ingresar_prospecto({'telefono': tel, 'nombre': 'X'}, self.embudo, 'api', **kw)

    def test_normalizacion_y_vinculo_al_ingresar(self):
        p = Pauta.objects.create(nombre='Pauta Instagram', claves=['ig_septiembre'])
        for texto in ('pauta instagram', 'PAUTA_INSTAGRAM', 'Paúta-Instagram ', 'IG Septiembre'):
            self.assertEqual(services.resolver_pauta(texto), p, texto)
        op = self.lead('1100000001', origen_pauta='pauta_instagram').oportunidad
        self.assertEqual((op.pauta, op.origen_pauta), (p, 'pauta_instagram'))

    def test_pauta_creada_despues_vincula_los_existentes(self):
        self.lead('1100000002', origen_pauta='Promo TikTok')
        self.lead('1100000003', origen_pauta='promo-tiktok')
        self.assertEqual(services.origenes_sin_pauta()[0]['n'], 1)
        self.client.force_login(self.mkt)
        r = self.client.post('/pautas/nueva/', {'nombre': 'Promo TikTok', 'plataforma': 'tiktok', 'activa': 'on'})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Oportunidad.objects.filter(pauta__nombre='Promo TikTok').count(), 2)
        r = self.client.post('/pautas/nueva/', {'nombre': 'Otra', 'plataforma': 'meta', 'claves_texto': 'promo tiktok'})
        self.assertContains(r, 'ya corresponde a la pauta')

    def test_metricas_costo_por_lead_y_por_cliente(self):
        p = Pauta.objects.create(nombre='pauta instagram')
        ops = [self.lead(f'11000001{n:02d}', origen_pauta='pauta instagram').oportunidad for n in range(10)]
        venta = Tipificacion.objects.get(nombre='Venta directa')
        for op in ops[:4]:
            crm.mover_etapa(op, self.embudo.etapa_ganado, self.agente, tipificacion=venta, valor=Decimal('20000'))
        self.lead('1100000100', origen_pauta='pauta instagram')  # repetido: ya tiene oportunidad activa
        InversionPauta.objects.create(pauta=p, fecha=timezone.localdate(), monto=Decimal('1000'))
        InversionPauta.objects.create(pauta=p, fecha=timezone.localdate() - timedelta(days=90), monto=Decimal('999'))
        ahora = timezone.now()
        fila = services.metricas(ahora - timedelta(days=1), ahora + timedelta(minutes=1), pautas=[p])[0]
        self.assertEqual((fila['leads'], fila['ventas'], fila['repetidos'], fila['inversion']), (10, 4, 1, Decimal('1000')))
        self.assertEqual((fila['cpl'], fila['cpa'], fila['conversion']), (Decimal('100.00'), Decimal('250.00'), 40.0))
        self.assertEqual(fila['meses_recupero'], Decimal('0.01'))  # 1000 / 80000 de cuotas mensuales

    def test_api_origen_libre_es_pauta_y_pantallas(self):
        p = Pauta.objects.create(nombre='pauta instagram')
        _, clave = ApiKey.generar(nombre='form')
        r = self.client.post('/api/v1/leads/', json.dumps({'nombre': 'Ana', 'telefono': '1155550000', 'origen': 'Pauta Instagram'}),
                             content_type='application/json', HTTP_X_API_KEY=clave)
        self.assertEqual(r.json()['pauta'], 'pauta instagram')
        op = Oportunidad.objects.get()
        self.assertEqual((op.origen, op.pauta), ('api', p))
        self.client.force_login(self.mkt)
        self.assertContains(self.client.get('/pautas/'), 'pauta instagram')
        r = self.client.post(f'/pautas/{p.pk}/', {'monto': '1.500,50', 'fecha': timezone.localdate().isoformat()})
        self.assertEqual(p.inversiones.get().monto, Decimal('1500.50'))
        self.assertContains(self.client.get(f'/pautas/{p.pk}/'), '$ 1.500,50')
        self.assertEqual(self.client.get('/oportunidades/', {'embudo': 'todos', 'pauta': p.pk}).context['page'].paginator.count, 1)

    def test_whatsapp_desde_anuncio_de_meta(self):
        from apps.whatsapp.proveedores.meta import _parsear
        msg = _parsear({'from': '5491166667777', 'id': 'w1', 'type': 'text', 'text': {'body': 'hola'},
                        'referral': {'source_type': 'ad', 'source_id': '123', 'headline': 'Doctor Flex IG'}}, {})
        self.assertEqual(msg.extra['pauta'], 'Doctor Flex IG')

    def test_agente_no_ve_el_panel(self):
        self.client.force_login(self.agente)
        self.assertEqual(self.client.get('/pautas/').status_code, 403)


class PautasWhatsAppTests(TestCase):
    """Chats de WhatsApp: ID de anuncio → título del anuncio → palabras clave del primer mensaje (todo opcional)."""

    @classmethod
    def setUpTestData(cls):
        call_command('setup_inicial', '--sin-admin', verbosity=0)
        cls.embudo = Embudo.objects.get()
        from apps.whatsapp.models import LineaWhatsApp
        cls.linea = LineaWhatsApp.objects.create(nombre='Ventas', proveedor='demo', embudo=cls.embudo)

    def entrar(self, tel, texto, extra=None):
        from apps.whatsapp.proveedores.base import MensajeEntrante
        from apps.whatsapp.services import procesar_mensaje_entrante
        procesar_mensaje_entrante(self.linea, MensajeEntrante(telefono=tel, wa_id=f'w{tel}', tipo='text',
                                                              contenido=texto, nombre_perfil='X', extra=extra or {}))
        return Oportunidad.objects.get(contacto__telefono=tel)

    def test_por_id_de_anuncio_aunque_el_titulo_no_coincida(self):
        p = Pauta.objects.create(nombre='Meta Instagram público general', anuncio_ids=['120210000000001'])
        op = self.entrar('+5491150000001', 'Hola', {'pauta': 'Titulo cualquiera',
                                                   'referral': {'source_id': '120210000000001'}})
        self.assertEqual((op.pauta, op.origen_pauta), (p, 'Titulo cualquiera'))

    def test_por_titulo_en_tambien_llega_como(self):
        p = Pauta.objects.create(nombre='Meta Instagram público general', claves=['Plan Flex - IG'])
        op = self.entrar('+5491150000002', 'Hola', {'pauta': 'Plan Flex IG', 'referral': {'source_id': '999'}})
        self.assertEqual(op.pauta, p)

    def test_por_palabra_clave_en_el_mensaje(self):
        p = Pauta.objects.create(nombre='Historias IG', palabras_clave=['#IG-SEP'])
        Pauta.objects.create(nombre='Bio IG', palabras_clave=['Hola, quería consultar por el plan familiar'])
        op = self.entrar('+5491150000003', 'Hola!! quiero info del plan #ig_sep 🙌')
        self.assertEqual((op.pauta, op.origen_pauta), (p, 'Historias IG'))
        op = self.entrar('+5491150000004', 'hola queria consultar por el plan familiar, gracias')
        self.assertEqual(op.pauta.nombre, 'Bio IG')
        op = self.entrar('+5491150000005', 'Hola, info')
        self.assertIsNone(op.pauta)

    def test_twilio_lee_el_anuncio(self):
        from django.test import RequestFactory
        from apps.whatsapp.models import LineaWhatsApp
        from apps.whatsapp.proveedores.twilio import ProveedorTwilio
        linea = LineaWhatsApp.objects.create(nombre='Tw', proveedor='twilio', twilio_account_sid='AC', twilio_from='+1')
        req = RequestFactory().post('/', {'From': 'whatsapp:+5491150000006', 'Body': 'hola', 'MessageSid': 'SM1',
                                          'ReferralSourceId': '777', 'ReferralHeadline': 'Anuncio Flex'})
        msg = ProveedorTwilio(linea).parsear_webhook(req).mensajes[0]
        self.assertEqual((msg.extra['pauta'], msg.extra['referral']['source_id']), ('Anuncio Flex', '777'))

    def test_formulario_valida_repetidos_y_cortas(self):
        from .views import PautaForm
        Pauta.objects.create(nombre='A', anuncio_ids=['111'], palabras_clave=['#COD-A'])
        f = PautaForm({'nombre': 'B', 'plataforma': 'meta', 'activa': 'on', 'anuncios_texto': '111',
                       'palabras_texto': '#cod_a\nhi'})
        self.assertFalse(f.is_valid())
        self.assertIn('anuncios_texto', f.errors)
        self.assertIn('palabras_texto', f.errors)
