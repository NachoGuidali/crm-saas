"""
Aislamiento entre clientes del SaaS. Corren con dos clientes que arma core.test_runner:
"test" (testserver) y "test_otro" (otro.testserver), más el dominio de la plataforma (plataforma.testserver).
"""
from django.db import connection
from django.test import Client
from django_tenants.utils import get_public_schema_name, schema_context

from apps.crm.models import Contacto
from apps.integraciones.models import ApiKey
from apps.users.models import User
from apps.whatsapp.models import LineaWhatsApp
from core import tenancy
from core.test_runner import SCHEMA_OTRO, SCHEMA_TESTS
from core.testing import TestCase

from .models import Cliente, Plan
from .services import schema_para, validar_subdominio

OTRO = 'otro.testserver'
PLATAFORMA = 'plataforma.testserver'


class BaseMultiCliente(TestCase):
    def setUp(self):
        super().setUp()
        self.cliente = Cliente.objects.get(schema_name=SCHEMA_TESTS)
        self.otro = Cliente.objects.get(schema_name=SCHEMA_OTRO)
        self.admin = User.objects.create_user('admin', 'a@test.com', 'clave-del-cliente-1', rol=User.ROL_ADMIN)
        with schema_context(SCHEMA_OTRO):
            self.admin_otro = User.objects.create_user('admin', 'a@otro.com', 'clave-del-otro-1', rol=User.ROL_ADMIN)

    def tearDown(self):
        # El middleware deja la conexión en el schema del último request: volver al cliente de tests.
        connection.set_tenant(self.cliente)
        super().tearDown()

    def pedir(self, cliente_http, metodo, url, host, **kw):
        resp = getattr(cliente_http, metodo)(url, HTTP_HOST=host, **kw)
        connection.set_tenant(self.cliente)
        return resp


class AislamientoTests(BaseMultiCliente):
    def test_mismo_telefono_en_dos_clientes(self):
        Contacto.objects.create(nombre='Juana', telefono='+5491155550001')
        with schema_context(SCHEMA_OTRO):
            self.assertFalse(Contacto.objects.exists())
            Contacto.objects.create(nombre='Otra Juana', telefono='+5491155550001')
        self.assertEqual(Contacto.objects.get().nombre, 'Juana')

    def test_cada_cliente_ve_solo_sus_contactos(self):
        Contacto.objects.create(nombre='Prospecto Del Primero', telefono='+5491155550001')
        c1, c2 = Client(), Client()
        c1.force_login(self.admin)
        self.assertContains(c1.get('/contactos/?q=Prospecto'), 'Prospecto Del Primero')
        with schema_context(SCHEMA_OTRO):
            c2.force_login(self.admin_otro)
        resp = self.pedir(c2, 'get', '/contactos/?q=Prospecto', OTRO)
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, 'Prospecto Del Primero')

    def test_contrasena_de_un_cliente_no_sirve_en_otro(self):
        resp = self.pedir(Client(), 'post', '/usuarios/login/', OTRO,
                          data={'username': 'admin', 'password': 'clave-del-cliente-1'})
        self.assertEqual(resp.status_code, 200)  # vuelve al formulario
        resp = self.pedir(Client(), 'post', '/usuarios/login/', OTRO,
                          data={'username': 'admin', 'password': 'clave-del-otro-1'})
        self.assertEqual(resp.status_code, 302)

    def test_sesion_de_un_cliente_no_abre_otro(self):
        c = Client()
        c.force_login(self.admin)
        self.assertEqual(c.get('/contactos/').status_code, 200)
        resp = self.pedir(c, 'get', '/contactos/', OTRO)
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/usuarios/login/', resp['Location'])

    def test_api_key_de_un_cliente_no_sirve_en_otro(self):
        from .services import cargar_configuracion_inicial
        cargar_configuracion_inicial()
        _, clave = ApiKey.generar(nombre='Landing')
        datos = {'nombre': 'Lead', 'telefono': '+5491155550009'}
        resp = self.pedir(Client(), 'post', '/api/v1/leads/', OTRO, data=datos, content_type='application/json',
                          HTTP_X_API_KEY=clave)
        self.assertEqual(resp.status_code, 401)
        resp = Client().post('/api/v1/leads/', data=datos, content_type='application/json', HTTP_X_API_KEY=clave)
        self.assertIn(resp.status_code, (200, 201))

    def test_subdominio_desconocido_da_404(self):
        self.assertEqual(self.pedir(Client(), 'get', '/', 'fantasma.testserver').status_code, 404)


class SuspensionTests(BaseMultiCliente):
    def test_cliente_suspendido_no_entra_y_el_otro_si(self):
        Cliente.objects.filter(pk=self.cliente.pk).update(estado=Cliente.ESTADO_SUSPENDIDO)
        c = Client()
        c.force_login(self.admin)
        resp = c.get('/contactos/')
        self.assertEqual(resp.status_code, 403)
        self.assertContains(resp, 'suspendido', status_code=403)
        with schema_context(SCHEMA_OTRO):
            c2 = Client()
            c2.force_login(self.admin_otro)
        self.assertEqual(self.pedir(c2, 'get', '/contactos/', OTRO).status_code, 200)


class UrlsYRecursosPorClienteTests(BaseMultiCliente):
    def test_url_del_sitio_es_la_del_subdominio(self):
        with self.settings(SITE_URL='https://tucrm.com'):
            self.assertEqual(tenancy.url_sitio(), 'https://testserver')
            linea = LineaWhatsApp.objects.create(nombre='L1', proveedor='evolution')
            self.assertTrue(linea.webhook_url.startswith('https://testserver/whatsapp/webhook/'))
            with schema_context(SCHEMA_OTRO):
                self.assertEqual(tenancy.url_sitio(), 'https://otro.testserver')
            with schema_context(get_public_schema_name()):
                self.assertEqual(tenancy.url_sitio(), 'https://tucrm.com')

    def test_instancias_de_evolution_con_prefijo_del_cliente(self):
        from apps.whatsapp.proveedores.evolution import nombre_instancia, prefijo_instancia
        linea = LineaWhatsApp.objects.create(nombre='L1', proveedor='evolution')
        self.assertEqual(nombre_instancia(linea), f'crm-{SCHEMA_TESTS}-linea-{linea.pk}')
        self.assertTrue(nombre_instancia(linea).startswith(prefijo_instancia()))
        with schema_context(SCHEMA_OTRO):
            self.assertEqual(nombre_instancia(linea), f'crm-{SCHEMA_OTRO}-linea-{linea.pk}')

    def test_no_puede_tomar_la_instancia_de_otro_cliente(self):
        from apps.whatsapp.views import LineaForm
        form = LineaForm(data={'nombre': 'Robada', 'proveedor': 'evolution', 'orden': 0,
                               'min_segundos_entre_envios': 3, 'meta_api_version': 'v21.0', 'evolution_instancia': f'crm-{SCHEMA_OTRO}-linea-1'})
        self.assertFalse(form.is_valid())
        self.assertIn('evolution_instancia', form.errors)
        form = LineaForm(data={'nombre': 'Propia', 'proveedor': 'evolution', 'orden': 0,
                               'min_segundos_entre_envios': 3, 'meta_api_version': 'v21.0', 'evolution_instancia': f'crm-{SCHEMA_TESTS}-ventas'})
        self.assertTrue(form.is_valid(), form.errors)

    def test_clave_global_de_evolution_no_viaja_a_un_servidor_del_cliente(self):
        with self.settings(EVOLUTION_API_KEY='clave-de-la-plataforma'):
            compartida = LineaWhatsApp(nombre='A', proveedor='evolution')
            propia = LineaWhatsApp(nombre='B', proveedor='evolution', evolution_api_url='https://evo.cliente.com')
            self.assertEqual(compartida.get_evolution_key(), 'clave-de-la-plataforma')
            self.assertEqual(propia.get_evolution_key(), '')

    def test_archivos_en_la_carpeta_de_cada_cliente(self):
        from core.storage import media_root
        self.assertTrue(media_root().endswith(f'/{SCHEMA_TESTS}'))
        with schema_context(SCHEMA_OTRO):
            self.assertTrue(media_root().endswith(f'/{SCHEMA_OTRO}'))

    def test_cache_separada_por_cliente(self):
        from django.core.cache import cache
        cache.set('contador', 1)
        with schema_context(SCHEMA_OTRO):
            self.assertIsNone(cache.get('contador'))


class LimitesDelPlanTests(BaseMultiCliente):
    def setUp(self):
        super().setUp()
        self.plan = Plan.objects.create(nombre='Básico', max_usuarios=2, max_lineas_whatsapp=1)
        Cliente.objects.filter(pk=self.cliente.pk).update(plan=self.plan)
        self.c = Client()
        self.c.force_login(self.admin)

    def _nuevo_usuario(self, username):
        return self.c.post('/usuarios/nuevo/', {
            'username': username, 'first_name': username, 'email': f'{username}@test.com', 'rol': 'agente',
            'is_active': 'on', 'disponible': 'on', 'modo_password': 'manual', 'password_temporal': 'Temporal-123',
        })

    def test_limite_de_usuarios(self):
        self.assertEqual(self._nuevo_usuario('laura').status_code, 302)  # 2 de 2
        resp = self._nuevo_usuario('martin')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'hasta 2 usuarios')
        self.assertFalse(User.objects.filter(username='martin').exists())

    def test_limite_propio_pisa_el_del_plan(self):
        Cliente.objects.filter(pk=self.cliente.pk).update(max_usuarios=0)  # 0 = sin límite
        self.assertEqual(self._nuevo_usuario('laura').status_code, 302)
        self.assertEqual(self._nuevo_usuario('martin').status_code, 302)

    def test_limite_de_lineas(self):
        LineaWhatsApp.objects.create(nombre='L1', proveedor='evolution')
        resp = self.c.post('/whatsapp/lineas/nueva/', {'nombre': 'L2', 'proveedor': 'evolution', 'orden': 0,
                                                       'min_segundos_entre_envios': 3, 'meta_api_version': 'v21.0', 'activa': 'on'})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'hasta 1 líneas')
        self.assertEqual(LineaWhatsApp.objects.count(), 1)

    def test_limite_diario_de_whatsapp(self):
        from apps.whatsapp import services as wa
        from apps.whatsapp.models import Conversacion, LineaWhatsApp, Mensaje

        def refrescar():  # el middleware refresca el tenant en cada request real; acá lo hacemos a mano
            connection.set_tenant(Cliente.objects.get(pk=self.cliente.pk))

        Cliente.objects.filter(pk=self.cliente.pk).update(max_wa_dia=2)
        refrescar()
        self.assertEqual(wa.wa_dia_restante(), 2)
        linea = LineaWhatsApp.objects.create(nombre='L', proveedor='evolution')
        conv = Conversacion.objects.create(linea=linea, telefono='+5491155550001')
        # Un mensaje manual (automatico=False) NO consume cupo
        Mensaje.objects.create(conversacion=conv, direccion=Mensaje.DIR_SALIENTE, contenido='manual', automatico=False)
        self.assertEqual(wa.wa_dia_restante(), 2)
        # Los automáticos/difusiones sí
        for _ in range(2):
            Mensaje.objects.create(conversacion=conv, direccion=Mensaje.DIR_SALIENTE, contenido='x', automatico=True)
        self.assertEqual(wa.wa_dia_restante(), 0)
        Cliente.objects.filter(pk=self.cliente.pk).update(max_wa_dia=0)  # 0 = sin límite
        refrescar()
        self.assertIsNone(wa.wa_dia_restante())


class FuncionesExtraTests(BaseMultiCliente):
    def _refrescar(self):  # el middleware refresca el tenant en cada request real; acá a mano
        connection.set_tenant(Cliente.objects.get(pk=self.cliente.pk))

    def test_union_de_plan_y_cliente(self):
        plan = Plan.objects.create(nombre='Pro', funciones_extra=['reportes'])
        cli = Cliente(plan=plan, funciones_extra=['marca_blanca'])
        self.assertEqual(cli.funciones_habilitadas, {'reportes', 'marca_blanca'})
        self.assertTrue(cli.tiene_funcion('reportes'))        # viene del plan
        self.assertTrue(cli.tiene_funcion('marca_blanca'))    # habilitada al cliente
        self.assertFalse(cli.tiene_funcion('otra'))

    def test_sin_plan_y_sin_extras(self):
        cli = Cliente(funciones_extra=[])
        self.assertEqual(cli.funciones_habilitadas, set())
        self.assertFalse(cli.tiene_funcion('reportes'))

    def test_helper_del_tenant_actual(self):
        plan = Plan.objects.create(nombre='Pro', funciones_extra=['reportes'])
        Cliente.objects.filter(pk=self.cliente.pk).update(plan=plan, funciones_extra=['marca_blanca'])
        self._refrescar()
        self.assertTrue(tenancy.tiene_funcion('reportes'))
        self.assertTrue(tenancy.tiene_funcion('marca_blanca'))
        self.assertFalse(tenancy.tiene_funcion('inexistente'))
        self.assertEqual(tenancy.funciones_cliente(), {'reportes', 'marca_blanca'})

    def test_no_se_filtra_a_otro_cliente(self):
        Cliente.objects.filter(pk=self.cliente.pk).update(funciones_extra=['marca_blanca'])
        Cliente.objects.filter(pk=self.otro.pk).update(funciones_extra=[])
        self._refrescar()
        self.assertTrue(tenancy.tiene_funcion('marca_blanca'))
        connection.set_tenant(Cliente.objects.get(pk=self.otro.pk))
        self.assertFalse(tenancy.tiene_funcion('marca_blanca'))

    def test_sin_funciones_en_el_schema_publico(self):
        with schema_context(get_public_schema_name()):
            self.assertEqual(tenancy.funciones_cliente(), set())
            self.assertFalse(tenancy.tiene_funcion('reportes'))

    def test_el_form_toma_las_opciones_del_registro(self):
        from unittest.mock import patch

        from . import models
        from .forms import ClienteForm
        with patch.object(models, 'FUNCIONES_EXTRA', [('reportes', 'Reportes avanzados')]):
            form = ClienteForm(instance=self.cliente)
            self.assertEqual(list(form.fields['funciones_extra'].choices), [('reportes', 'Reportes avanzados')])
            datos = {'nombre': self.cliente.nombre, 'estado': self.cliente.estado,
                     'cobro': self.cliente.cobro, 'funciones_extra': ['reportes']}
            form = ClienteForm(datos, instance=self.cliente)
            self.assertTrue(form.is_valid(), form.errors)
            form.save()
        self._refrescar()
        self.assertEqual(Cliente.objects.get(pk=self.cliente.pk).funciones_extra, ['reportes'])


class PanelTests(BaseMultiCliente):
    def setUp(self):
        super().setUp()
        with schema_context(get_public_schema_name()):
            self.super = User.objects.create_superuser('plataforma', 'p@p.com', 'clave-plataforma-1')
        self.panel = Client()

    def test_solo_superusuarios_de_la_plataforma(self):
        resp = self.pedir(self.panel, 'get', '/panel/', PLATAFORMA)
        self.assertEqual(resp.status_code, 302)
        # El admin de un cliente no existe en el schema público
        resp = self.pedir(self.panel, 'post', '/panel/login/', PLATAFORMA,
                          data={'username': 'admin', 'password': 'clave-del-cliente-1'})
        self.assertEqual(resp.status_code, 200)
        resp = self.pedir(self.panel, 'post', '/panel/login/', PLATAFORMA,
                          data={'username': 'plataforma', 'password': 'clave-plataforma-1'})
        self.assertEqual(resp.status_code, 302)
        resp = self.pedir(self.panel, 'get', '/panel/', PLATAFORMA)
        self.assertContains(resp, 'Otro cliente')

    def test_el_panel_no_existe_en_el_subdominio_de_un_cliente(self):
        c = Client()
        c.force_login(self.admin)
        self.assertNotEqual(c.get('/panel/').status_code, 200)

    def test_el_crm_no_existe_en_el_dominio_principal(self):
        self.assertEqual(self.pedir(Client(), 'get', '/contactos/', PLATAFORMA).status_code, 404)

    def test_suspender_desde_el_panel(self):
        with schema_context(get_public_schema_name()):
            self.panel.force_login(self.super)
        resp = self.pedir(self.panel, 'post', f'/panel/clientes/{self.otro.pk}/', PLATAFORMA,
                          data={'accion': 'suspender'})
        self.assertEqual(resp.status_code, 302)
        self.otro.refresh_from_db()
        self.assertEqual(self.otro.estado, Cliente.ESTADO_SUSPENDIDO)

    def test_ingresar_lleva_al_subdominio(self):
        # La URL del cliente sale de SITE_URL (esquema y puerto públicos), no del request.
        with self.settings(SAAS_DOMINIO='testserver', SITE_URL='https://testserver'):
            resp = self.pedir(Client(), 'post', '/ingresar/', PLATAFORMA, data={'empresa': 'otro'})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp['Location'], 'https://otro.testserver/usuarios/login/')


class SubdominioTests(TestCase):
    def test_validaciones(self):
        from django.core.exceptions import ValidationError
        self.assertEqual(validar_subdominio('  Acme-SA '), 'acme-sa')
        self.assertEqual(schema_para('acme-sa'), 'c_acme_sa')
        for malo in ('ab', '1acme', 'acme-', 'ac--me', 'acme.sa', 'www', 'panel', 'a' * 41):
            with self.assertRaises(ValidationError, msg=malo):
                validar_subdominio(malo)


class AuditoriaYSeguridadTests(BaseMultiCliente):
    """A1, A2 y C2: IP real, bloqueo del panel y bitácora inmutable por cliente."""

    def test_ip_no_se_puede_falsear_con_xff(self):
        from django.test import RequestFactory
        from django.conf import settings
        from apps.users.services import ip_de
        rf = RequestFactory()
        # Un proxy de confianza (nginx) → la IP real es la última que agrega nginx
        req = rf.get('/', HTTP_X_FORWARDED_FOR='1.2.3.4, 200.0.0.9', REMOTE_ADDR='10.0.0.1')
        with self.settings(PROXIES_CONFIABLES=1):
            self.assertEqual(ip_de(req), '200.0.0.9')  # no la "1.2.3.4" falseada por el cliente

    def test_auditoria_es_inmutable(self):
        from apps.users.models import RegistroAuditoria
        r = RegistroAuditoria.objects.create(accion=RegistroAuditoria.LOGIN, usuario_desc='x')
        with self.assertRaises(ValueError):
            r.save()
        with self.assertRaises(ValueError):
            r.delete()

    def test_login_del_crm_queda_en_la_bitacora_del_cliente(self):
        from apps.users.models import RegistroAuditoria
        c = Client()
        self.pedir(c, 'post', '/usuarios/login/', 'testserver',
                   data={'username': 'admin', 'password': 'clave-del-cliente-1'})
        self.assertTrue(RegistroAuditoria.objects.filter(accion='login', usuario=self.admin).exists())
        # Y no se mezcla con el otro cliente
        with schema_context(SCHEMA_OTRO):
            self.assertFalse(RegistroAuditoria.objects.filter(accion='login').exists())

    def test_login_fallido_del_panel_se_audita_y_bloquea(self):
        from django.core.cache import cache
        from apps.users.models import RegistroAuditoria
        cache.clear()
        panel = Client()
        # El host del panel enruta al schema público aunque no usemos schema_context.
        for _ in range(6):
            self.pedir(panel, 'post', '/panel/login/', PLATAFORMA, data={'username': 'noexiste', 'password': 'mal'})
        ultima = self.pedir(panel, 'post', '/panel/login/', PLATAFORMA,
                            data={'username': 'noexiste', 'password': 'mal'})
        self.assertContains(ultima, 'Demasiados intentos', status_code=200)  # A2: queda bloqueado
        with schema_context(get_public_schema_name()):
            self.assertTrue(RegistroAuditoria.objects.filter(accion='login_fallido').exists())
