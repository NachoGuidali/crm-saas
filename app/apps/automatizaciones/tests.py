from datetime import timedelta
from unittest import mock

from django.core.management import call_command
from django.utils import timezone

from core.testing import TestCase
from apps.crm import services as crm
from apps.crm.models import Embudo, Etapa
from apps.users.models import User
from apps.whatsapp.models import LineaWhatsApp, Mensaje

from .models import AccionEtapa, CondicionCampo, EjecucionAccion
from .services import campos_cambiaron, ejecutar, proxima_apertura, revisar_inactividad


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

    def test_plantilla_html_se_envia_tal_cual_con_texto_de_respaldo(self):
        from django.core import mail
        from .models import PlantillaEmail
        from .services import enviar_email
        pe = PlantillaEmail.objects.create(
            nombre='Promo HTML', asunto='Oferta', es_html=True,
            cuerpo='<h1>Hola {primer_nombre}</h1><img src="https://ejemplo.com/foto.jpg">'
                   '<a href="https://ejemplo.com/plan">Ver plan</a>')
        op = self.ops[0]
        enviar_email(op.contacto, op, 'Oferta', pe.cuerpo.replace('{primer_nombre}', 'Persona0'), plantilla=pe)
        m = mail.outbox[-1]
        html = m.alternatives[0][0]
        self.assertIn('<img src="https://ejemplo.com/foto.jpg">', html)  # el HTML se respeta
        self.assertIn('/e/c/', html)  # el link se reescribe para medir clics
        self.assertIn('/e/o/', html)  # píxel de apertura
        self.assertNotIn('<h1>', m.body)  # la parte de texto va sin etiquetas
        self.assertIn('Hola Persona0', m.body)

    def test_subir_imagen_email_y_acceso_publico(self):
        import io
        import tempfile
        from urllib.parse import urlsplit
        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.test import override_settings
        from PIL import Image
        buf = io.BytesIO(); Image.new('RGB', (4, 4), 'red').save(buf, 'PNG')
        up = SimpleUploadedFile('flyer.png', buf.getvalue(), content_type='image/png')
        with override_settings(MEDIA_ROOT=tempfile.mkdtemp()):
            r = self.client.post('/automatizaciones/plantillas-email/imagenes/', {'imagen': up})
            self.assertEqual(r.status_code, 200)
            url = r.json()['url']
            self.assertIn('/media/email/', url)
            self.client.logout()  # un cliente de correo no tiene sesión en el CRM
            pub = self.client.get(urlsplit(url).path)
            self.assertEqual(pub.status_code, 200)  # la imagen de email es pública

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


class CondicionCampoCumpleTests(TestCase):
    """La comparación de un campo del contacto contra un valor (sin tocar la base de embudos)."""

    def _cond(self, operador, valor=''):
        from apps.crm.models import CampoPersonalizado
        campo = CampoPersonalizado(slug='es_socio', nombre='Es socio', tipo=CampoPersonalizado.TIPO_SINO)
        return CondicionCampo(campo=campo, operador=operador, valor=valor)

    class _C:
        def __init__(self, extra):
            self.datos_extra = extra

    def test_igual_sino(self):
        c = self._cond(CondicionCampo.OP_IGUAL, 'Sí')  # el usuario escribe "Sí"; guardado es booleano True
        self.assertTrue(c.cumple(self._C({'es_socio': True})))
        self.assertFalse(c.cumple(self._C({'es_socio': False})))
        self.assertFalse(c.cumple(self._C({})))

    def test_distinto(self):
        c = self._cond(CondicionCampo.OP_DISTINTO, 'Sí')
        self.assertTrue(c.cumple(self._C({'es_socio': False})))
        self.assertTrue(c.cumple(self._C({})))  # vacío es distinto de Sí
        self.assertFalse(c.cumple(self._C({'es_socio': True})))

    def test_lleno_vacio(self):
        self.assertTrue(self._cond(CondicionCampo.OP_LLENO).cumple(self._C({'es_socio': False})))
        self.assertFalse(self._cond(CondicionCampo.OP_LLENO).cumple(self._C({'es_socio': ''})))
        self.assertTrue(self._cond(CondicionCampo.OP_VACIO).cumple(self._C({})))

    def test_contiene(self):
        c = self._cond(CondicionCampo.OP_CONTIENE, 'oro')
        c.campo.tipo = 'texto'
        self.assertTrue(c.cumple(self._C({'es_socio': 'Plan Oro'})))
        self.assertFalse(c.cumple(self._C({'es_socio': 'Plan Plata'})))

    def _cond_num(self, operador, valor):
        from apps.crm.models import CampoPersonalizado
        campo = CampoPersonalizado(slug='edad', nombre='Edad', tipo=CampoPersonalizado.TIPO_NUMERO)
        return CondicionCampo(campo=campo, operador=operador, valor=valor)

    def test_comparacion_numerica(self):
        self.assertTrue(self._cond_num(CondicionCampo.OP_MAYOR, '18').cumple(self._C({'edad': 25})))
        self.assertFalse(self._cond_num(CondicionCampo.OP_MAYOR, '18').cumple(self._C({'edad': 18})))
        self.assertTrue(self._cond_num(CondicionCampo.OP_MAYOR_IGUAL, '18').cumple(self._C({'edad': '18'})))
        self.assertTrue(self._cond_num(CondicionCampo.OP_MENOR, '18').cumple(self._C({'edad': '17'})))
        self.assertTrue(self._cond_num(CondicionCampo.OP_MENOR_IGUAL, '18').cumple(self._C({'edad': '18,0'})))
        self.assertFalse(self._cond_num(CondicionCampo.OP_MENOR, '18').cumple(self._C({'edad': ''})))  # vacío no compara

    def test_comparacion_fecha(self):
        from apps.crm.models import CampoPersonalizado
        campo = CampoPersonalizado(slug='vence', nombre='Vence', tipo=CampoPersonalizado.TIPO_FECHA)
        c = CondicionCampo(campo=campo, operador=CondicionCampo.OP_MENOR, valor='2026-01-01')
        self.assertTrue(c.cumple(self._C({'vence': '2025-12-31'})))
        self.assertFalse(c.cumple(self._C({'vence': '2026-06-01'})))

    def test_operadores_por_tipo(self):
        from apps.crm.models import CampoPersonalizado as Cp
        self.assertIn(CondicionCampo.OP_MAYOR, CondicionCampo.operadores_para(Cp.TIPO_NUMERO))
        self.assertIn(CondicionCampo.OP_MAYOR, CondicionCampo.operadores_para(Cp.TIPO_FECHA))
        self.assertNotIn(CondicionCampo.OP_MAYOR, CondicionCampo.operadores_para(Cp.TIPO_SINO))
        self.assertIn(CondicionCampo.OP_CONTIENE, CondicionCampo.operadores_para(Cp.TIPO_TEXTO))
        self.assertNotIn(CondicionCampo.OP_CONTIENE, CondicionCampo.operadores_para(Cp.TIPO_NUMERO))


class AutomatizacionPorCampoTests(TestCase):
    """Disparador "cuando cambia un campo": el agente/bot solo marca el campo y la tarjeta se mueve sola."""

    @classmethod
    def setUpTestData(cls):
        call_command('setup_inicial', '--sin-admin', verbosity=0)
        cls.embudo = Embudo.objects.get()
        cls.ana = User.objects.create_user('ana_campo', password='x')
        cls.embudo.agentes.set([cls.ana])
        cls.inicial = cls.embudo.etapas.filter(tipo='normal').order_by('orden').first()

    def setUp(self):
        super().setUp()
        from apps.crm.models import CampoPersonalizado
        AccionEtapa.objects.all().delete()
        self.socio = CampoPersonalizado.objects.create(nombre='Es socio', tipo=CampoPersonalizado.TIPO_SINO)
        self.auth = CampoPersonalizado.objects.create(nombre='Autorizaciones', tipo=CampoPersonalizado.TIPO_SINO)
        self.op = crm.ingresar_prospecto({'telefono': '1190000001', 'nombre': 'Campo'}, self.embudo, 'web').oportunidad

    def _embudo(self, nombre):
        e = Embudo.objects.create(nombre=nombre)
        Etapa.objects.create(embudo=e, nombre='Nuevo', orden=0, tipo=Etapa.TIPO_NORMAL)
        e.agentes.set([self.ana])
        return e

    def _automatizacion(self, destino, condiciones, orden=0):
        acc = AccionEtapa.objects.create(
            embudo=self.embudo, etapa=self.inicial, nombre=f'A {destino}', disparador=AccionEtapa.DISP_CAMPO,
            tipo=AccionEtapa.TIPO_EMBUDO, modo_embudo=AccionEtapa.MODO_MOVER, embudo_destino=destino,
            asignar_destino='embudo', solo_en_horario=False, orden=orden)
        for campo, valor in condiciones:
            CondicionCampo.objects.create(accion=acc, campo=campo, operador=CondicionCampo.OP_IGUAL, valor=valor)
        return acc

    def _marcar(self, **campos):
        contacto = self.op.contacto
        extra_antes = dict(contacto.datos_extra or {})
        contacto.datos_extra = {**extra_antes, **campos}
        contacto.save(update_fields=['datos_extra'])
        with self.captureOnCommitCallbacks(execute=True):
            crm.avisar_campos_cambiados(contacto, extra_antes, self.ana)

    def test_form_adapta_operadores_segun_tipo(self):
        from django.test import Client
        from apps.crm.models import CampoPersonalizado
        CampoPersonalizado.objects.create(nombre='Edad', tipo=CampoPersonalizado.TIPO_NUMERO)
        admin = User.objects.create_user('jefe_campo', password='x', rol=User.ROL_ADMIN)
        cl = Client()
        cl.force_login(admin)
        r = cl.get(f'/automatizaciones/nueva/?etapa={self.inicial.pk}')
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn('data-tipo="numero"', html)          # la opción del campo lleva su tipo
        self.assertIn('es mayor que', html)                # etiqueta del operador numérico, para el front
        self.assertIn('"por_tipo"', html)                  # metadata de operadores por tipo

    def test_marcar_socio_mueve_al_embudo_socios(self):
        socios = self._embudo('Socios')
        self._automatizacion(socios, [(self.socio, 'true')])
        self._marcar(es_socio=True)
        self.op.refresh_from_db()
        self.assertEqual(self.op.embudo, socios)

    def test_filtro_no_se_cumple_no_mueve(self):
        socios = self._embudo('Socios')
        self._automatizacion(socios, [(self.socio, 'true')])
        self._marcar(es_socio=False)
        self.op.refresh_from_db()
        self.assertEqual(self.op.embudo, self.embudo)  # sigue en el de ventas

    def test_dos_campos_gana_la_mas_especifica(self):
        socios = self._embudo('Socios')
        autorizaciones = self._embudo('Autorizaciones')
        # Más específica primero (orden 0): socio Y autorizaciones -> Autorizaciones. La general después.
        self._automatizacion(autorizaciones, [(self.socio, 'true'), (self.auth, 'true')], orden=0)
        self._automatizacion(socios, [(self.socio, 'true')], orden=1)
        self._marcar(es_socio=True, autorizaciones=True)
        self.op.refresh_from_db()
        self.assertEqual(self.op.embudo, autorizaciones)

    def test_solo_socio_va_a_socios(self):
        socios = self._embudo('Socios')
        autorizaciones = self._embudo('Autorizaciones')
        self._automatizacion(autorizaciones, [(self.socio, 'true'), (self.auth, 'true')], orden=0)
        self._automatizacion(socios, [(self.socio, 'true')], orden=1)
        self._marcar(es_socio=True)  # autorizaciones sigue vacío
        self.op.refresh_from_db()
        self.assertEqual(self.op.embudo, socios)

    def test_el_chat_de_whatsapp_sigue_al_nuevo_agente(self):
        from apps.whatsapp.models import Conversacion, LineaWhatsApp
        beto = User.objects.create_user('beto_campo', password='x')
        socios = self._embudo('Socios')
        socios.agentes.set([beto])
        linea = LineaWhatsApp.objects.create(nombre='L', proveedor='demo')
        conv = Conversacion.objects.create(linea=linea, telefono=self.op.contacto.telefono,
                                           contacto=self.op.contacto, agente=self.ana)
        self._automatizacion(socios, [(self.socio, 'true')])
        self._marcar(es_socio=True)
        self.op.refresh_from_db()
        conv.refresh_from_db()
        self.assertEqual(self.op.embudo, socios)
        self.assertEqual(self.op.agente, beto)      # repartida entre los agentes del embudo Socios
        self.assertEqual(conv.agente, beto)         # el chat lo sigue
