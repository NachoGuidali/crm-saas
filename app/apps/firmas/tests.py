import base64
import json
from io import BytesIO

from django.core import mail
from django.core.files.storage import default_storage
from django.core.management import call_command
from django.db import connection

from core.testing import TestCase
from apps.users.models import User


def _habilitar(si=True):
    connection.tenant.funciones_extra = ['firmas'] if si else []
    connection.tenant.save(update_fields=['funciones_extra'])


def _firma_png():
    from PIL import Image, ImageDraw
    img = Image.new('RGBA', (300, 120), (0, 0, 0, 0))
    ImageDraw.Draw(img).line([(10, 100), (120, 20), (280, 90)], fill=(0, 0, 0, 255), width=4)
    buf = BytesIO()
    img.save(buf, 'PNG')
    return 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode()


def _png_vacio():
    from PIL import Image
    buf = BytesIO()
    Image.new('RGBA', (300, 120), (0, 0, 0, 0)).save(buf, 'PNG')
    return 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode()


CAMPOS = [
    {'id': 'c1', 'tipo': 'texto', 'etiqueta': 'Nombre y apellido', 'requerido': True, 'prellenar': 'nombre'},
    {'id': 'c2', 'tipo': 'numero', 'etiqueta': 'Peso (kg)', 'requerido': True},
    {'id': 'c3', 'tipo': 'sino', 'etiqueta': '¿Tuvo cirugías?', 'requerido': True},
    {'id': 'c4', 'tipo': 'acepto', 'etiqueta': 'Declaro que es verdad', 'requerido': True},
    {'id': 'c5', 'tipo': 'firma', 'etiqueta': 'Firma del titular', 'requerido': True},
]


class FirmasTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from apps.crm.models import Embudo
        call_command('setup_inicial', '--sin-admin', verbosity=0)
        cls.embudo = Embudo.objects.get()
        cls.etapas = list(cls.embudo.etapas.filter(tipo='normal').order_by('orden'))
        cls.ana = User.objects.create_user('ana', password='x', first_name='Ana', email='ana@x.com')
        cls.embudo.agentes.set([cls.ana])
        cls.admin = User.objects.create_superuser('root', 'r@x.com', 'x', rol=User.ROL_ADMIN)

    def setUp(self):
        super().setUp()
        _habilitar()
        from apps.crm import services as crm
        from .models import FormularioFirma
        self.op = crm.ingresar_prospecto({'telefono': '1180000011', 'nombre': 'María Gómez', 'email': 'maria@x.com',
                                          'dni': '30123456'}, self.embudo, 'web').oportunidad
        self.form = FormularioFirma.objects.create(
            nombre='DDJJ salud', titulo='Declaración jurada de {nombre}',
            documento='Yo, {nombre}, DNI {dni}, declaro bajo juramento que los datos son ciertos.', campos=CAMPOS)
        self.client.force_login(self.admin)

    def _datos_ok(self, **extra):
        return {'f_c1': 'María Gómez', 'f_c2': '68,5', 'f_c3': 'false', 'f_c4': '1', 'f_c5': _firma_png(), **extra}

    def _solicitud(self):
        from . import services
        return services.crear_solicitud(self.form, self.op, usuario=self.admin)

    def test_modulo_apagado_no_muestra_nada(self):
        sol = self._solicitud()
        _habilitar(False)
        self.assertEqual(self.client.get('/firmas/').status_code, 404)
        self.assertEqual(self.client.get(f'/f/{sol.token}/').status_code, 404)
        self.assertNotContains(self.client.get(self.op.get_absolute_url()), 'id="firmas"')
        from .services import ErrorFirma, crear_solicitud
        with self.assertRaises(ErrorFirma):
            crear_solicitud(self.form, self.op)

    def test_editor_exige_firma_y_normaliza_campos(self):
        base = {'nombre': 'Consentimiento', 'titulo': 'Consentimiento', 'color_fondo': '#ffffff',
                'color_principal': '#000000', 'texto_final': 'Gracias', 'vence_dias': 3, 'asunto_email': 'x',
                'texto_email': '{link}', 'texto_whatsapp': '{link}', 'activo': 'on'}
        r = self.client.post('/firmas/nuevo/', {**base, 'campos_json': json.dumps([{'tipo': 'texto', 'etiqueta': 'Edad'}])})
        self.assertContains(r, 'al menos un campo de tipo')
        r = self.client.post('/firmas/nuevo/', {**base, 'campos_json': json.dumps([
            {'tipo': 'lista', 'etiqueta': 'Plan', 'opciones': 'A, B'}, {'tipo': 'firma', 'etiqueta': ''},
            {'tipo': 'raro', 'etiqueta': 'x'}])})
        self.assertEqual(r.status_code, 302)
        from .models import FormularioFirma
        campos = FormularioFirma.objects.get(nombre='Consentimiento').campos
        self.assertEqual([c['tipo'] for c in campos], ['lista', 'firma'])
        self.assertEqual(campos[0]['opciones'], ['A', 'B'])
        self.assertTrue(campos[1]['requerido'])

    def test_circuito_completo_firma_pdf_copia_y_automatizacion(self):
        from apps.automatizaciones.models import AccionEtapa
        from apps.crm.models import Actividad
        from .models import SolicitudFirma
        etapa_actual = self.op.etapa
        AccionEtapa.objects.create(embudo=self.embudo, etapa=etapa_actual, nombre='Firmó → avanzar',
                                   disparador='firma', formulario_firma=self.form, tipo='etapa',
                                   mover_a=self.etapas[2], solo_en_horario=False)
        sol = self._solicitud()
        self.assertEqual(sol.snapshot['titulo'], 'Declaración jurada de María Gómez')
        self.assertIn('DNI 30123456', sol.snapshot['documento'])
        # El lead abre el link: precargado con su nombre
        r = self.client.get(f'/f/{sol.token}/')
        self.assertContains(r, 'value="María Gómez"')
        sol.refresh_from_db()
        self.assertEqual(sol.estado, 'abierta')
        # Sin firma (o firma en blanco) no pasa
        r = self.client.post(f'/f/{sol.token}/', self._datos_ok(f_c5=_png_vacio()))
        self.assertEqual(r.status_code, 400)
        self.assertContains(r, 'Falta la firma', status_code=400)
        r = self.client.post(f'/f/{sol.token}/', self._datos_ok(f_c2='mucho', f_c4=''))
        self.assertContains(r, 'Ingresá un número', status_code=400)
        self.assertContains(r, 'Tenés que aceptar', status_code=400)
        # Firma válida
        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            r = self.client.post(f'/f/{sol.token}/', self._datos_ok())
        self.assertEqual(r.status_code, 302)
        sol.refresh_from_db()
        self.assertEqual(sol.estado, SolicitudFirma.ESTADO_FIRMADA)
        self.assertEqual(sol.respuestas['c2'], '68,5')
        self.assertIs(sol.respuestas['c3'], False)
        self.assertEqual(len(sol.huella), 64)
        with default_storage.open(sol.pdf, 'rb') as f:
            self.assertTrue(f.read(5).startswith(b'%PDF'))
        # Copia al lead con el PDF adjunto
        copia = [m for m in mail.outbox if 'copia firmada' in m.subject]
        self.assertEqual(len(copia), 1)
        self.assertEqual(copia[0].to, ['maria@x.com'])
        self.assertEqual(copia[0].attachments[0][2], 'application/pdf')
        # Ficha, aviso y automatización "cuando se recibe firmado" → movió de etapa
        self.assertTrue(Actividad.objects.filter(oportunidad=self.op, texto__contains='Firmó').exists())
        self.op.refresh_from_db()
        self.assertEqual(self.op.etapa, self.etapas[2])
        # El lead puede descargar el PDF; no puede volver a firmar
        r = self.client.get(f'/f/{sol.token}/')
        self.assertContains(r, 'Descargar PDF firmado')
        self.assertEqual(self.client.get(f'/f/{sol.token}/pdf/')['Content-Type'], 'application/pdf')
        self.assertEqual(self.client.post(f'/f/{sol.token}/', self._datos_ok()).status_code, 409)
        # Desde la ficha se ve y se descarga
        self.assertContains(self.client.get(self.op.get_absolute_url()), f'/firmas/solicitud/{sol.pk}/pdf/')
        self.assertEqual(self.client.get(f'/firmas/solicitud/{sol.pk}/pdf/').status_code, 200)

    def test_vencida_y_anulada(self):
        from datetime import timedelta
        from django.utils import timezone
        from .models import SolicitudFirma
        sol = self._solicitud()
        SolicitudFirma.objects.filter(pk=sol.pk).update(vence_at=timezone.now() - timedelta(minutes=1))
        self.assertContains(self.client.get(f'/f/{sol.token}/'), 'venció')
        self.assertEqual(self.client.post(f'/f/{sol.token}/', self._datos_ok()).status_code, 409)
        otra = self._solicitud()
        self.client.post(f'/firmas/solicitud/{otra.pk}/anular/')
        self.assertContains(self.client.get(f'/f/{otra.token}/'), 'anulado')

    def test_enviar_desde_la_ficha_y_por_automatizacion(self):
        from apps.automatizaciones.models import AccionEtapa, EjecucionAccion
        from apps.automatizaciones.services import ejecutar
        from django.utils import timezone
        mail.outbox.clear()
        r = self.client.post(f'/firmas/enviar/{self.op.pk}/', {'formulario': self.form.pk, 'canal': ['email']})
        self.assertEqual(r.status_code, 302)
        sol = self.op.firmas.get()
        self.assertTrue(sol.enviada_email)
        self.assertIn(f'/f/{sol.token}/', mail.outbox[-1].body)
        # Acción de automatización "Enviar un formulario para firmar"
        acc = AccionEtapa.objects.create(embudo=self.embudo, etapa=self.op.etapa, nombre='Mandar DDJJ', tipo='firma',
                                         formulario_firma=self.form, canal_firma='email', solo_en_horario=False)
        ej = EjecucionAccion.objects.create(accion=acc, oportunidad=self.op, programada_para=timezone.now())
        self.assertEqual(ejecutar(ej.pk), 'ejecutada')
        self.assertEqual(self.op.firmas.filter(origen='automatizacion').count(), 1)

    def test_api_para_el_bot(self):
        from apps.integraciones.models import ApiKey
        _, clave = ApiKey.generar(nombre='bot')
        r = self.client.post('/api/v1/firmas/', json.dumps({'telefono': '11 8000-0011', 'formulario': 'ddjj salud'}),
                             content_type='application/json', HTTP_X_API_KEY=clave)
        self.assertEqual(r.status_code, 201, r.content)
        data = r.json()
        self.assertIn('/f/', data['url'])
        self.assertEqual(data['estado'], 'enviada')
        r = self.client.get('/api/v1/firmas/', {'id': data['id']}, HTTP_X_API_KEY=clave)
        self.assertEqual(r.json()['oportunidad_id'], self.op.pk)
        r = self.client.post('/api/v1/firmas/', json.dumps({'telefono': '1199999999', 'formulario': 'ddjj salud'}),
                             content_type='application/json', HTTP_X_API_KEY=clave)
        self.assertEqual(r.status_code, 404)
