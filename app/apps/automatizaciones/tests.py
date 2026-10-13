from datetime import timedelta
from unittest import mock

from django.core.management import call_command
from django.utils import timezone

from core.testing import TestCase
from apps.crm import services as crm
from apps.crm.models import Embudo, Etapa
from apps.users.models import User
from apps.whatsapp.models import LineaWhatsApp, Mensaje

from .models import AccionEtapa, EjecucionAccion
from .services import ejecutar, proxima_apertura, revisar_inactividad


class AutomatizacionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command('setup_inicial', '--sin-admin', verbosity=0)
        cls.embudo = Embudo.objects.get()
        cls.agente = User.objects.create_user('ana', password='x', first_name='Ana')
        cls.embudo.agentes.set([cls.agente])
        cls.linea = LineaWhatsApp.objects.create(nombre='Demo', proveedor='demo', min_segundos_entre_envios=0)
        Embudo.objects.filter(pk=cls.embudo.pk).update(linea_whatsapp=cls.linea)
        AccionEtapa.objects.update(activa=True, solo_en_horario=False)

    def ingresar(self, tel='1155556666'):
        with self.captureOnCommitCallbacks(execute=True):
            return crm.ingresar_prospecto({'telefono': tel, 'nombre': 'María López'}, Embudo.objects.get(), 'importacion')

    @mock.patch('apps.whatsapp.proveedores.demo.ProveedorDemo._quizas_responder')
    def test_bienvenida_al_ingresar(self, _):
        with self.captureOnCommitCallbacks(execute=True):
            res = self.ingresar()
        ej = EjecucionAccion.objects.get(oportunidad=res.oportunidad)
        self.assertEqual(ej.estado, 'ejecutada', ej.detalle)
        msg = Mensaje.objects.get()
        self.assertIn('María', msg.contenido)
        self.assertIn('Ana', msg.contenido)
        self.assertTrue(msg.automatico)

    @mock.patch('apps.whatsapp.proveedores.demo.ProveedorDemo._quizas_responder')
    def test_no_contactar_y_pausa_bloquean(self, _):
        res = crm.ingresar_prospecto({'telefono': '1100001111', 'nombre': 'X'}, self.embudo, 'manual',
                                     disparar_automatizaciones=False)
        op = res.oportunidad
        res.contacto.no_contactar = True
        res.contacto.save()
        accion = AccionEtapa.objects.get(etapa=self.embudo.etapa_inicial)
        ej = EjecucionAccion.objects.create(accion=accion, oportunidad=op, programada_para=timezone.now())
        ejecutar(ej.pk)
        ej.refresh_from_db()
        self.assertEqual(ej.estado, 'omitida')
        self.assertEqual(Mensaje.objects.count(), 0)

    def test_demora_y_si_ya_avanzo_se_omite(self):
        etapa = Etapa.objects.get(nombre='Propuesta enviada')
        AccionEtapa.objects.filter(etapa=etapa).update(demora_minutos=60 * 24)
        op = self.ingresar().oportunidad
        with self.captureOnCommitCallbacks(execute=True):
            crm.mover_etapa(op, etapa, self.agente)
        ej = EjecucionAccion.objects.get(accion__etapa=etapa)
        self.assertEqual(ej.estado, 'programada')
        self.assertGreater(ej.programada_para, timezone.now() + timedelta(hours=23))
        crm.mover_etapa(op, Etapa.objects.get(nombre='Negociación'), self.agente)
        EjecucionAccion.objects.filter(pk=ej.pk).update(programada_para=timezone.now())
        ejecutar(ej.pk)
        ej.refresh_from_db()
        self.assertEqual(ej.estado, 'omitida')

    def test_idempotente(self):
        op = self.ingresar().oportunidad
        from .services import programar_acciones_de_etapa
        hist = op.historial.first()
        self.assertEqual(programar_acciones_de_etapa(op.pk, hist.pk), 0)

    def test_proxima_apertura(self):
        e = Embudo(respetar_horario=True, horario_desde='09:00', horario_hasta='18:00', dias_habiles=[0, 1, 2, 3, 4])
        sabado = timezone.make_aware(timezone.datetime(2026, 9, 26, 22, 0))
        lunes = proxima_apertura(e, sabado)
        self.assertEqual((timezone.localtime(lunes).weekday(), timezone.localtime(lunes).hour), (0, 9))

    def test_recordatorio_por_inactividad_una_sola_vez(self):
        op = crm.ingresar_prospecto({'telefono': '1122223333', 'nombre': 'Y'}, self.embudo, 'manual',
                                    disparar_automatizaciones=False).oportunidad
        op.tareas.all().delete()
        type(op).objects.filter(pk=op.pk).update(ultima_actividad_at=timezone.now() - timedelta(days=5),
                                                  etapa_desde=timezone.now() - timedelta(days=9))
        r1, r2 = revisar_inactividad(), revisar_inactividad()
        self.assertEqual((r1['recordatorios'], r1['estancados']), (1, 1))
        self.assertEqual((r2['recordatorios'], r2['estancados']), (0, 0))
        self.assertEqual(op.tareas.count(), 1)


class DisparadoresTests(TestCase):
    """Cuando responde / si no responde / sin actividad, y la acción "mover a otra etapa / cerrar"."""

    @classmethod
    def setUpTestData(cls):
        from django.core.management import call_command
        from apps.crm.models import Embudo
        from apps.users.models import User
        call_command('setup_inicial', '--sin-admin', verbosity=0)
        cls.embudo = Embudo.objects.get()
        cls.ana = User.objects.create_user('ana2', password='x')
        cls.embudo.agentes.set([cls.ana])
        cls.etapas = list(cls.embudo.etapas.filter(tipo='normal').order_by('orden'))

    def setUp(self):
        super().setUp()
        from apps.crm import services as crm
        from apps.automatizaciones.models import AccionEtapa
        AccionEtapa.objects.all().delete()
        self.A, self.crm = AccionEtapa, crm
        self.op = crm.ingresar_prospecto({'telefono': '1180000001', 'nombre': 'D'}, self.embudo, 'web').oportunidad

    def _ultima(self, accion):
        from apps.automatizaciones.models import EjecucionAccion
        return EjecucionAccion.objects.filter(accion=accion).order_by('-pk').first()

    def test_cuando_responde_mueve_de_etapa(self):
        from apps.whatsapp.models import LineaWhatsApp
        from apps.whatsapp.proveedores.base import MensajeEntrante
        from apps.whatsapp.services import procesar_mensaje_entrante
        inicial = self.etapas[0]
        acc = self.A.objects.create(embudo=self.embudo, etapa=inicial, nombre='Respondió', disparador='respuesta',
                                    tipo='etapa', mover_a=self.etapas[2], solo_en_horario=False)
        linea = LineaWhatsApp.objects.create(nombre='L', proveedor='demo')
        with self.captureOnCommitCallbacks(execute=True):
            procesar_mensaje_entrante(linea, MensajeEntrante(telefono=self.op.contacto.telefono, wa_id='r1', tipo='text',
                                                             contenido='hola', nombre_perfil='D'))
        self.op.refresh_from_db()
        self.assertEqual(self.op.etapa, self.etapas[2], self._ultima(acc).detalle)

    def test_si_no_responde_se_omite_cuando_respondio(self):
        from apps.automatizaciones.services import ejecutar, programar_acciones_de_etapa
        from apps.crm.models import HistorialEtapa
        from apps.whatsapp.models import Conversacion, LineaWhatsApp, Mensaje
        acc = self.A.objects.create(embudo=self.embudo, etapa=self.etapas[0], nombre='Seguimiento',
                                    disparador='sin_respuesta', tipo='notif_agente', demora_minutos=60,
                                    solo_en_horario=False)
        hist = HistorialEtapa.objects.filter(oportunidad=self.op).latest('pk')
        programar_acciones_de_etapa(self.op.pk, hist.pk)
        ej = self._ultima(acc)
        ej.programada_para = timezone.now() - timedelta(minutes=1)
        ej.save()
        conv = Conversacion.objects.create(linea=LineaWhatsApp.objects.create(nombre='L', proveedor='demo'),
                                           telefono=self.op.contacto.telefono, contacto=self.op.contacto)
        Mensaje.objects.create(conversacion=conv, direccion='in', contenido='sí, me interesa')
        self.assertEqual(ejecutar(ej.pk), 'omitida')
        ej.refresh_from_db()
        self.assertEqual(ej.detalle, 'El cliente respondió.')

    def test_sin_actividad_cierra_como_sin_respuesta(self):
        from apps.automatizaciones.services import ejecutar, revisar_sin_actividad
        from apps.crm.models import Oportunidad, Tipificacion
        tip = Tipificacion.objects.filter(resultado='no_venta', accion='').exclude(nombre__icontains='recontact').first()
        acc = self.A.objects.create(embudo=self.embudo, etapa=self.etapas[0], nombre='Cerrar dormidos',
                                    disparador='sin_actividad', tipo='etapa', mover_a=self.embudo.etapa_perdido,
                                    tipificacion=tip, demora_minutos=7 * 1440, solo_en_horario=False)
        hace = timezone.now() - timedelta(days=8)
        Oportunidad.objects.filter(pk=self.op.pk).update(ultima_actividad_at=hace, etapa_desde=hace)
        self.assertEqual(revisar_sin_actividad(), 1)
        self.assertEqual(revisar_sin_actividad(), 0)  # una sola vez por entrada a la etapa
        ejecutar(self._ultima(acc).pk)
        self.op.refresh_from_db()
        self.assertEqual((self.op.estado, self.op.tipificacion), ('perdida', tip))


class CorreoYDifusionesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command('setup_inicial', '--sin-admin', verbosity=0)
        cls.embudo = Embudo.objects.get()
        cls.ana = User.objects.create_user('ana5', password='x')
        cls.embudo.agentes.set([cls.ana])
        cls.jefa = User.objects.create_user('jefa5', password='x', rol=User.ROL_SUPERVISOR, email='jefa@x.com')

    def setUp(self):
        super().setUp()
        from .models import PlantillaEmail
        self.pe = PlantillaEmail.objects.create(nombre='Promo', asunto='Hola {primer_nombre}',
                                                cuerpo='Mirá el plan https://ejemplo.com/plan')
        self.ops = [crm.ingresar_prospecto({'telefono': f'11810000{i:02d}', 'nombre': f'Persona{i} X',
                                            'email': f'p{i}@x.com' if i != 2 else ''}, self.embudo, 'web').oportunidad
                    for i in range(4)]
        self.client.force_login(self.jefa)

    def crear_desde_lista(self):
        r = self.client.post('/oportunidades/masivas/', {'accion': 'difusion', 'ids': [o.pk for o in self.ops]})
        from .models import Difusion
        dif = Difusion.objects.latest('pk')
        self.assertRedirects(r, f'/automatizaciones/difusiones/{dif.pk}/')
        return dif

    def test_difusion_por_email_con_baja(self):
        from django.core import mail
        from .difusiones import tick
        from .models import Difusion, EmailEnviado
        dif = self.crear_desde_lista()
        self.assertEqual(dif.destinatarios.count(), 4)
        r = self.client.post(f'/automatizaciones/difusiones/{dif.pk}/', {'accion': 'enviar', 'nombre': 'Promo octubre',
                                                                        'canal': 'email', 'plantilla_email': self.pe.pk})
        self.assertEqual(r.status_code, 302)
        tick()
        dif.refresh_from_db()
        self.assertEqual(dif.estado, Difusion.FINALIZADA)
        self.assertEqual(len(mail.outbox), 3)  # el que no tiene email se omite
        m = next(x for x in mail.outbox if x.to == ['p0@x.com'])  # el orden de envío no está garantizado
        self.assertEqual(m.subject, 'Hola Persona0')
        self.assertIn('List-Unsubscribe', m.extra_headers)
        self.assertIn('/e/baja/', m.alternatives[0][0])
        envio = EmailEnviado.objects.filter(difusion=dif).first()
        self.client.logout()
        self.assertContains(self.client.get(f'/e/baja/{envio.token}/'), 'Dejar de recibir')
        self.client.post(f'/e/baja/{envio.token}/')
        envio.contacto.refresh_from_db()
        self.assertTrue(envio.contacto.no_email)
        from .difusiones import resultados
        r = resultados(dif)
        self.assertEqual((r['enviados'], r['omitidos'], r['bajas']), (3, 1, 1))

    def test_difusion_por_whatsapp(self):
        from apps.whatsapp.models import LineaWhatsApp, Mensaje
        from .difusiones import tick
        LineaWhatsApp.objects.create(nombre='Demo', proveedor='demo')
        dif = self.crear_desde_lista()
        self.client.post(f'/automatizaciones/difusiones/{dif.pk}/', {'accion': 'enviar', 'nombre': 'WA', 'canal': 'whatsapp',
                                                                     'texto_wa': 'Hola {primer_nombre}!'})
        with mock.patch('apps.whatsapp.proveedores.demo.ProveedorDemo._quizas_responder'):
            tick()
        self.assertEqual(Mensaje.objects.filter(direccion='out').count(), 4)
        self.assertEqual(Mensaje.objects.filter(contenido='Hola Persona1!').count(), 1)

    def test_borrador_sin_mensaje_no_sale_y_cancelar(self):
        from .models import Difusion
        dif = self.crear_desde_lista()
        r = self.client.post(f'/automatizaciones/difusiones/{dif.pk}/', {'accion': 'enviar', 'nombre': 'x', 'canal': 'email'})
        self.assertContains(r, 'Elegí la plantilla de email')
        self.client.post(f'/automatizaciones/difusiones/{dif.pk}/', {'accion': 'cancelar'})
        dif.refresh_from_db()
        self.assertEqual(dif.estado, Difusion.CANCELADA)

    def test_agente_sin_permiso_no_crea_difusion(self):
        self.client.force_login(self.ana)
        r = self.client.post('/oportunidades/masivas/', {'accion': 'difusion', 'ids': [self.ops[0].pk]})
        self.assertEqual(r.status_code, 403)

    def test_config_email_desde_pantalla(self):
        from .models import ConfigEmail
        from .services import conexion_email
        admin = User.objects.create_user('adm5', password='x', rol=User.ROL_ADMIN)
        self.client.force_login(admin)
        self.client.post('/automatizaciones/correo/', {'activo': 'on', 'host': 'smtp-relay.brevo.com', 'puerto': 587,
                                                      'seguridad': 'tls', 'usuario': 'u', 'password': 'clave',
                                                      'remitente': 'Ventas <v@x.com>', 'por_minuto': 40})
        c = ConfigEmail.get()
        self.assertEqual((c.host, c.password), ('smtp-relay.brevo.com', 'clave'))
        self.client.post('/automatizaciones/correo/', {'activo': 'on', 'host': 'smtp-relay.brevo.com', 'puerto': 587,
                                                      'seguridad': 'tls', 'usuario': 'u', 'password': '',
                                                      'remitente': 'Ventas <v@x.com>', 'por_minuto': 40})
        self.assertEqual(ConfigEmail.get().password, 'clave')  # vacío no la borra
        con, remitente = conexion_email()
        self.assertEqual((con.host, remitente), ('smtp-relay.brevo.com', 'Ventas <v@x.com>'))

    def test_secuencia_despues_de_otra_automatizacion(self):
        from .models import EjecucionAccion
        etapa = self.embudo.etapa_inicial
        a1 = AccionEtapa.objects.create(embudo=self.embudo, etapa=etapa, nombre='Bienvenida', tipo='email',
                                        plantilla_email=self.pe, solo_en_horario=False)
        a2 = AccionEtapa.objects.create(embudo=self.embudo, etapa=etapa, nombre='Recordatorio', tipo='notif_agente',
                                        disparador='despues_de', accion_previa=a1, demora_minutos=3 * 1440,
                                        solo_en_horario=False)
        op = crm.ingresar_prospecto({'telefono': '1181009999', 'nombre': 'Seq', 'email': 's@x.com'}, self.embudo, 'web').oportunidad
        from apps.crm.models import HistorialEtapa
        from .services import programar_acciones_de_etapa
        programar_acciones_de_etapa(op.pk, HistorialEtapa.objects.filter(oportunidad=op).latest('pk').pk)
        e1 = EjecucionAccion.objects.get(accion=a1, oportunidad=op)  # en tests Celery corre al instante
        self.assertEqual(e1.estado, 'ejecutada', e1.detalle)
        e2 = EjecucionAccion.objects.get(accion=a2, oportunidad=op)
        self.assertEqual(e2.estado, 'programada')
        self.assertAlmostEqual((e2.programada_para - timezone.now()).total_seconds(), 3 * 86400, delta=60)

    def test_formulario_secuencia_toma_la_etapa(self):
        from .views import AccionForm
        a1 = AccionEtapa.objects.create(embudo=self.embudo, etapa=self.embudo.etapa_inicial, nombre='B', tipo='notif_agente')
        f = AccionForm({'nombre': 'Seguimiento', 'disparador': 'despues_de', 'accion_previa': a1.pk, 'tipo': 'email',
                        'plantilla_email': self.pe.pk, 'demora_valor': 3, 'demora_unidad': 'd', 'tarea_vence_horas': 24,
                        'modo_embudo': 'crear', 'volver_a': 'misma', 'asignar_destino': 'mismo'}, embudo=self.embudo)
        self.assertTrue(f.is_valid(), f.errors)
        acc = f.save()
        self.assertEqual((acc.etapa, acc.demora_minutos), (self.embudo.etapa_inicial, 3 * 1440))
