from celery import shared_task

from . import cobros


@shared_task(name='clientes.revisar_cobros')
def revisar_cobros():
    """Corre en el schema público (ver CELERY_BEAT_SCHEDULE): avisos de vencimiento y suspensión por falta de pago."""
    r = cobros.revisar_cobros()
    return {'avisados': len(r['avisados']), 'suspendidos': len(r['suspendidos'])}
