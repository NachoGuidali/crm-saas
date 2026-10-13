from celery import shared_task


@shared_task(name='automatizaciones.ejecutar_accion')
def ejecutar_accion(ejecucion_id):
    from .services import ejecutar
    return ejecutar(ejecucion_id)


@shared_task(name='automatizaciones.revisar_inactividad')
def revisar_inactividad():
    from .services import revisar_inactividad as revisar
    return revisar()


@shared_task(name='automatizaciones.barrer_programadas')
def barrer_programadas():
    from .services import barrer_programadas as barrer
    return barrer()


@shared_task(name='automatizaciones.revisar_sin_actividad')
def revisar_sin_actividad():
    from .services import revisar_sin_actividad as revisar
    return revisar()


@shared_task(name='automatizaciones.difusiones_tick')
def difusiones_tick():
    from .difusiones import tick
    return tick()
