from django.core.management import call_command
from django.db import connection
from django.urls import reverse
from django.utils import timezone

from core.testing import TestCase
from apps.crm.models import Contacto, Embudo, Oportunidad, Tipificacion
from apps.crm import services as crm
from apps.users.models import User

from datetime import date

from .models import AvisoRecurrente, Cuenta, EstadoProyecto, Proyecto, TareaPostventa
from . import services
from .tasks import enviar_avisos_recurrentes


def _habilitar_postventa(habilitada=True):
    """Prende/apaga la función extra 'postventa' en el cliente de tests."""
    connection.tenant.funciones_extra = ['postventa'] if habilitada else []
    connection.tenant.save(update_fields=['funciones_extra'])


class BasePostventa(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command('setup_inicial', '--sin-admin', verbosity=0)
        cls.embudo = Embudo.objects.get()
        cls.vendedor = User.objects.create_user('ana', password='x', first_name='Ana')
        cls.embudo.agentes.set([cls.vendedor])

    def setUp(self):
        super().setUp()
        _habilitar_postventa(True)

    def tearDown(self):
        _habilitar_postventa(False)
        super().tearDown()

    def _contacto(self, nombre='Acme SA', tel='1123456789'):
        return Contacto.objects.create(nombre=nombre, telefono=tel, email='hola@acme.com')


class EstadosProyectoTests(BasePostventa):
    def test_crea_columnas_por_defecto_una_sola_vez(self):
        self.assertEqual(EstadoProyecto.objects.count(), 0)
        estados = services.estados_proyecto()
        self.assertEqual(len(estados), 4)
        self.assertTrue(EstadoProyecto.objects.filter(final=True).exists())
        # Idempotente: no vuelve a crearlas.
        services.estados_proyecto()
        self.assertEqual(EstadoProyecto.objects.count(), 4)


class ConversionAlGanarTests(BasePostventa):
    def setUp(self):
        super().setUp()
        self.embudo.crear_cuenta_al_ganar = True
        self.embudo.save(update_fields=['crear_cuenta_al_ganar'])

    def _ganar(self):
        r = crm.ingresar_prospecto({'telefono': '1123456789', 'nombre': 'Acme SA', 'email': 'hola@acme.com'},
                                   self.embudo, Oportunidad.ORIGEN_MANUAL, usuario=self.vendedor)
        op = r.oportunidad
        crm.mover_etapa(op, self.embudo.etapa_ganado, self.vendedor,
                        tipificacion=Tipificacion.objects.get(nombre='Venta directa'))
        return op

    def test_al_ganar_se_crea_la_cuenta(self):
        op = self._ganar()
        cuenta = Cuenta.objects.get()
        self.assertEqual(cuenta.nombre, 'Acme SA')
        self.assertIn(op.contacto, cuenta.contactos.all())
        self.assertEqual(cuenta.origen_oportunidad_id, op.pk)

    def test_sin_funcion_no_crea_cuenta(self):
        _habilitar_postventa(False)
        self._ganar()
        self.assertEqual(Cuenta.objects.count(), 0)

    def test_embudo_sin_el_toggle_no_crea_cuenta(self):
        self.embudo.crear_cuenta_al_ganar = False
        self.embudo.save(update_fields=['crear_cuenta_al_ganar'])
        self._ganar()
        self.assertEqual(Cuenta.objects.count(), 0)

    def test_no_duplica_cuenta_si_el_contacto_ya_tiene(self):
        op = self._ganar()
        # Segunda venta del mismo contacto no crea otra cuenta.
        services.crear_cuenta_desde_oportunidad(op)
        self.assertEqual(Cuenta.objects.filter(contactos=op.contacto).count(), 1)


class MoverProyectoTests(BasePostventa):
    def test_mover_cambia_el_estado(self):
        estados = services.estados_proyecto()
        cuenta = Cuenta.objects.create(nombre='Acme SA')
        p = Proyecto.objects.create(cuenta=cuenta, nombre='Web nueva', estado=estados[0])
        services.mover_proyecto(p, estados[2])
        p.refresh_from_db()
        self.assertEqual(p.estado, estados[2])


class AvisosRecurrentesTests(BasePostventa):
    def test_proxima_fecha_respeta_dia_y_salta_meses(self):
        self.assertEqual(services.proxima_fecha(date(2026, 1, 15), 1, 15), date(2026, 2, 15))
        self.assertEqual(services.proxima_fecha(date(2026, 12, 10), 1, 10), date(2027, 1, 10))
        self.assertEqual(services.proxima_fecha(date(2026, 3, 31), 1, 31), date(2026, 4, 30))  # abril no tiene 31
        self.assertEqual(services.proxima_fecha(date(2026, 1, 1), 2, 5), date(2026, 3, 5))

    def test_primer_envio_este_mes_o_el_proximo(self):
        self.assertEqual(services.primer_envio(1, 20, hoy=date(2026, 6, 10)), date(2026, 6, 20))
        self.assertEqual(services.primer_envio(1, 5, hoy=date(2026, 6, 10)), date(2026, 7, 5))

    def test_disparar_agenda_el_proximo_aunque_no_haya_linea(self):
        cuenta = Cuenta.objects.create(nombre='Acme SA')
        cuenta.contactos.add(self._contacto())
        aviso = AvisoRecurrente.objects.create(cuenta=cuenta, mensaje='Hola {primer_nombre}',
                                               cada_meses=1, dia_mes=10, proximo_envio=date(2026, 6, 10))
        services.disparar_aviso(aviso, hoy=date(2026, 6, 10))
        aviso.refresh_from_db()
        self.assertEqual(aviso.proximo_envio, date(2026, 7, 10))
        self.assertIn('Omitido', aviso.ultimo_detalle)  # no hay línea de WhatsApp en los tests
        self.assertIsNotNone(aviso.ultimo_envio_at)

    def test_la_tarea_toma_los_vencidos(self):
        cuenta = Cuenta.objects.create(nombre='Acme SA')
        cuenta.contactos.add(self._contacto())
        AvisoRecurrente.objects.create(cuenta=cuenta, mensaje='x', proximo_envio=timezone.localdate())
        AvisoRecurrente.objects.create(cuenta=cuenta, mensaje='futuro',
                                       proximo_envio=date(2099, 1, 1))
        self.assertEqual(enviar_avisos_recurrentes(), 1)

    def test_la_tarea_no_hace_nada_sin_funcion(self):
        _habilitar_postventa(False)
        cuenta = Cuenta.objects.create(nombre='Acme SA')
        AvisoRecurrente.objects.create(cuenta=cuenta, mensaje='x', proximo_envio=timezone.localdate())
        self.assertEqual(enviar_avisos_recurrentes(), 0)


class AccesoYVistasTests(BasePostventa):
    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_user('jefe', password='x', rol=User.ROL_ADMIN)
        self.agente = User.objects.create_user('beto', password='x', rol=User.ROL_AGENTE)

    def test_admin_ve_la_lista(self):
        self.client.force_login(self.admin)
        r = self.client.get(reverse('cuentas:lista'))
        self.assertEqual(r.status_code, 200)

    def test_agente_sin_permiso_no_entra(self):
        self.client.force_login(self.agente)
        r = self.client.get(reverse('cuentas:lista'))
        self.assertEqual(r.status_code, 403)

    def test_sin_funcion_la_seccion_no_existe(self):
        _habilitar_postventa(False)
        self.client.force_login(self.admin)
        r = self.client.get(reverse('cuentas:lista'))
        self.assertEqual(r.status_code, 404)

    def test_crear_cuenta_por_la_vista(self):
        self.client.force_login(self.admin)
        r = self.client.post(reverse('cuentas:nueva'),
                             {'nombre': 'Nueva SA', 'email': '', 'telefono': '', 'notas': '', 'activa': 'on'})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(Cuenta.objects.filter(nombre='Nueva SA').exists())

    def test_asociar_y_quitar_contacto(self):
        self.client.force_login(self.admin)
        cuenta = Cuenta.objects.create(nombre='Acme SA')
        contacto = self._contacto(nombre='Juan Pérez')
        url = reverse('cuentas:contacto', args=[cuenta.pk])
        # Buscar: encuentra el contacto no asociado.
        r = self.client.post(url, {'accion': 'buscar', 'q': 'Juan'})
        self.assertEqual(r.status_code, 200)
        self.assertIn(contacto, r.context['candidatos'])
        # Asociar.
        r = self.client.post(url, {'accion': 'agregar', 'contacto': contacto.pk})
        self.assertEqual(r.status_code, 302)
        self.assertIn(contacto, cuenta.contactos.all())
        # Ya asociado: no vuelve a aparecer en la búsqueda.
        r = self.client.post(url, {'accion': 'buscar', 'q': 'Juan'})
        self.assertNotIn(contacto, r.context['candidatos'])
        # Quitar.
        r = self.client.post(url, {'accion': 'quitar', 'contacto': contacto.pk})
        self.assertEqual(r.status_code, 302)
        self.assertNotIn(contacto, cuenta.contactos.all())

    def test_completar_tarea(self):
        self.client.force_login(self.admin)
        cuenta = Cuenta.objects.create(nombre='Acme SA')
        t = TareaPostventa.objects.create(cuenta=cuenta, titulo='Llamar', asignado_a=self.admin)
        r = self.client.post(reverse('cuentas:tarea_completar', args=[t.pk]))
        self.assertEqual(r.status_code, 302)
        t.refresh_from_db()
        self.assertEqual(t.estado, TareaPostventa.ESTADO_COMPLETADA)
        self.assertIsNotNone(t.completada_at)
