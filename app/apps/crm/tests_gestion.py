"""
Pedidos de la reunión con Roisa: resultado de gestión obligatorio, reproceso (reasignar), reingresos prioritarios,
vencimiento de fichas, socios, condiciones por campaña/etiqueta, tareas masivas y campos de tipo archivo.
"""
import json
import shutil
import tempfile
from datetime import timedelta
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from core.testing import TestCase
from apps.automatizaciones.models import AccionEtapa, EjecucionAccion
from apps.automatizaciones.services import revisar_sin_actividad
from apps.telefonia import services as tel
from apps.telefonia.models import AgenteDiscador, CampaniaDiscado, ConfigAnura, InternoAnura, Llamada
from apps.users.models import User

from . import services as crm
from .models import (Actividad, CampoPersonalizado, Embudo, Etapa, Etiqueta, Oportunidad, ResultadoGestion, Tarea,
                     Tipificacion)


class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command('setup_inicial', '--sin-admin', verbosity=0)
        AccionEtapa.objects.update(activa=False)  # los mensajes del circuito no interfieren
        cls.embudo = Embudo.objects.get()
        cls.ana = User.objects.create_user('ana', password='x', first_name='Ana')
        cls.beto = User.objects.create_user('beto', password='x', first_name='Beto')
        cls.embudo.agentes.set([cls.ana, cls.beto])
        cls.etapa = lambda nombre: Etapa.objects.get(embudo=cls.embudo, nombre=nombre)

    def ingresar(self, tel='1155556666', agente=None, embudo=None):
        with self.captureOnCommitCallbacks(execute=True):
            return crm.ingresar_prospecto({'telefono': tel, 'nombre': 'María López'}, embudo or self.embudo,
                                          'importacion', agente=agente)

    def llamada(self, op, agente, estado=Llamada.ESTADO_ATENDIDA, **kw):
        return Llamada.objects.create(direccion=Llamada.DIR_SALIENTE, estado=estado, numero=op.contacto.telefono,
                                      agente=agente, contacto=op.contacto, oportunidad=op, duracion_seg=60,
                                      procesada=True, call_id=kw.pop('call_id', None), **kw)


class ResultadoGestionTests(Base):
    def test_atendida_queda_sin_calificar_y_se_califica_con_rellamado(self):
        op = self.ingresar(agente=self.ana).oportunidad
        ll = self.llamada(op, self.ana, call_id='CDR-777')
        self.llamada(op, self.ana, estado=Llamada.ESTADO_NO_ATENDIDA)  # las no atendidas no se califican
        self.assertEqual(tel.pendiente_de_calificar(self.ana), ll)
        self.assertEqual(ll.id_grabacion, 'CDR-777')
        rellamar = ResultadoGestion.objects.get(nombre='Pidió que lo llamen más tarde')
        with self.assertRaises(tel.ErrorTelefonia):
            tel.calificar_llamada(ll, rellamar, self.ana)
        cuando = timezone.now() + timedelta(days=1)
        self.assertTrue(tel.calificar_llamada(ll, rellamar, self.ana, nota='a la tarde', volver_a_llamar=cuando))
        self.assertFalse(tel.calificar_llamada(ll, rellamar, self.ana))  # doble clic
        self.assertIsNone(tel.pendiente_de_calificar(self.ana))
        self.assertTrue(Tarea.objects.filter(oportunidad=op, titulo__startswith='Volver a llamar', asignado_a=self.ana,
                                             estado=Tarea.ESTADO_PENDIENTE).exists())
        self.assertIn('Resultado: Pidió que lo llamen más tarde — a la tarde',
                      Actividad.objects.get(llamada=ll).texto)

    def test_resultado_mueve_etapa(self):
        op = self.ingresar(agente=self.ana).oportunidad
        r = ResultadoGestion.objects.create(nombre='Interesado', mover_a=self.etapa('Contacto efectivo'))
        tel.calificar_llamada(self.llamada(op, self.ana), r, self.ana)
        op.refresh_from_db()
        self.assertEqual(op.etapa.nombre, 'Contacto efectivo')

    def test_embudo_que_no_exige_resultado(self):
        Embudo.objects.filter(pk=self.embudo.pk).update(exigir_resultado=False)
        op = self.ingresar(agente=self.ana).oportunidad
        self.llamada(op, self.ana)
        self.assertIsNone(tel.pendiente_de_calificar(self.ana))

    def test_discador_no_pasa_la_proxima_sin_calificar(self):
        campania = CampaniaDiscado.objects.create(nombre='C', embudo=self.embudo, segundos_entre_llamadas=0)
        AgenteDiscador.objects.create(agente=self.ana, campania=campania,
                                      libre_desde=timezone.now() - timedelta(minutes=5))
        op = self.ingresar(agente=self.ana).oportunidad
        ll = self.llamada(op, self.ana)
        self.assertEqual(tel.agentes_libres(campania), [])
        tel.calificar_llamada(ll, ResultadoGestion.objects.first(), self.ana)
        AgenteDiscador.objects.filter(agente=self.ana).update(libre_desde=timezone.now() - timedelta(minutes=5))
        self.assertEqual([s.agente for s in tel.agentes_libres(campania)], [self.ana])

    def test_pulso_pide_resultado_y_endpoint_califica(self):
        ConfigAnura.objects.filter(pk=1).update(activo=True, modo_demo=True)
        InternoAnura.objects.create(usuario=self.ana, interno='201')
        op = self.ingresar(agente=self.ana).oportunidad
        ll = self.llamada(op, self.ana)
        self.client.force_login(self.ana)
        d = self.client.get('/pulso/').json()
        self.assertEqual(d['calificar']['id'], ll.pk)
        self.assertTrue(d['calificar']['resultados'])
        r = self.client.post(f'/api/telephony/calificar/{ll.pk}', json.dumps({'resultado': 0}),
                             content_type='application/json')
        self.assertEqual(r.status_code, 400)
        res = ResultadoGestion.objects.get(nombre='No le interesa')
        r = self.client.post(f'/api/telephony/calificar/{ll.pk}', json.dumps({'resultado': res.pk, 'nota': 'x'}),
                             content_type='application/json')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertIsNone(self.client.get('/pulso/').json()['calificar'])
        # Otra agente no puede calificar llamadas ajenas
        self.client.force_login(self.beto)
        self.assertEqual(self.client.post(f'/api/telephony/calificar/{ll.pk}', {}).status_code, 404)

    def test_listados_y_reportes_de_sin_calificar(self):
        op = self.ingresar(agente=self.ana).oportunidad
        self.llamada(op, self.ana, call_id='CDR-1')
        admin = User.objects.create_superuser('jefa', password='x', email='j@x.com')
        self.client.force_login(admin)
        r = self.client.get('/telefonia/llamadas/?resultado=sin')
        self.assertContains(r, 'Sin calificar')
        self.assertContains(r, 'CDR-1')
        self.assertContains(self.client.get('/telefonia/llamadas/?q=CDR-1'), 'CDR-1')
        csv = self.client.get('/telefonia/llamadas/?csv=1').content.decode()
        self.assertIn('ID grabación', csv)
        self.assertIn('CDR-1', csv)
        self.assertEqual(crm.filtrar_oportunidades(Oportunidad.objects.all(), {'sin_calificar': '1'}, admin).get(), op)
        from apps.reportes import analisis
        ini, fin = timezone.now() - timedelta(days=1), timezone.now() + timedelta(days=1)
        self.assertEqual(analisis.resultados_gestion(ini, fin)['sin'], 1)
        fila = next(f for f in analisis.actividad_vendedoras(ini, fin) if f['u'] == self.ana)
        self.assertEqual(fila['sin_calificar'], 1)
        self.assertContains(self.client.get('/supervision/'), 'Sin calificar')


@mock.patch('apps.whatsapp.proveedores.demo.ProveedorDemo._quizas_responder')
class ReprocesoTests(Base):
    def accion(self, **kw):
        datos = {'embudo': self.embudo, 'etapa': self.etapa('En gestión'), 'nombre': 'Reproceso',
                 'disparador': AccionEtapa.DISP_SIN_RESPUESTA, 'tipo': AccionEtapa.TIPO_REASIGNAR,
                 'demora_minutos': 2 * 1440, 'solo_en_horario': False, 'volver_a_etapa': self.etapa('Prospecto (Nuevo)')}
        datos.update(kw)
        return AccionEtapa.objects.create(**datos)

    def test_sin_respuesta_reasigna_a_otra_y_vuelve_a_nuevo(self, _):
        op = self.ingresar(agente=self.ana).oportunidad
        with self.captureOnCommitCallbacks(execute=True):
            crm.mover_etapa(op, self.etapa('Propuesta enviada'), self.ana)
        # Base actual: entró a la etapa hace 3 días, antes de crear la automatización
        accion = self.accion()
        accion.otras_etapas.add(self.etapa('Propuesta enviada'))
        Oportunidad.objects.filter(pk=op.pk).update(etapa_desde=timezone.now() - timedelta(days=3))
        with self.captureOnCommitCallbacks(execute=True):
            revisar_sin_actividad()
        ej = EjecucionAccion.objects.get(accion=accion)
        self.assertEqual(ej.estado, 'ejecutada', ej.detalle)
        op.refresh_from_db()
        self.assertEqual((op.agente, op.etapa.nombre), (self.beto, 'Prospecto (Nuevo)'))
        self.assertTrue(Tarea.objects.filter(oportunidad=op, asignado_a=self.beto, estado='pendiente').exists())

    def test_si_respondio_no_reprocesa(self, _):
        op = self.ingresar(agente=self.ana).oportunidad
        accion = self.accion(etapa=self.etapa('Prospecto (Nuevo)'))
        Oportunidad.objects.filter(pk=op.pk).update(etapa_desde=timezone.now() - timedelta(days=3))
        op.historial.update(created_at=timezone.now() - timedelta(days=3))
        Llamada.objects.create(direccion=Llamada.DIR_ENTRANTE, estado=Llamada.ESTADO_ATENDIDA, numero='x',
                               contacto=op.contacto, inicio_at=timezone.now() - timedelta(days=1))
        with self.captureOnCommitCallbacks(execute=True):
            revisar_sin_actividad()
        self.assertEqual(EjecucionAccion.objects.get(accion=accion).estado, 'omitida')
        op.refresh_from_db()
        self.assertEqual(op.agente, self.ana)

    def test_sin_otro_vendedor_queda_igual(self, _):
        self.embudo.agentes.set([self.ana])
        accion = self.accion(disparador=AccionEtapa.DISP_ENTRADA, etapa=self.etapa('Prospecto (Nuevo)'),
                             demora_minutos=0, volver_a_etapa=None)
        op = self.ingresar(agente=self.ana).oportunidad
        self.assertEqual(EjecucionAccion.objects.get(accion=accion).estado, 'omitida')
        op.refresh_from_db()
        self.assertEqual(op.agente, self.ana)

    def test_todas_las_etapas(self, _):
        accion = self.accion(todas_las_etapas=True, etapa=self.etapa('Prospecto (Nuevo)'))
        self.assertIn(self.etapa('Negociación').pk, accion.ids_etapas())
        self.assertNotIn(self.etapa('Venta (Ganado)').pk, accion.ids_etapas())


@mock.patch('apps.whatsapp.proveedores.demo.ProveedorDemo._quizas_responder')
class ReingresoYCondicionesTests(Base):
    def test_reingreso_marca_prioridad_y_etiqueta(self, _):
        caliente = Etiqueta.objects.create(nombre='Caliente')
        base = {'embudo': self.embudo, 'etapa': self.etapa('Prospecto (Nuevo)'), 'todas_las_etapas': True,
                'disparador': AccionEtapa.DISP_REINGRESO, 'solo_en_horario': False}
        AccionEtapa.objects.create(nombre='Prioridad', tipo=AccionEtapa.TIPO_PRIORIDAD, tarea_titulo='Reingresó', **base)
        AccionEtapa.objects.create(nombre='Etiqueta', tipo=AccionEtapa.TIPO_ETIQUETA, etiqueta=caliente, **base)
        op = self.ingresar(agente=self.ana).oportunidad
        with self.captureOnCommitCallbacks(execute=True):
            crm.mover_etapa(op, self.etapa('En gestión'), self.ana)
        self.assertFalse(Oportunidad.objects.get(pk=op.pk).prioritaria)
        self.ingresar()  # vuelve a llenar el formulario
        op.refresh_from_db()
        self.assertTrue(op.prioritaria)
        self.assertEqual(op.prioridad_motivo, 'Reingresó')
        self.assertIn(caliente, op.contacto.etiquetas.all())
        self.ingresar()  # ráfaga: no se repite
        self.assertEqual(EjecucionAccion.objects.filter(accion__disparador='reingreso').count(), 2)
        # Mi día: primero las prioritarias; y se limpia cuando la vendedora la gestiona
        self.assertEqual(crm.siguiente_prospecto(self.ana), op)
        tel.calificar_llamada(self.llamada(op, self.ana), ResultadoGestion.objects.first(), self.ana)
        op.refresh_from_db()
        self.assertFalse(op.prioritaria)

    def test_condiciones_por_pauta_y_etiqueta(self, _):
        from apps.pautas.models import Pauta
        socio = Etiqueta.objects.create(nombre='Socio')
        pauta = Pauta.objects.create(nombre='Día de la madre')
        accion = AccionEtapa.objects.create(embudo=self.embudo, etapa=self.etapa('Prospecto (Nuevo)'), nombre='Tarea',
                                            tipo=AccionEtapa.TIPO_TAREA, solo_en_horario=False)
        accion.solo_pautas.add(pauta)
        op = self.ingresar(agente=self.ana).oportunidad
        self.assertEqual(EjecucionAccion.objects.get(accion=accion).estado, 'omitida')
        accion.solo_pautas.clear()
        accion.excluir_etiquetas.add(socio)
        op2 = self.ingresar('1144443333', agente=self.ana).oportunidad
        self.assertEqual(EjecucionAccion.objects.get(accion=accion, oportunidad=op2).estado, 'ejecutada')
        op2.contacto.etiquetas.add(socio)
        self.assertEqual(accion.condiciones_ok(op2), (False, 'El contacto tiene una etiqueta excluida.'))
        self.assertTrue(accion.condiciones_ok(op)[0])

    def test_formulario_acepta_reingreso_y_condiciones(self, _):
        from apps.automatizaciones.views import AccionForm
        socio = Etiqueta.objects.create(nombre='Socio')
        f = AccionForm({'nombre': 'R', 'disparador': 'reingreso', 'todas_las_etapas': 'on', 'tipo': 'prioridad',
                        'demora_valor': 0, 'demora_unidad': 'min', 'activa': 'on', 'excluir_etiquetas': [socio.pk],
                        'modo_embudo': 'crear', 'volver_a': 'misma', 'asignar_destino': 'mismo',
                        'tarea_vence_horas': 24}, embudo=self.embudo)
        self.assertTrue(f.is_valid(), f.errors)
        a = f.save()
        self.assertEqual((a.etapa, a.todas_las_etapas), (self.embudo.etapa_inicial, True))
        self.assertEqual(list(a.excluir_etiquetas.all()), [socio])


class VencimientoYSociosTests(Base):
    def test_ficha_vencida_vuelve_al_reparto_salvo_desde_preventa(self):
        Embudo.objects.filter(pk=self.embudo.pk).update(vence_dias=10, vence_hasta_etapa=self.etapa('Negociación'))
        vieja = self.ingresar('1100000001', agente=self.ana).oportunidad
        protegida = self.ingresar('1100000002', agente=self.ana).oportunidad
        nueva = self.ingresar('1100000003', agente=self.ana).oportunidad
        crm.mover_etapa(protegida, self.etapa('Negociación'), self.ana)
        hace = timezone.now() - timedelta(days=11)
        Oportunidad.objects.filter(pk__in=[vieja.pk, protegida.pk]).update(asignada_at=hace)
        self.assertEqual(crm.revisar_vencimientos(), 1)
        for o in (vieja, protegida, nueva):
            o.refresh_from_db()
        self.assertEqual((vieja.agente, protegida.agente, nueva.agente), (self.beto, self.ana, self.ana))
        self.assertTrue(Actividad.objects.filter(oportunidad=vieja, datos__vencimiento=True).exists())
        self.assertEqual(crm.revisar_vencimientos(), 0)  # el nuevo vendedor arranca con el plazo completo

    def test_vencida_liberar(self):
        Embudo.objects.filter(pk=self.embudo.pk).update(vence_dias=5, vence_accion=Embudo.VENCE_LIBERAR)
        op = self.ingresar(agente=self.ana).oportunidad
        Oportunidad.objects.filter(pk=op.pk).update(asignada_at=timezone.now() - timedelta(days=6))
        crm.revisar_vencimientos()
        op.refresh_from_db()
        self.assertIsNone(op.agente)
        self.assertIsNotNone(op.vencida_at)

    def test_cliente_no_entra_como_lead_en_otro_embudo_y_va_a_postventa(self):
        postventa = User.objects.create_user('pv', password='x', first_name='Postventa')
        socio = Etiqueta.objects.create(nombre='Socio')
        Embudo.objects.filter(pk=self.embudo.pk).update(socios_a=Embudo.SOCIOS_USUARIO, socios_usuario=postventa,
                                                         etiqueta_venta=socio)
        op = self.ingresar(agente=self.ana).oportunidad
        tip = Tipificacion.objects.filter(resultado='venta').first()
        crm.mover_etapa(op, self.etapa('Venta (Ganado)'), self.ana, tipificacion=tip)
        self.assertIn(socio, op.contacto.etiquetas.all())
        otro = Embudo.objects.create(nombre='Otro')
        Etapa.objects.create(embudo=otro, nombre='Nuevo', orden=1)
        otro.agentes.set([self.beto])
        res = self.ingresar(embudo=otro)
        self.assertEqual((res.oportunidad_nueva, res.motivo), (False, 'ya_cliente'))
        otro.clientes_de_otros = True
        otro.save()
        self.assertTrue(self.ingresar(embudo=otro).oportunidad_nueva)
        # Su WhatsApp va a postventa, no a la bandeja sin asignar de ventas
        from apps.whatsapp.models import Conversacion, LineaWhatsApp
        from apps.whatsapp.proveedores.base import MensajeEntrante
        from apps.whatsapp.services import procesar_mensaje_entrante
        Oportunidad.objects.filter(embudo=otro).delete()
        linea = LineaWhatsApp.objects.create(nombre='L', proveedor='demo')
        procesar_mensaje_entrante(linea, MensajeEntrante(telefono=op.contacto.telefono, contenido='Hola, soy socio',
                                                         wa_id='w1'))
        self.assertEqual(Conversacion.objects.get().agente, postventa)


class MasivasYArchivosTests(Base):
    def test_tarea_y_prioridad_masivas(self):
        from . import masivas
        ops = [self.ingresar(f'11000000{i:02d}', agente=self.ana).oportunidad for i in range(3)]
        r = masivas.ejecutar(self.ana, 'tarea', [o.pk for o in ops],
                             {'titulo': 'Llamar por campaña X', 'tipo_tarea': 'llamada', 'vence': ''})
        self.assertEqual(r['hechos'], 3)
        self.assertEqual(Tarea.objects.filter(titulo='Llamar por campaña X', asignado_a=self.ana).count(), 3)
        with self.assertRaises(crm.ErrorNegocio):
            masivas.validar(self.ana, 'tarea', {'titulo': ''})
        masivas.ejecutar(self.ana, 'prioridad', [ops[0].pk], {'motivo': 'Campaña X'})
        self.assertTrue(Oportunidad.objects.get(pk=ops[0].pk).prioritaria)

    def test_campo_archivo_desde_ficha_y_desde_whatsapp(self):
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media, True)
        with override_settings(MEDIA_ROOT=media):
            campo = CampoPersonalizado.objects.create(nombre='Recibo de sueldo', tipo=CampoPersonalizado.TIPO_ARCHIVO)
            op = self.ingresar(agente=self.ana).oportunidad
            self.client.force_login(self.ana)
            r = self.client.post(f'/contactos/{op.contacto_id}/archivos/', {
                'campo': campo.slug, 'oportunidad': op.pk,
                'archivo': SimpleUploadedFile('recibo.pdf', b'%PDF-1.4 x', content_type='application/pdf')})
            self.assertEqual(r.status_code, 302)
            op.contacto.refresh_from_db()
            self.assertEqual(op.contacto.datos_extra[campo.slug][0]['nombre'], 'recibo.pdf')
            self.assertEqual(campo.valor_display(op.contacto.datos_extra[campo.slug]), '1 archivo')
            self.assertContains(self.client.get(op.get_absolute_url()), 'recibo.pdf')
            # Desde un adjunto de WhatsApp
            from django.core.files.base import ContentFile
            from django.core.files.storage import default_storage
            from apps.whatsapp.models import Conversacion, LineaWhatsApp, Mensaje
            ruta = default_storage.save('whatsapp/x/foto.jpg', ContentFile(b'jpgdata'))
            linea = LineaWhatsApp.objects.create(nombre='L', proveedor='demo')
            conv = Conversacion.objects.create(linea=linea, telefono=op.contacto.telefono, contacto=op.contacto,
                                               agente=self.ana)
            msg = Mensaje.objects.create(conversacion=conv, direccion='in', tipo='image', media_url=default_storage.url(ruta),
                                         media_mime='image/jpeg')
            url = f'/whatsapp/mensaje/{msg.pk}/guardar-adjunto/'
            self.assertEqual(self.client.get(url).json()['campos'][0]['slug'], campo.slug)
            r = self.client.post(url, json.dumps({'campo': campo.slug}), content_type='application/json')
            self.assertEqual(r.status_code, 200, r.content)
            op.contacto.refresh_from_db()
            self.assertEqual(len(op.contacto.datos_extra[campo.slug]), 2)
            # Quitar
            self.client.post(f'/contactos/{op.contacto_id}/archivos/', {'campo': campo.slug, 'quitar': '0'})
            op.contacto.refresh_from_db()
            self.assertEqual(len(op.contacto.datos_extra[campo.slug]), 1)


class PantallasTests(Base):
    def test_pantallas_tocadas_cargan(self):
        admin = User.objects.create_superuser('jefa', password='x', email='j@x.com')
        self.client.force_login(admin)
        op = self.ingresar(agente=self.ana).oportunidad
        crm.marcar_prioridad(op, 'Reingresó')
        CampoPersonalizado.objects.create(nombre='DNI frente', tipo=CampoPersonalizado.TIPO_ARCHIVO)
        accion = AccionEtapa.objects.first()
        for url in [f'/config/embudos/{self.embudo.pk}/', '/automatizaciones/nueva/', f'/automatizaciones/{accion.pk}/',
                    '/automatizaciones/', f'/tablero/?embudo={self.embudo.pk}&prioritaria=1&pauta=ninguna', '/oportunidades/?prioritaria=1&sin_calificar=1&vencida=1',
                    '/mi-dia/', '/supervision/', '/reportes/', op.get_absolute_url(), '/telefonia/llamadas/']:
            r = self.client.get(url, follow=True)
            self.assertEqual(r.status_code, 200, url)
        self.assertContains(self.client.get(op.get_absolute_url()), '🔥 Prioridad')
        r = self.client.post(f'/config/embudos/{self.embudo.pk}/resultados/', {
            'nombre': 'Agendó visita', 'contactado': 'on', 'orden': 9, 'activo': 'on', 'solo_este': '1'})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(ResultadoGestion.objects.filter(nombre='Agendó visita', embudo=self.embudo).exists())


class GestionPorCampaniaTests(Base):
    def test_llamadas_y_mensajes_por_pauta(self):
        from apps.pautas.models import Pauta
        from apps.reportes.analisis import gestion_por
        pauta = Pauta.objects.create(nombre='Dr Flex base')
        op = self.ingresar(agente=self.ana).oportunidad
        Oportunidad.objects.filter(pk=op.pk).update(pauta=pauta)
        self.llamada(op, self.ana)
        self.llamada(op, self.ana, estado=Llamada.ESTADO_NO_ATENDIDA)
        ini, fin = timezone.now() - timedelta(days=1), timezone.now() + timedelta(days=1)
        g = gestion_por('pauta', ini, fin)[pauta.pk]
        self.assertEqual((g['llamadas'], g['atendidas'], g['minutos']), (2, 1, 2))
        admin = User.objects.create_superuser('jefa', password='x', email='j@x.com')
        self.client.force_login(admin)
        self.assertContains(self.client.get('/pautas/'), 'Gestión por pauta')


class FormatoNumerosTests(TestCase):
    def test_ids_sin_separador_de_miles(self):
        """Los IDs van en botones, formularios y JS: '10.000' rompía llamar y las acciones masivas."""
        from django.template import Context, Template
        html = Template('{% load crm_extras %}<b data-op="{{ n }}">{{ n|miles }}</b>').render(Context({'n': 10000}))
        self.assertEqual(html, '<b data-op="10000">10.000</b>')
