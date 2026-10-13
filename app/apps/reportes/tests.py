from datetime import timedelta

from django.core.management import call_command
from django.utils import timezone

from core.testing import TestCase
from apps.crm import services as crm
from apps.crm.models import Embudo, Oportunidad, Tipificacion
from apps.users.models import User

from . import analisis


class ReportesComercialesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command('setup_inicial', '--sin-admin', verbosity=0)
        cls.embudo = Embudo.objects.get()
        cls.ana = User.objects.create_user('ana', password='x', first_name='Ana')
        cls.beto = User.objects.create_user('beto', password='x', first_name='Beto')
        cls.jefa = User.objects.create_user('jefa', password='x', rol=User.ROL_ADMIN)
        cls.embudo.agentes.set([cls.ana, cls.beto])

    def rango(self):
        return timezone.now() - timedelta(days=1), timezone.now() + timedelta(minutes=1)

    def lead(self, tel):
        return crm.ingresar_prospecto({'telefono': tel, 'nombre': 'X'}, self.embudo, 'web').oportunidad

    def test_leads_unicos_y_contador_de_ingresos(self):
        a = self.lead('1160000001')
        self.lead('1160000002')
        self.lead('1160000001')  # reingreso
        self.lead('1160000001')
        a.refresh_from_db()
        self.assertEqual(a.ingresos, 3)
        u = analisis.leads_unicos(self.embudo, *self.rango())
        self.assertEqual((u['unicos'], u['nuevos'], u['reingresos'], u['eventos_reingreso']), (2, 2, 0, 2))
        self.client.force_login(self.jefa)
        r = self.client.get('/oportunidades/', {'ingresos_min': 2})
        self.assertEqual(r.context['page'].paginator.count, 1)
        self.assertContains(self.client.get(a.get_absolute_url()), '3 veces')

    def test_conversion_por_etapa_y_pipeline(self):
        etapas = list(self.embudo.etapas.filter(tipo='normal').order_by('orden'))
        ops = [self.lead(f'11600001{i:02d}') for i in range(4)]
        crm.mover_etapa(ops[0], etapas[2], self.jefa)
        crm.mover_etapa(ops[0], etapas[1], self.jefa)  # volvió atrás: igual "llegó" a la 3
        crm.mover_etapa(ops[1], etapas[1], self.jefa)
        tip = Tipificacion.objects.filter(resultado=Tipificacion.RESULTADO_VENTA).first()
        crm.mover_etapa(ops[2], self.embudo.etapa_ganado, self.jefa, tipificacion=tip, valor=100)
        conv = analisis.embudo_conversion(self.embudo, *self.rango())
        self.assertEqual([c['n'] for c in conv[:3]], [4, 3, 2])
        self.assertEqual((conv[-1]['nombre'], conv[-1]['n']), ('Venta', 1))
        self.assertEqual(conv[1]['pct_anterior'], 75.0)
        pipe = analisis.matriz_pipeline(self.embudo)
        self.assertEqual(pipe['total'], 3)
        self.assertEqual(sum(f['total'] for f in pipe['filas']), 3)

    def test_actividad_por_vendedora(self):
        from apps.telefonia.models import Llamada
        from apps.whatsapp.models import Conversacion, LineaWhatsApp, Mensaje, Plantilla
        op = self.lead('1160000300')
        linea = LineaWhatsApp.objects.create(nombre='L', proveedor='demo')
        conv = Conversacion.objects.create(linea=linea, telefono=op.contacto.telefono, contacto=op.contacto, agente=self.ana)
        p = Plantilla.objects.first()
        Mensaje.objects.create(conversacion=conv, direccion='out', contenido='hola', enviado_por=self.ana)
        Mensaje.objects.create(conversacion=conv, direccion='out', contenido='plantilla', enviado_por=self.ana, plantilla=p)
        Mensaje.objects.create(conversacion=conv, direccion='out', contenido='auto', automatico=True)
        Llamada.objects.create(direccion='OUT', estado='atendida', agente=self.ana, duracion_seg=120)
        crm.agregar_nota(op, self.ana, 'nota')
        fila = next(f for f in analisis.actividad_vendedoras(*self.rango()) if f['u'] == self.ana)
        self.assertEqual((fila['mensajes'], fila['plantillas'], fila['automaticos'], fila['llamadas'], fila['minutos'],
                          fila['promedio_seg'], fila['notas']), (2, 1, 1, 1, 2, 120, 1))
        self.client.force_login(self.jefa)
        r = self.client.get('/reportes/', {'embudo': self.embudo.pk})
        self.assertContains(r, 'Actividad por vendedora')
        self.assertContains(r, 'Conversión por etapa')
        r = self.client.get('/reportes/actividad.csv')
        self.assertIn('Ana', r.content.decode())

    def test_mis_numeros_para_la_vendedora(self):
        self.client.force_login(self.ana)
        r = self.client.get('/reportes/mis-numeros/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['agente'], self.ana)
        r = self.client.get('/reportes/mis-numeros/', {'agente': self.beto.pk})  # sin permiso: ve lo suyo
        self.assertEqual(r.context['agente'], self.ana)
        self.client.force_login(self.jefa)
        self.assertEqual(self.client.get('/reportes/mis-numeros/', {'agente': self.beto.pk}).context['agente'], self.beto)


class CalidadEnviosTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command('setup_inicial', '--sin-admin', verbosity=0)
        cls.embudo = Embudo.objects.get()
        cls.ana = User.objects.create_user('ana', password='x')
        cls.embudo.agentes.set([cls.ana])
        cls.jefa = User.objects.create_user('jefa', password='x', rol=User.ROL_ADMIN)

    def test_respuesta_por_automatizacion_y_plantilla(self):
        from apps.automatizaciones.models import AccionEtapa
        from apps.whatsapp.models import Conversacion, LineaWhatsApp, Mensaje, Plantilla
        etapa = self.embudo.etapa_inicial
        acc = AccionEtapa.objects.create(embudo=self.embudo, etapa=etapa, nombre='Bienvenida', tipo='whatsapp', texto='Hola')
        linea = LineaWhatsApp.objects.create(nombre='L', proveedor='demo')
        p = Plantilla.objects.first()
        ahora = timezone.now() - timedelta(hours=5)
        for i, responde in enumerate([True, False, False, True]):
            op = crm.ingresar_prospecto({'telefono': f'11900000{i:02d}', 'nombre': 'X'}, self.embudo, 'web').oportunidad
            conv = Conversacion.objects.create(linea=linea, telefono=op.contacto.telefono, contacto=op.contacto)
            Mensaje.objects.create(conversacion=conv, direccion='out', contenido='Hola', automatico=True, accion=acc,
                                   plantilla=p, status='read' if i < 3 else 'delivered', timestamp=ahora)
            if responde:
                Mensaje.objects.create(conversacion=conv, direccion='in', contenido='sí', timestamp=ahora + timedelta(hours=1))
        c = analisis.calidad_envios(timezone.now() - timedelta(days=1), timezone.now())
        fila = c['automatizaciones'][0]
        self.assertEqual((fila['nombre'], fila['n'], fila['respondidos'], fila['pct_respondidos'], fila['pct_leidos']),
                         ('Bienvenida', 4, 2, 50.0, 75.0))
        self.assertEqual(c['plantillas'][0]['respondidos'], 2)
        self.client.force_login(self.jefa)
        self.assertContains(self.client.get('/reportes/envios/'), 'Bienvenida')

    def test_email_apertura_y_clic(self):
        import re
        from django.core import mail
        from apps.automatizaciones.models import AccionEtapa, EmailEnviado
        from apps.automatizaciones.services import _correr
        op = crm.ingresar_prospecto({'telefono': '1190000100', 'nombre': 'Mail', 'email': 'a@b.com'}, self.embudo, 'web').oportunidad
        acc = AccionEtapa.objects.create(embudo=self.embudo, etapa=op.etapa, nombre='Mail info', tipo='email',
                                         email_asunto='Info', texto='Mirá https://supregsolutions.com/plan')
        self.assertEqual(_correr(acc, op)[0], 'ejecutada')
        html = mail.outbox[-1].alternatives[0][0]
        envio = EmailEnviado.objects.get()
        self.assertIn(f'/e/o/{envio.token}.gif', html)
        link = re.search(r'href="[^"]*(/e/c/[^"]+)"', html).group(1).replace('&amp;', '&')
        self.client.get(f'/e/o/{envio.token}.gif')
        r = self.client.get(link)
        self.assertEqual((r.status_code, r['Location']), (302, 'https://supregsolutions.com/plan'))
        envio.refresh_from_db()
        self.assertEqual((envio.aperturas, envio.clics, envio.abierto_at is not None), (1, 1, True))
        self.assertEqual(self.client.get(f'/e/c/{envio.token}/?u=https://malo.com').status_code, 404)
        c = analisis.calidad_envios(timezone.now() - timedelta(days=1), timezone.now() + timedelta(minutes=1))
        self.assertEqual((c['emails'][0]['abiertos'], c['emails'][0]['clics']), (1, 1))


class CallCenterTests(TestCase):
    def test_conectado_habla_y_acw(self):
        from core import presencia
        from apps.crm.models import Actividad, Contacto
        from apps.telefonia.models import Llamada
        from apps.users.models import SesionConexion
        ana = User.objects.create_user('ana', password='x', first_name='Ana')
        presencia.marcar(ana)
        sesion = SesionConexion.objects.get(usuario=ana)
        sesion.inicio = timezone.now() - timedelta(hours=2)
        sesion.save()
        presencia.desconectar(ana)
        c = Contacto.objects.create(nombre='C', telefono='1150001111')
        fin = timezone.now() - timedelta(minutes=30)
        Llamada.objects.create(direccion='OUT', estado='atendida', agente=ana, contacto=c, duracion_seg=300,
                               inicio_at=fin - timedelta(minutes=5), fin_at=fin)
        Llamada.objects.create(direccion='OUT', estado='no_atendida', agente=ana, contacto=c,
                               inicio_at=fin + timedelta(minutes=10), fin_at=fin + timedelta(minutes=11))
        Actividad.objects.create(contacto=c, tipo='nota', usuario=ana, texto='tipifico', created_at=fin + timedelta(seconds=90))
        f = analisis.call_center(timezone.now() - timedelta(days=1), timezone.now())[0]
        self.assertEqual((f['llamadas'], f['atendidas'], f['habla_min'], f['acw_prom'], f['conectado_h']),
                         (2, 1, 5, 90, 2.0))
        self.assertAlmostEqual(f['ocupacion'], 4.2, delta=0.1)
