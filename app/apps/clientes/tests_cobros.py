"""Cobro de la plataforma: pagos manuales, vencimientos, suspensión automática, Mercado Pago y aviso en el CRM."""
import hashlib
import hmac
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.core import mail
from django.test import Client, override_settings
from django.utils import timezone
from django_tenants.utils import get_public_schema_name, schema_context

from apps.users.models import User

from . import cobros, mercadopago
from .models import Cliente, Pago, Plan
from .tests import PLATAFORMA, BaseMultiCliente

HOY = date(2026, 3, 10)


class FechasTests(BaseMultiCliente):
    def test_sumar_meses_ajusta_fin_de_mes(self):
        self.assertEqual(cobros.sumar_meses(date(2026, 1, 31), 1), date(2026, 2, 28))
        self.assertEqual(cobros.sumar_meses(date(2026, 11, 15), 3), date(2027, 2, 15))
        self.assertEqual(cobros.sumar_meses(date(2026, 3, 1), -1), date(2026, 2, 1))

    @mock.patch('django.utils.timezone.localdate', return_value=HOY)
    def test_desde_sugerido(self, _):
        c = self.cliente
        c.pagado_hasta = HOY - timedelta(days=3)  # vencido dentro de la gracia: mantiene el ciclo
        self.assertEqual(cobros.desde_sugerido(c), c.pagado_hasta)
        c.pagado_hasta = HOY - timedelta(days=60)  # vuelve después de mucho: desde hoy
        self.assertEqual(cobros.desde_sugerido(c), HOY)
        c.pagado_hasta, c.estado, c.prueba_hasta = None, Cliente.ESTADO_PRUEBA, HOY + timedelta(days=4)
        self.assertEqual(cobros.desde_sugerido(c), HOY + timedelta(days=4))  # respeta lo que le queda de prueba


@mock.patch('django.utils.timezone.localdate', return_value=HOY)
class PagosTests(BaseMultiCliente):
    def test_pago_extiende_vencimiento_y_activa(self, _):
        self.cliente.estado, self.cliente.pagado_hasta = Cliente.ESTADO_PRUEBA, None
        self.cliente.save()
        pago = cobros.registrar_pago(self.cliente, monto=100, meses=2)
        self.cliente.refresh_from_db()
        self.assertEqual((pago.periodo_desde, pago.periodo_hasta), (HOY, date(2026, 5, 10)))
        self.assertEqual(self.cliente.pagado_hasta, date(2026, 5, 10))
        self.assertEqual(self.cliente.estado, Cliente.ESTADO_ACTIVO)
        # El siguiente sigue el ciclo
        pago2 = cobros.registrar_pago(self.cliente, monto=50)
        self.assertEqual(pago2.periodo_desde, date(2026, 5, 10))

    def test_pago_reactiva_suspendido(self, _):
        cobros.suspender(self.cliente, Cliente.SUSPENSION_FALTA_PAGO)
        cobros.registrar_pago(self.cliente, monto=100)
        self.cliente.refresh_from_db()
        self.assertEqual(self.cliente.estado, Cliente.ESTADO_ACTIVO)
        self.assertEqual(self.cliente.motivo_suspension, '')

    def test_anular_pago_recalcula(self, _):
        p1 = cobros.registrar_pago(self.cliente, monto=100, desde=HOY)
        p2 = cobros.registrar_pago(self.cliente, monto=100)
        cobros.anular_pago(p2)
        self.cliente.refresh_from_db()
        self.assertEqual(self.cliente.pagado_hasta, p1.periodo_hasta)
        cobros.anular_pago(p1)
        self.cliente.refresh_from_db()
        self.assertIsNone(self.cliente.pagado_hasta)

    def test_estado_cobro(self, _):
        c = self.cliente
        c.estado = Cliente.ESTADO_ACTIVO
        for dias, esperado in ((30, 'al_dia'), (5, 'por_vencer'), (0, 'por_vencer'), (-5, 'en_gracia'),
                               (-6, 'vencido')):
            c.pagado_hasta = HOY + timedelta(days=dias)
            self.assertEqual(c.estado_cobro, esperado, dias)
        c.pagado_hasta = None
        self.assertEqual(c.estado_cobro, 'sin_fecha')
        c.estado = Cliente.ESTADO_SUSPENDIDO
        self.assertEqual(c.estado_cobro, '')


@override_settings(SAAS_EMAIL_AVISOS=True, SAAS_DATOS_PAGO='Alias: mi.alias')
@mock.patch('django.utils.timezone.localdate', return_value=HOY)
class RevisionDiariaTests(BaseMultiCliente):
    def setUp(self):
        super().setUp()
        Cliente.objects.filter(pk__in=[self.cliente.pk, self.otro.pk]).update(
            estado=Cliente.ESTADO_ACTIVO, contacto_email='dueno@cliente.com')
        self.cliente.refresh_from_db()
        self.otro.refresh_from_db()

    def _vence(self, cliente, dias, **extra):
        Cliente.objects.filter(pk=cliente.pk).update(pagado_hasta=HOY + timedelta(days=dias), **extra)

    def test_suspende_vencidos_pasada_la_gracia(self, _):
        self._vence(self.cliente, -6)
        self._vence(self.otro, -5)  # todavía en gracia
        r = cobros.revisar_cobros()
        self.assertEqual([c.pk for c in r['suspendidos']], [self.cliente.pk])
        self.cliente.refresh_from_db()
        self.assertEqual((self.cliente.estado, self.cliente.motivo_suspension),
                         (Cliente.ESTADO_SUSPENDIDO, Cliente.SUSPENSION_FALTA_PAGO))
        self.assertIn('suspendimos', mail.outbox[0].subject)

    def test_no_suspende_si_tiene_la_suspension_automatica_apagada(self, _):
        self._vence(self.cliente, -30, suspension_automatica=False)
        self.assertEqual(cobros.revisar_cobros()['suspendidos'], [])

    def test_prueba_vencida_se_suspende(self, _):
        Cliente.objects.filter(pk=self.cliente.pk).update(estado=Cliente.ESTADO_PRUEBA, pagado_hasta=None,
                                                          prueba_hasta=HOY - timedelta(days=10))
        self.assertEqual(len(cobros.revisar_cobros()['suspendidos']), 1)

    def test_avisa_una_sola_vez_por_vencimiento(self, _):
        self._vence(self.cliente, 3)
        self.assertEqual(len(cobros.revisar_cobros()['avisados']), 1)
        self.assertEqual(len(cobros.revisar_cobros()['avisados']), 0)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('Alias: mi.alias', mail.outbox[0].body)
        self.assertEqual(mail.outbox[0].to, ['dueno@cliente.com'])
        # Paga y vuelve a vencer: se vuelve a avisar
        cobros.registrar_pago(self.cliente, monto=10, desde=HOY - timedelta(days=30))
        self._vence(self.cliente, 2)
        self.assertEqual(len(cobros.revisar_cobros()['avisados']), 1)

    def test_no_avisa_si_debita_con_mercado_pago(self, _):
        self._vence(self.cliente, 3, cobro=Cliente.COBRO_MERCADOPAGO, mp_suscripcion_estado='authorized')
        self.assertEqual(cobros.revisar_cobros()['avisados'], [])

    def test_la_tarea_de_beat_corre_solo_en_la_plataforma(self, _):
        from django.conf import settings
        entrada = settings.CELERY_BEAT_SCHEDULE['clientes-cobros']
        self.assertEqual(entrada['tenant_schemas'], [get_public_schema_name()])


class PanelCobrosTests(BaseMultiCliente):
    def setUp(self):
        super().setUp()
        with schema_context(get_public_schema_name()):
            self.super = User.objects.create_superuser('plataforma', 'p@p.com', 'clave-plataforma-1')
            self.panel = Client()
            self.panel.force_login(self.super)
        self.url = f'/panel/clientes/{self.otro.pk}/'

    def test_registrar_pago_desde_el_panel(self):
        cobros.suspender(self.otro, Cliente.SUSPENSION_MANUAL)
        hoy = timezone.localdate()
        resp = self.pedir(self.panel, 'post', self.url, PLATAFORMA, data={
            'accion': 'pago', 'pago-monto': '15000', 'pago-meses': '1', 'pago-periodo_desde': hoy.isoformat(),
            'pago-fecha': hoy.isoformat(), 'pago-medio': 'transferencia', 'pago-referencia': 'op 123',
            'pago-reactivar': 'on'})
        self.assertEqual(resp.status_code, 302)
        self.otro.refresh_from_db()
        self.assertEqual(self.otro.pagado_hasta, cobros.sumar_meses(hoy, 1))
        self.assertEqual(self.otro.estado, Cliente.ESTADO_ACTIVO)
        pago = Pago.objects.get(cliente=self.otro)
        self.assertEqual((pago.monto, pago.registrado_por), (Decimal('15000'), 'plataforma'))
        resp = self.pedir(self.panel, 'get', '/panel/pagos/', PLATAFORMA)
        self.assertContains(resp, 'op 123')

    def test_lista_filtra_vencidos(self):
        Cliente.objects.filter(pk=self.otro.pk).update(pagado_hasta=timezone.localdate() - timedelta(days=2))
        Cliente.objects.filter(pk=self.cliente.pk).update(pagado_hasta=timezone.localdate() + timedelta(days=40))
        resp = self.pedir(self.panel, 'get', '/panel/?filtro=vencidos', PLATAFORMA)
        self.assertContains(resp, 'Otro cliente')
        self.assertNotContains(resp, 'Cliente de tests')

    def test_ficha_sin_mercado_pago_configurado(self):
        resp = self.pedir(self.panel, 'get', self.url, PLATAFORMA)
        self.assertContains(resp, 'Mercado Pago no está configurado')


def _resp(json_data, status=200):
    r = mock.Mock(status_code=status)
    r.json.return_value = json_data
    r.text = str(json_data)
    return r


@override_settings(MP_ACCESS_TOKEN='TEST-token', MP_WEBHOOK_SECRET='secreto', SITE_URL='https://plataforma.testserver')
class MercadoPagoTests(BaseMultiCliente):
    def setUp(self):
        super().setUp()
        plan = Plan.objects.create(nombre='Pro', precio_mensual=Decimal('20000'))
        Cliente.objects.filter(pk=self.otro.pk).update(plan=plan, estado=Cliente.ESTADO_ACTIVO)
        self.otro.refresh_from_db()

    @mock.patch('apps.clientes.mercadopago.requests.request')
    def test_crear_suscripcion(self, req):
        req.return_value = _resp({'id': 'pre123', 'status': 'pending', 'init_point': 'https://mp/checkout/pre123'})
        link = mercadopago.crear_suscripcion(self.otro, 'pagador@mail.com')
        self.assertEqual(link, 'https://mp/checkout/pre123')
        cuerpo = req.call_args.kwargs['json']
        self.assertEqual(cuerpo['auto_recurring']['transaction_amount'], 20000.0)
        self.assertEqual(cuerpo['external_reference'], f'crm-cliente-{self.otro.pk}')
        self.otro.refresh_from_db()
        self.assertEqual((self.otro.mp_suscripcion_id, self.otro.cobro), ('pre123', Cliente.COBRO_MERCADOPAGO))

    def _webhook(self, tipo, recurso_id, secreto='secreto'):
        ts, request_id = '1700000000', 'req-1'
        manifiesto = f'id:{recurso_id};request-id:{request_id};ts:{ts};'
        v1 = hmac.new(secreto.encode(), manifiesto.encode(), hashlib.sha256).hexdigest()
        return self.pedir(Client(), 'post', f'/mercadopago/webhook/?type={tipo}&data.id={recurso_id}', PLATAFORMA,
                          data={'type': tipo, 'data': {'id': recurso_id}}, content_type='application/json',
                          HTTP_X_SIGNATURE=f'ts={ts},v1={v1}', HTTP_X_REQUEST_ID=request_id)

    @mock.patch('apps.clientes.mercadopago.requests.request')
    def test_cobro_de_suscripcion_registra_pago_una_sola_vez(self, req):
        Cliente.objects.filter(pk=self.otro.pk).update(
            mp_suscripcion_id='pre123', estado=Cliente.ESTADO_SUSPENDIDO,
            motivo_suspension=Cliente.SUSPENSION_FALTA_PAGO)
        req.return_value = _resp({'id': 777, 'preapproval_id': 'pre123', 'transaction_amount': 20000,
                                  'payment': {'id': 999, 'status': 'approved'}})
        self.assertEqual(self._webhook('subscription_authorized_payment', '777').status_code, 200)
        self.assertEqual(self._webhook('subscription_authorized_payment', '777').status_code, 200)  # repetido
        pago = Pago.objects.get(cliente=self.otro)
        self.assertEqual((pago.medio, pago.mp_pago_id, pago.monto), ('mercadopago', '999', Decimal('20000')))
        self.otro.refresh_from_db()
        self.assertEqual(self.otro.estado, Cliente.ESTADO_ACTIVO)

    @mock.patch('apps.clientes.mercadopago.requests.request')
    def test_pago_de_mp_no_pisa_suspension_manual(self, req):
        Cliente.objects.filter(pk=self.otro.pk).update(
            mp_suscripcion_id='pre123', estado=Cliente.ESTADO_SUSPENDIDO, motivo_suspension=Cliente.SUSPENSION_MANUAL)
        req.return_value = _resp({'id': 1, 'preapproval_id': 'pre123', 'transaction_amount': 20000,
                                  'payment': {'id': 5, 'status': 'approved'}})
        self._webhook('subscription_authorized_payment', '1')
        self.otro.refresh_from_db()
        self.assertEqual(self.otro.estado, Cliente.ESTADO_SUSPENDIDO)
        self.assertTrue(Pago.objects.filter(cliente=self.otro).exists())

    @mock.patch('apps.clientes.mercadopago.requests.request')
    def test_link_de_pago_unico_por_varios_meses(self, req):
        req.return_value = _resp({'id': 42, 'status': 'approved', 'transaction_amount': 60000,
                                  'external_reference': f'crm-cliente-{self.otro.pk}', 'metadata': {'meses': 3}})
        self._webhook('payment', '42')
        pago = Pago.objects.get(cliente=self.otro)
        self.assertEqual(cobros.sumar_meses(pago.periodo_desde, 3), pago.periodo_hasta)

    @mock.patch('apps.clientes.mercadopago.requests.request')
    def test_pago_rechazado_no_registra(self, req):
        req.return_value = _resp({'id': 43, 'status': 'rejected', 'transaction_amount': 20000,
                                  'external_reference': f'crm-cliente-{self.otro.pk}'})
        self._webhook('payment', '43')
        self.assertFalse(Pago.objects.exists())

    @mock.patch('apps.clientes.mercadopago.requests.request')
    def test_firma_invalida_se_rechaza(self, req):
        resp = self._webhook('payment', '42', secreto='otro')
        self.assertEqual(resp.status_code, 401)
        req.assert_not_called()

    @mock.patch('apps.clientes.mercadopago.requests.request')
    def test_cambio_de_estado_de_suscripcion(self, req):
        Cliente.objects.filter(pk=self.otro.pk).update(mp_suscripcion_id='pre123', mp_suscripcion_estado='pending')
        req.return_value = _resp({'id': 'pre123', 'status': 'authorized',
                                  'external_reference': f'crm-cliente-{self.otro.pk}'})
        self._webhook('subscription_preapproval', 'pre123')
        self.otro.refresh_from_db()
        self.assertEqual(self.otro.mp_suscripcion_estado, 'authorized')

    @mock.patch('apps.clientes.mercadopago.requests.request')
    def test_error_de_la_api_pide_reintento(self, req):
        req.return_value = _resp({'message': 'boom'}, status=500)
        self.assertEqual(self._webhook('payment', '44').status_code, 500)


@mock.patch('django.utils.timezone.localdate', return_value=HOY)
class AvisoEnElCrmTests(BaseMultiCliente):
    def test_admin_ve_aviso_de_vencimiento_y_agente_no(self, _):
        Cliente.objects.filter(pk=self.cliente.pk).update(estado=Cliente.ESTADO_ACTIVO,
                                                          pagado_hasta=HOY + timedelta(days=2))
        c = Client()
        c.force_login(self.admin)
        resp = self.pedir(c, 'get', '/contactos/', 'testserver')
        self.assertContains(resp, 'Tu suscripción')
        agente = User.objects.create_user('agente', 'ag@test.com', 'x', rol=User.ROL_AGENTE)
        c.force_login(agente)
        self.assertNotContains(self.pedir(c, 'get', '/contactos/', 'testserver'), 'Tu suscripción')
