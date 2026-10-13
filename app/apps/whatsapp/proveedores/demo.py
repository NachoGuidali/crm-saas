"""Proveedor simulado: para capacitación y presentaciones. No sale nada a WhatsApp."""
import random
import uuid

from .base import MensajeEntrante, ProveedorBase, ResultadoWebhook

RESPUESTAS = ['¡Hola! Sí, contame más 🙂', '¿Cuánto sale por mes?', 'Ahora no puedo, ¿me llamás a la tarde?',
              'Gracias, lo voy a pensar.', '¿Qué cobertura tiene?', 'Dale, pasame la info por acá.']


class ProveedorDemo(ProveedorBase):
    nombre = 'Simulado'

    def _id(self):
        return f'demo-{uuid.uuid4().hex[:16]}'

    def enviar_texto(self, telefono, texto):
        self._quizas_responder(telefono)
        return self._id()

    def enviar_media(self, telefono, url, mime, filename='', caption=''):
        return self._id()

    def consultar_estado(self):
        from apps.whatsapp.models import LineaWhatsApp as L
        return L.ESTADO_CONECTADA, 'Línea simulada (no envía mensajes reales)'

    def parsear_webhook(self, request):
        return ResultadoWebhook()

    def _quizas_responder(self, telefono):
        """A veces el 'cliente' contesta a los pocos segundos, para mostrar el circuito completo."""
        if random.random() > 0.35:
            return
        from apps.whatsapp.tasks import procesar_mensaje_entrante_task
        msg = MensajeEntrante(telefono=telefono, wa_id=self._id(), contenido=random.choice(RESPUESTAS))
        try:
            procesar_mensaje_entrante_task.apply_async(args=[self.linea.pk, msg.to_dict()], countdown=random.randint(4, 12))
        except Exception:
            pass
