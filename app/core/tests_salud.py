import time
from unittest import mock

from django.core import mail
from django.test import override_settings

from core import salud
from core.testing import TestCase


class RedisFalso:
    def __init__(self):
        self.d = {}

    def set(self, k, v, ex=None, nx=False):
        if nx and k in self.d:
            return None
        self.d[k] = v
        return True

    def mget(self, claves):
        return [self.d.get(k) for k in claves]


@override_settings(ALERTAS_EMAIL='yo@x.com', SAAS_EMAIL_AVISOS_COPIA='')
class SaludTests(TestCase):
    def setUp(self):
        super().setUp()
        self.r = RedisFalso()
        p = mock.patch('core.salud._redis', return_value=self.r)
        p.start()
        self.addCleanup(p.stop)
        mock.patch.object(salud, '_INICIO', time.time() - 3600).start()
        self.addCleanup(mock.patch.stopall)

    def _latir_todas(self, hace=0):
        for c in salud.COLAS:
            self.r.d[f'salud:latido:{c}'] = int(time.time() - hace)

    def test_todo_ok_y_worker_caido(self):
        self._latir_todas()
        self.assertTrue(salud.estado()['ok'])
        self.r.d['salud:latido:salientes'] = int(time.time() - 600)
        self.r.d['salud:latido:masivos'] = int(time.time() - 600)
        e = salud.estado()
        self.assertFalse(e['ok'])
        self.assertEqual(e['problemas'], ['El contenedor "worker-masivos" no responde (colas: salientes, masivos).'])

    def test_todo_caido_endpoint_503_y_un_solo_email(self):
        self._latir_todas(hace=900)
        r = self.client.get('/salud/')
        self.assertEqual(r.status_code, 503)
        self.assertIn('No responde ningún worker', r.json()['problemas'][0])
        self.client.get('/salud/')
        self.assertEqual(len(mail.outbox), 1)  # no se repite durante 30 min
        self.assertEqual(mail.outbox[0].to, ['yo@x.com'])
        self._latir_todas()
        self.assertEqual(self.client.get('/salud/').json(), {'ok': True, 'estado': 'ok', 'problemas': []})

    def test_recien_arrancada_no_alarma(self):
        salud._INICIO = time.time()
        self.assertTrue(salud.estado()['ok'])

    def test_cartel_para_admins(self):
        from apps.users.models import User
        self._latir_todas(hace=900)
        admin = User.objects.create_user('a', password='x', rol=User.ROL_ADMIN)
        agente = User.objects.create_user('b', password='x', rol=User.ROL_AGENTE)
        with override_settings(MONITOR_SALUD=True):
            self.client.force_login(admin)
            self.assertContains(self.client.get('/', follow=True), 'Falla en el servidor')
            self.client.force_login(agente)
            self.assertNotContains(self.client.get('/', follow=True), 'Falla en el servidor')
