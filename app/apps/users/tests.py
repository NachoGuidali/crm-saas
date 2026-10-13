import re

from django.core import mail
from django.test import Client, override_settings

from core.testing import TestCase

from .models import RolPersonalizado, SesionConexion, User


class AccesoTests(TestCase):
    def setUp(self):
        super().setUp()
        self.u = User.objects.create_user('ana', email='ana@x.com', password='Clave-segura-1')

    def test_login_con_usuario_o_email(self):
        self.assertEqual(self.client.post('/usuarios/login/', {'username': 'ANA@x.com', 'password': 'Clave-segura-1'}).status_code, 302)

    def test_bloqueo_tras_intentos_fallidos(self):
        for _ in range(5):
            self.client.post('/usuarios/login/', {'username': 'ana', 'password': 'mal'})
        r = self.client.post('/usuarios/login/', {'username': 'ana', 'password': 'Clave-segura-1'})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Demasiados intentos')

    def test_recupero_completo(self):
        r = self.client.post('/usuarios/recuperar/', {'email': 'ana@x.com'})
        self.assertRedirects(r, '/usuarios/recuperar/enviado/')
        self.assertEqual(len(mail.outbox), 1)
        link = re.search(r'https?://[^\s"]+/usuarios/recuperar/[^\s"]+/', mail.outbox[0].body).group(0)
        path = '/' + link.split('/', 3)[3]
        r = self.client.get(path, follow=True)
        r = self.client.post(r.redirect_chain[-1][0], {'new_password1': 'Otra-clave-99', 'new_password2': 'Otra-clave-99'})
        self.assertRedirects(r, '/usuarios/recuperar/listo/')
        self.assertTrue(Client().login(username='ana', password='Otra-clave-99'))

    def test_recupero_email_inexistente_no_revela_nada(self):
        r = self.client.post('/usuarios/recuperar/', {'email': 'nadie@x.com'})
        self.assertRedirects(r, '/usuarios/recuperar/enviado/')
        self.assertEqual(len(mail.outbox), 0)

    def test_contrasena_temporal_obliga_a_cambiarla(self):
        User.objects.filter(pk=self.u.pk).update(debe_cambiar_password=True)
        self.client.login(username='ana', password='Clave-segura-1')
        self.assertRedirects(self.client.get('/tablero/'), '/usuarios/password/')
        self.client.post('/usuarios/password/', {'old_password': 'Clave-segura-1', 'new_password1': 'Nueva-clave-77',
                                                 'new_password2': 'Nueva-clave-77'})
        self.assertFalse(User.objects.get(pk=self.u.pk).debe_cambiar_password)


class PermisosTests(TestCase):
    def test_roles(self):
        agente = User.objects.create_user('a', password='x')
        sup = User.objects.create_user('s', password='x', rol=User.ROL_SUPERVISOR)
        self.assertFalse(agente.tiene_permiso('ver_todo'))
        self.assertTrue(sup.tiene_permiso('reasignar'))
        self.assertFalse(sup.tiene_permiso('usuarios'))
        rol = RolPersonalizado.objects.create(nombre='Solo reportes', permisos=['reportes', 'inventado'])
        self.assertEqual(rol.permisos, ['reportes'])
        agente.rol_custom = rol
        agente.save()
        agente = User.objects.get(pk=agente.pk)
        self.assertTrue(agente.tiene_permiso('reportes'))
        self.assertFalse(agente.tiene_permiso('ver_todo'))

    def test_alta_de_usuario_con_invitacion(self):
        admin = User.objects.create_superuser('root', 'root@x.com', 'x', rol=User.ROL_ADMIN)
        self.client.force_login(admin)
        r = self.client.post('/usuarios/nuevo/', {'username': 'nuevo', 'first_name': 'Nu', 'email': 'nuevo@x.com',
                                                  'rol': 'agente', 'is_active': 'on', 'disponible': 'on',
                                                  'modo_password': 'invitar', 'interno_anura': '305'})
        self.assertEqual(r.status_code, 302)
        u = User.objects.get(username='nuevo')
        self.assertEqual(u.interno_anura.interno, '305')
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('nuevo', mail.outbox[0].body)

    def test_supervisor_no_puede_crear_admin(self):
        sup = User.objects.create_user('s', password='x', rol=User.ROL_SUPERVISOR,
                                       rol_custom=RolPersonalizado.objects.create(nombre='Sup+', permisos=['usuarios']))
        self.client.force_login(sup)
        r = self.client.post('/usuarios/nuevo/', {'username': 'x2', 'email': 'x2@x.com', 'rol': 'admin',
                                                  'modo_password': 'invitar', 'is_active': 'on'})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(User.objects.filter(username='x2').exists())


class PresenciaPorPestanasTests(TestCase):
    def test_cerrar_la_ultima_pestana_desconecta_y_navegar_no(self):
        import time
        from unittest import mock
        from core import presencia
        u = User.objects.create_user('pres', password='x')
        ahora = time.time()
        with mock.patch('time.time', return_value=ahora):
            presencia.marcar(u, 'A')
            presencia.marcar(u, 'B')
            presencia.salir_pestana(u, 'A')          # cerró una de dos: sigue en línea
        self.assertIn(u.pk, presencia.conectados([u.pk]))
        with mock.patch('time.time', return_value=ahora + 1):
            presencia.salir_pestana(u, 'B')          # navega a otra página…
            presencia.marcar(u, 'B')                 # …y la página nueva late enseguida
        self.assertIn(u.pk, presencia.conectados([u.pk]))
        self.assertEqual(SesionConexion.objects.filter(usuario=u).count(), 1)  # sin cortes de sesión
        with mock.patch('time.time', return_value=ahora + 2):
            presencia.salir_pestana(u, 'B')          # cerró la última
        from django.core.cache import cache
        self.assertLessEqual(cache._expire_info[cache.make_and_validate_key(f'presencia:{u.pk}')] - time.time(),
                             presencia.GRACIA + 5)   # vence en segundos, no en minutos

    def test_endpoint_salir(self):
        from core import presencia
        u = User.objects.create_user('pres2', password='x')
        self.client.force_login(u)
        self.client.get('/pulso/', {'tab': 'T1'})
        self.assertIn(u.pk, presencia.conectados([u.pk]))
        self.assertEqual(self.client.post('/pulso/salir/', {'tab': 'T1'}).status_code, 204)


class AusenteTests(TestCase):
    def test_ausente_es_informativo(self):
        from core import presencia
        u = User.objects.create_user('aus', password='x')
        self.client.force_login(u)
        self.client.get('/pulso/', {'tab': 'T', 'sin_uso': 20 * 60})   # 20 min sin tocar el CRM
        self.assertIn(u.pk, presencia.ausentes([u.pk]))
        self.assertIn(u.pk, presencia.conectados([u.pk]))             # sigue contando para el reparto
        self.assertNotIn(u.pk, presencia.ausentes([u.pk], en_llamada={u.pk}))  # en llamada nunca ausente
        self.client.get('/pulso/', {'tab': 'T', 'sin_uso': 3})          # movió el mouse
        self.assertEqual(presencia.ausentes([u.pk]), {})
