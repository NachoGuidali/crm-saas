import io
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from django.core.management import call_command
from django.db import connection
from django.test import Client
from django.utils import timezone

from core.testing import TestCase
from apps.users.models import User
from apps.whatsapp.models import Conversacion, LineaWhatsApp, Mensaje


# ── Claude falso: cada llamada devuelve la siguiente respuesta del guion ────

def texto(t):
    return SimpleNamespace(type='text', text=t)


def tool(nombre, entrada, id_='t1'):
    return SimpleNamespace(type='tool_use', name=nombre, input=entrada, id=id_)


def respuesta(*bloques, stop=None):
    stop = stop or ('tool_use' if any(b.type == 'tool_use' for b in bloques) else 'end_turn')
    return SimpleNamespace(content=list(bloques), stop_reason=stop, _request_id='req_test',
                           usage=SimpleNamespace(input_tokens=1000, output_tokens=200, cache_read_input_tokens=5000,
                                                 cache_creation_input_tokens=0))


class ClaudeFalso:
    def __init__(self, *guion):
        self.guion, self.llamadas = list(guion), []
        self.messages = SimpleNamespace(create=self._create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.llamadas.append(json.loads(json.dumps(kw, default=lambda o: o.__dict__)))
        return self.guion.pop(0)


def _habilitar(si=True):
    connection.tenant.funciones_extra = ['agente_ia', 'firmas'] if si else []
    connection.tenant.save(update_fields=['funciones_extra'])


def payload_evolution(tel='5491155551234', wa_id='ABC1', texto_='Hola, quiero info de los planes', nombre='Ana Test'):
    return {'event': 'messages.upsert', 'instance': 'x', 'data': {
        'key': {'remoteJid': f'{tel}@s.whatsapp.net', 'fromMe': False, 'id': wa_id},
        'pushName': nombre, 'messageType': 'conversation', 'message': {'conversation': texto_},
        'messageTimestamp': int(timezone.now().timestamp())}}


class AgenteBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        from apps.crm.models import CampoPersonalizado, Embudo
        call_command('setup_inicial', '--sin-admin', verbosity=0)
        cls.embudo = Embudo.objects.get()
        cls.etapas = list(cls.embudo.etapas.filter(tipo='normal').order_by('orden'))
        cls.admin = User.objects.create_superuser('root', 'r@x.com', 'x', rol=User.ROL_ADMIN)
        cls.recibo = CampoPersonalizado.objects.create(nombre='Tiene recibo de sueldo', tipo='sino')
        cls.linea = LineaWhatsApp.objects.create(nombre='Agente 1', proveedor='demo', embudo=cls.embudo,
                                                 telefono='+54 9 11 4000-0001', agente_ia_activo=True,
                                                 estado='conectada')

    def setUp(self):
        super().setUp()
        _habilitar()
        from apps.agente.models import ConfigAgente
        self.cfg = ConfigAgente.get()
        self.cfg.activo = True
        self.cfg.empresa = 'RAS'
        self.cfg.criterios = 'Califica si tiene recibo de sueldo.'
        self.cfg.campos_base = ['dni']
        self.cfg.embudo = self.embudo
        self.cfg.etapa_derivado = self.etapas[-1]
        self.cfg.demora_segundos = 0
        self.cfg.precios_columnas = ['Plan', 'Zona', 'Precio']
        self.cfg.precios_filas = [['Plan 210', 'CABA', '45000'], ['Plan 310', 'Córdoba', '52000']]
        self.cfg.save()
        self.cfg.campos.set([self.recibo])
        self.web = Client()
        self.web.force_login(self.admin)
        mock.patch('apps.whatsapp.proveedores.demo.ProveedorDemo._quizas_responder').start()
        self.addCleanup(mock.patch.stopall)

    def entrante(self, claude, **kw):
        """Simula un WhatsApp entrante y deja que el agente responda con el guion dado."""
        from apps.whatsapp.proveedores import MensajeEntrante
        from apps.whatsapp.services import procesar_mensaje_entrante
        p = payload_evolution(**kw)['data']
        msg = MensajeEntrante(telefono=p['key']['remoteJid'].split('@')[0], wa_id=p['key']['id'], tipo='text',
                              contenido=p['message']['conversation'], nombre_perfil=p['pushName'], timestamp=None)
        with mock.patch('apps.agente.motor._cliente', return_value=claude), \
                mock.patch('django.conf.settings.ANTHROPIC_API_KEY', 'sk-test'), \
                self.captureOnCommitCallbacks(execute=True):
            procesar_mensaje_entrante(self.linea, msg)
        return Conversacion.objects.get(linea=self.linea)


class AtenderTests(AgenteBase):
    def test_responde_guarda_datos_y_cotiza_con_la_tabla(self):
        claude = ClaudeFalso(
            respuesta(texto('¡Hola Ana!'), tool('guardar_datos', {'datos': [
                {'campo': 'dni', 'valor': '30.123.456'}, {'campo': self.recibo.slug, 'valor': 'sí'}]}, 't1'),
                tool('consultar_precios', {'terminos': ['plan 210', 'caba']}, 't2')),
            respuesta(texto('Soy Clara, asistente virtual de RAS 😊\n---\nEl Plan 210 en CABA sale $45000 por mes.')),
        )
        conv = self.entrante(claude)
        salientes = list(conv.mensajes.filter(direccion='out').order_by('pk'))
        self.assertEqual([m.contenido for m in salientes],
                         ['¡Hola Ana!', 'Soy Clara, asistente virtual de RAS 😊', 'El Plan 210 en CABA sale $45000 por mes.'])
        self.assertTrue(all(m.por_ia for m in salientes))
        conv.contacto.refresh_from_db()
        self.assertEqual(conv.contacto.dni, '30123456')
        self.assertIs(conv.contacto.datos_extra[self.recibo.slug], True)
        # La herramienta de precios devolvió la fila de la tabla (sin importar acentos/mayúsculas)
        resultado = claude.llamadas[1]['messages'][-1]['content'][1]['content']
        self.assertIn('Plan 210 | CABA | 45000', resultado)
        # Request: instrucciones en caché, contexto del lead como mensaje de sistema después del último del lead
        req = claude.llamadas[0]
        self.assertEqual(req['model'], 'claude-opus-5-5')
        self.assertEqual(req['system'][0]['cache_control'], {'type': 'ephemeral'})
        self.assertIn('Califica si tiene recibo de sueldo', req['system'][0]['text'])
        self.assertEqual([m['role'] for m in req['messages']][-2:], ['user', 'system'])
        self.assertEqual(req['fallbacks'], 'default')
        self.assertTrue(all(t['strict'] for t in req['tools']))
        from apps.agente.models import TurnoAgente
        turno = TurnoAgente.objects.get()
        self.assertGreater(turno.costo, 0)
        self.assertEqual([h['nombre'] for h in turno.herramientas], ['guardar_datos', 'consultar_precios'])

    def test_deriva_al_crm_externo_y_apaga_el_agente(self):
        self.cfg.derivar_url = 'https://crm-cerrador.test/webhook'
        self.cfg.derivar_token = 'secreto'
        self.cfg.save()
        claude = ClaudeFalso(
            respuesta(tool('derivar_a_asesor', {'resumen': 'Ana, 35, con recibo, quiere Plan 210', 'motivo': 'Tiene recibo'})),
            respuesta(texto('¡Listo! Un asesor te contacta en breve.')),
        )
        with mock.patch('apps.agente.tasks.requests.post', return_value=mock.Mock(status_code=200)) as post:
            conv = self.entrante(claude)
        conv.refresh_from_db()
        self.assertFalse(conv.bot_activo)
        op = conv.contacto.oportunidades.get()
        self.assertEqual(op.etapa, self.etapas[-1])
        enviado = post.call_args.kwargs
        self.assertEqual(enviado['headers']['Authorization'], 'Bearer secreto')
        self.assertEqual(enviado['json']['resumen'], 'Ana, 35, con recibo, quiere Plan 210')
        self.assertEqual(enviado['json']['lead']['telefono'], conv.telefono)
        self.assertIn(f'/whatsapp/?conv={conv.pk}', enviado['json']['crm_origen']['url_chat'])
        self.assertTrue(op.actividades.filter(texto__startswith='Agente IA derivó').exists())
        self.assertTrue(op.actividades.filter(texto__startswith='Lead enviado al CRM del asesor').exists())
        # Si el lead vuelve a escribir, el agente ya no responde
        otro = ClaudeFalso()
        self.entrante(otro, wa_id='ABC2', texto_='¿Y entonces?')
        self.assertEqual(otro.llamadas, [])

    def test_si_escribe_una_persona_el_agente_se_calla(self):
        conv = self.entrante(ClaudeFalso(respuesta(texto('Hola!'))))
        self.web.post(f'/whatsapp/conversacion/{conv.pk}/enviar/', {'texto': 'Hola, soy Juan del equipo'})
        conv.refresh_from_db()
        self.assertFalse(conv.bot_activo)
        otro = ClaudeFalso()
        self.entrante(otro, wa_id='ABC2', texto_='Gracias Juan')
        self.assertEqual(otro.llamadas, [])

    def test_no_responde_apagado_sin_modulo_o_sin_presupuesto(self):
        self.cfg.activo = False
        self.cfg.save()
        c = ClaudeFalso()
        self.entrante(c)
        self.assertEqual(c.llamadas, [])
        self.cfg.activo = True
        self.cfg.presupuesto_diario = 0
        self.cfg.save()
        self.entrante(c, wa_id='X2')
        self.assertEqual(c.llamadas, [])
        self.cfg.presupuesto_diario = 20
        self.cfg.save()
        _habilitar(False)
        self.entrante(c, wa_id='X3')
        self.assertEqual(c.llamadas, [])

    def test_mensajes_seguidos_se_responden_una_sola_vez(self):
        from apps.agente import tasks
        conv = self.entrante(ClaudeFalso(respuesta(texto('Hola!'))))
        primero = conv.mensajes.filter(direccion='in').first()
        Mensaje.objects.create(conversacion=conv, direccion='in', contenido='otra cosa')
        # La tarea del primer mensaje ve que llegó otro y no responde (lo hace la siguiente)
        self.assertIn('llegó otro', tasks.atender.run(conv.pk, primero.pk))

    def test_rechazo_del_modelo_pasa_a_una_persona(self):
        conv = self.entrante(ClaudeFalso(respuesta(stop='refusal')))
        conv.refresh_from_db()
        self.assertFalse(conv.bot_activo)
        self.assertTrue(conv.bot_derivada)

    def test_seguimiento_programado_se_envia_si_no_contesta(self):
        from apps.agente import tasks
        from apps.agente.models import SeguimientoAgente
        conv = self.entrante(ClaudeFalso(
            respuesta(tool('programar_seguimiento', {'en_horas': 24, 'motivo': 'Preguntar si vio los planes'})),
            respuesta(texto('Te paso los planes 👇'))))
        seg = SeguimientoAgente.objects.get()
        SeguimientoAgente.objects.filter(pk=seg.pk).update(cuando=timezone.now() - timedelta(minutes=1))
        claude = ClaudeFalso(respuesta(texto('¿Pudiste ver los planes?')))
        with mock.patch('apps.agente.motor._cliente', return_value=claude), \
                mock.patch('django.conf.settings.ANTHROPIC_API_KEY', 'sk-test'):
            tasks.seguimientos()
        self.assertEqual(conv.mensajes.filter(direccion='out').last().contenido, '¿Pudiste ver los planes?')
        self.assertIn('Preguntar si vio los planes', claude.llamadas[0]['messages'][-1]['content'])
        seg.refresh_from_db()
        self.assertTrue(seg.hecho)


class ProspeccionYLineasTests(AgenteBase):
    def test_automatizacion_el_agente_escribe_primero_y_respeta_calentamiento(self):
        from apps.automatizaciones.models import AccionEtapa, EjecucionAccion
        from apps.automatizaciones.services import ejecutar
        from apps.crm import services as crm
        from apps.agente.models import TurnoAgente
        LineaWhatsApp.objects.filter(pk=self.linea.pk).update(calentamiento_inicio=timezone.localdate(), max_salientes_dia=1)
        acc = AccionEtapa.objects.create(embudo=self.embudo, etapa=self.etapas[0], nombre='IA prospecta', tipo='agente_ia',
                                         texto='Presentate y preguntá si sigue buscando cobertura', solo_en_horario=False)
        ops = [crm.ingresar_prospecto({'telefono': f'11500000{i}0', 'nombre': f'Lead {i}'}, self.embudo, 'importacion').oportunidad
               for i in range(2)]
        claude = ClaudeFalso(respuesta(texto('Hola Lead 0, soy Clara, asistente virtual de RAS.')))
        estados = []
        with mock.patch('apps.agente.motor._cliente', return_value=claude), \
                mock.patch('django.conf.settings.ANTHROPIC_API_KEY', 'sk-test'), \
                mock.patch('apps.automatizaciones.services._encolar'):
            for op in ops:
                ej = EjecucionAccion.objects.create(accion=acc, oportunidad=op, programada_para=timezone.now())
                with self.captureOnCommitCallbacks(execute=True):
                    estados.append(ejecutar(ej.pk))
        self.assertEqual(estados, ['ejecutada', 'programada'])  # tope 1 por día: el segundo pasa a mañana
        ej2 = EjecucionAccion.objects.get(oportunidad=ops[1])
        self.assertEqual(timezone.localtime(ej2.programada_para).date(), timezone.localdate() + timedelta(days=1))
        self.assertIn('reintenta mañana', ej2.detalle)
        self.assertIn('Presentate y preguntá', claude.llamadas[0]['messages'][-1]['content'])
        self.assertEqual(TurnoAgente.objects.get().modo, 'inicio')
        conv = Conversacion.objects.get(contacto=ops[0].contacto)
        self.assertTrue(conv.bot_activo)
        self.assertTrue(conv.mensajes.filter(direccion='out', por_ia=True).exists())

    def test_cupo_de_calentamiento(self):
        from apps.agente.lineas import cupo_dia
        hoy = timezone.localdate()
        l = LineaWhatsApp(calentamiento_inicio=hoy - timedelta(days=3), max_salientes_dia=0)
        self.assertEqual(cupo_dia(l, hoy), 45)
        l.max_salientes_dia = 30
        self.assertEqual(cupo_dia(l, hoy), 30)
        l.calentamiento_inicio = None
        self.assertEqual(cupo_dia(l, hoy), 30)
        l.max_salientes_dia = 0
        self.assertIsNone(cupo_dia(l, hoy))

    def test_link_rotativo_reparte_y_saltea_lineas_caidas(self):
        from apps.agente.models import EnlaceRotativo
        l2 = LineaWhatsApp.objects.create(nombre='Agente 2', proveedor='evolution', telefono='5491140000002',
                                          agente_ia_activo=True, estado='conectada')
        caida = LineaWhatsApp.objects.create(nombre='Agente 3', proveedor='evolution', telefono='5491140000003',
                                             agente_ia_activo=True, estado='desconectada')
        e = EnlaceRotativo.objects.create(nombre='Meta', mensaje='Hola, quiero info')
        anon = Client()
        destinos = [anon.get(f'/w/{e.slug}/')['Location'] for _ in range(4)]
        self.assertEqual(destinos[0], 'https://wa.me/5491140000001?text=Hola%2C%20quiero%20info')
        self.assertEqual(destinos[1].split('?')[0], 'https://wa.me/5491140000002')
        self.assertEqual(destinos[0], destinos[2])
        self.assertNotIn('5491140000003', ''.join(destinos))
        e.refresh_from_db()
        self.assertEqual(e.clics, 4)
        LineaWhatsApp.objects.filter(pk=l2.pk).update(estado='desconectada')
        LineaWhatsApp.objects.filter(pk=self.linea.pk).update(agente_ia_activo=False)  # la simulada siempre figura conectada
        self.assertEqual(anon.get(f'/w/{e.slug}/').status_code, 503)
        self.assertIsNotNone(caida)

    def test_si_se_cae_una_linea_entra_la_de_reserva(self):
        from apps.agente.services import linea_cambio_estado
        from apps.users.models import NotificacionInterna
        reserva = LineaWhatsApp.objects.create(nombre='Reserva', proveedor='evolution', reserva=True, estado='conectada')
        self.assertEqual(linea_cambio_estado(self.linea, 'conectada', 'desconectada'), reserva)
        reserva.refresh_from_db()
        self.assertTrue(reserva.agente_ia_activo and not reserva.reserva)
        self.assertTrue(NotificacionInterna.objects.filter(titulo__contains='Se desconectó').exists())


class ConocimientoTests(AgenteBase):
    def test_planilla_csv_xlsx_y_busqueda(self):
        from openpyxl import Workbook
        from apps.agente import conocimiento as k
        cols, filas = k.leer_planilla('p.csv', 'Plan;Zona;Precio\nPlan 210;Córdoba;52000\n\n'.encode())
        self.assertEqual((cols, filas), (['Plan', 'Zona', 'Precio'], [['Plan 210', 'Córdoba', '52000']]))
        wb = Workbook()
        wb.active.append(['Plan', 'Edad', 'Precio'])
        wb.active.append(['Plan 310', 40, 61000])
        buf = io.BytesIO()
        wb.save(buf)
        self.assertEqual(k.leer_planilla('p.xlsx', buf.getvalue())[1], [['Plan 310', '40', '61000']])
        self.assertEqual(k.buscar_precios(self.cfg, ['cordoba']), [['Plan 310', 'Córdoba', '52000']])
        self.assertEqual(k.url_csv_google('https://docs.google.com/spreadsheets/d/AbC-1_x/edit#gid=77'),
                         'https://docs.google.com/spreadsheets/d/AbC-1_x/export?format=csv&gid=77')
        with self.assertRaises(k.ErrorConocimiento):
            k.url_csv_google('https://example.com')

    def test_documento_desde_archivo_y_pantallas(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from apps.agente.models import DocumentoAgente
        r = self.web.post('/agente/conocimiento/nuevo/', {
            'titulo': 'Planes', 'orden': 0, 'activo': 'on', 'texto': '',
            'archivo': SimpleUploadedFile('planes.txt', 'El Plan 210 cubre internación.'.encode())})
        self.assertEqual(r.status_code, 302, r.content[:500])
        self.assertIn('Plan 210 cubre internación', DocumentoAgente.objects.get().texto)
        for url in ('/agente/', '/agente/conocimiento/', '/agente/lineas/', '/agente/simulador/', '/agente/registro/'):
            self.assertEqual(self.web.get(url).status_code, 200, url)
        _habilitar(False)
        self.assertEqual(self.web.get('/agente/').status_code, 404)

    def test_simulador_no_toca_leads_ni_whatsapp(self):
        claude = ClaudeFalso(
            respuesta(tool('guardar_datos', {'datos': [{'campo': 'nombre', 'valor': 'Pepe'}]}),
                      tool('derivar_a_asesor', {'resumen': 'ok', 'motivo': 'califica'}, 't2')),
            respuesta(texto('Genial Pepe, te contacta un asesor.')))
        with mock.patch('apps.agente.motor._cliente', return_value=claude):
            r = self.web.post('/agente/simulador/', json.dumps({'historial': [{'de': 'lead', 'texto': 'Soy Pepe'}]}),
                              content_type='application/json')
        d = r.json()
        self.assertEqual(d['mensajes'], ['Genial Pepe, te contacta un asesor.'])
        self.assertEqual((d['datos'], d['resultado']), ({'nombre': 'Pepe'}, 'derivado'))
        self.assertFalse(Mensaje.objects.exists())
        from apps.crm.models import Contacto
        self.assertFalse(Contacto.objects.exists())


class TranscripcionTests(AgenteBase):
    def test_transcribe_con_gpt_transcribe(self):
        import os
        import tempfile
        from apps.agente import transcripcion
        with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as f:
            f.write(b'ID3fake')
        self.addCleanup(os.unlink, f.name)
        with mock.patch('django.conf.settings.OPENAI_API_KEY', 'sk-openai'), \
                mock.patch('apps.agente.transcripcion.requests.post',
                           return_value=mock.Mock(status_code=200, json=lambda: {'text': ' Hola, quiero el plan 210 '})) as post:
            self.assertEqual(transcripcion.transcribir(f.name), 'Hola, quiero el plan 210')
        self.assertEqual(post.call_args.kwargs['data'], {'model': 'gpt-transcribe', 'languages[]': 'es'})
        self.assertEqual(post.call_args.kwargs['headers']['Authorization'], 'Bearer sk-openai')
