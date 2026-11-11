from celery import shared_task

# Reintentos: 1 min, 2, 4, 8, 16 (≈ media hora en total) antes de darlo por fallido.
ESPERAS = [60, 120, 240, 480, 960]


@shared_task(name='integraciones.enviar_webhook', bind=True, max_retries=len(ESPERAS))
def enviar_webhook(self, envio_pk):
    from .models import EnvioWebhook
    from . import webhooks
    envio = EnvioWebhook.objects.select_related('destino', 'oportunidad').filter(pk=envio_pk).first()
    if envio is None or envio.estado != EnvioWebhook.PENDIENTE:
        return 'nada'
    if webhooks.enviar_ahora(envio):
        webhooks.registrar_en_ficha(envio)
        return 'ok'
    if self.request.retries < len(ESPERAS):
        raise self.retry(countdown=ESPERAS[self.request.retries])
    webhooks.fallo_definitivo(envio)
    webhooks.registrar_en_ficha(envio)
    return 'error'
