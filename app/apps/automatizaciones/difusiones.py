"""
Difusiones: envío masivo por email o WhatsApp a un grupo de leads elegido con los filtros de la lista.

- Los destinatarios se fijan al crear la difusión (una persona una sola vez, aunque tenga varias oportunidades).
- Se envía de a tandas (cada minuto): email según "Emails por minuto" de la configuración; WhatsApp respetando el
  ritmo anti-bloqueo de cada línea.
- Al enviar se vuelve a chequear: "no contactar", baja de emails, sin email / teléfono.
"""
import logging
from datetime import timedelta

from django.db import transaction
from django.db.models import Count, Exists, OuterRef, Q
from django.utils import timezone

from .models import ConfigEmail, Difusion, DifusionDestinatario

logger = logging.getLogger('apps.automatizaciones')

TANDA_WHATSAPP = 20


def crear(usuario, oportunidades, descripcion=''):
    """Crea un borrador con un destinatario por persona (la oportunidad más reciente de cada una)."""
    dif = Difusion.objects.create(nombre=f'Difusión {timezone.localtime():%d/%m %H:%M}', creada_por=usuario,
                                  descripcion_filtro=descripcion[:500])
    vistos, filas = set(), []
    for op_id, contacto_id in oportunidades.order_by('contacto_id', '-created_at').values_list('pk', 'contacto_id'):
        if contacto_id in vistos:
            continue
        vistos.add(contacto_id)
        filas.append(DifusionDestinatario(difusion=dif, contacto_id=contacto_id, oportunidad_id=op_id))
    DifusionDestinatario.objects.bulk_create(filas, batch_size=1000)
    return dif


def validar(dif):
    if dif.canal == Difusion.CANAL_EMAIL and dif.plantilla_email is None:
        return 'Elegí la plantilla de email.'
    if dif.canal == Difusion.CANAL_WHATSAPP and dif.plantilla_wa is None and not dif.texto_wa.strip():
        return 'Elegí una plantilla de WhatsApp o escribí el texto.'
    if dif.adjunto and dif.canal == Difusion.CANAL_WHATSAPP and dif.plantilla_wa is not None:
        return 'La imagen va con texto libre: sacá la plantilla o sacá la imagen.'
    if dif.adjunto and dif.linea_id and dif.linea.es_oficial:
        return f'La línea {dif.linea} es oficial: no puede mandar imágenes en una difusión (solo plantillas aprobadas).'
    if dif.canal == Difusion.CANAL_AGENTE:
        from apps.agente.models import ConfigAgente
        from apps.agente.services import habilitado
        if not habilitado():
            return 'El módulo Agente IA no está habilitado.'
        if not ConfigAgente.get().activo:
            return 'El agente IA está apagado: prendelo en Agente IA → Configuración.'
        if not dif.texto_wa.strip():
            return 'Escribí el objetivo del mensaje para el agente.'
    if not dif.destinatarios.exists():
        return 'La difusión no tiene destinatarios.'
    return None


def iniciar(dif, cuando=None):
    error = validar(dif)
    if error:
        raise ValueError(error)
    dif.programada_para = cuando or timezone.now()
    dif.estado = Difusion.PROGRAMADA
    dif.save(update_fields=['programada_para', 'estado'])


def _omitir(dest, motivo):
    DifusionDestinatario.objects.filter(pk=dest.pk).update(estado=DifusionDestinatario.OMITIDO, detalle=motivo[:300])


def adjunto_de(dif):
    """(url, mime, nombre) del adjunto de la difusión, o None."""
    if not dif.adjunto:
        return None
    import mimetypes
    import os
    nombre = os.path.basename(dif.adjunto.name)
    return dif.adjunto.url, mimetypes.guess_type(nombre)[0] or 'application/octet-stream', nombre


SIN_CUPO = 'sin_cupo'
TANDA_AGENTE = 5  # por minuto: el espaciado real lo dan las pausas de cada línea


def _enviar_uno(dif, dest, conexion=None):
    from apps.whatsapp.models import reemplazar_variables_texto
    contacto, op = dest.contacto, dest.oportunidad
    if contacto.no_contactar:
        return _omitir(dest, 'Pidió no ser contactado')
    if dif.canal == Difusion.CANAL_EMAIL:
        from .services import enviar_email
        if not contacto.email:
            return _omitir(dest, 'Sin email')
        if contacto.no_email:
            return _omitir(dest, 'Se dio de baja de los emails')
        pe = dif.plantilla_email
        envio = enviar_email(contacto, op, reemplazar_variables_texto(pe.asunto, contacto, op),
                             reemplazar_variables_texto(pe.cuerpo, contacto, op), difusion=dif, plantilla=pe,
                             conexion=conexion)
        DifusionDestinatario.objects.filter(pk=dest.pk).update(estado=DifusionDestinatario.ENVIADO, email=envio,
                                                               enviado_at=timezone.now())
        return
    if op is None:
        return _omitir(dest, 'Sin oportunidad')
    if dif.canal == Difusion.CANAL_AGENTE:
        # El agente escribe un mensaje propio para cada uno, con las pausas, topes y reparto de líneas del agente.
        from apps.agente.services import iniciar_chat
        ok, detalle, reintentar = iniciar_chat(op, objetivo=dif.texto_wa, destinatario_difusion=dest.pk,
                                               adjunto=adjunto_de(dif))
        if reintentar:
            return SIN_CUPO  # hoy no hay cupo en las líneas: queda pendiente y se sigue en la próxima vuelta
        if not ok:
            return _omitir(dest, detalle)
        DifusionDestinatario.objects.filter(pk=dest.pk).update(estado=DifusionDestinatario.ENVIADO,
                                                               enviado_at=timezone.now(), detalle='')
        return None
    from apps.whatsapp.services import enviar_automatico
    msg, detalle = enviar_automatico(op, plantilla=dif.plantilla_wa, texto=dif.texto_wa, linea=dif.linea,
                                     adjunto=adjunto_de(dif))
    if msg is None:
        return _omitir(dest, detalle)
    DifusionDestinatario.objects.filter(pk=dest.pk).update(estado=DifusionDestinatario.ENVIADO, mensaje=msg,
                                                           enviado_at=timezone.now())


def tick():
    """Cada minuto: arranca las programadas que llegaron a su hora y envía una tanda de cada una en curso."""
    ahora = timezone.now()
    Difusion.objects.filter(estado=Difusion.PROGRAMADA, programada_para__lte=ahora).update(
        estado=Difusion.ENVIANDO, iniciada_at=ahora)
    enviados = 0
    config = ConfigEmail.get()
    # Cupo de WhatsApp que le queda al cliente hoy (límite diario del plan); None = sin límite.
    from apps.whatsapp.services import wa_dia_restante
    cupo_wa = wa_dia_restante()
    for dif in Difusion.objects.filter(estado=Difusion.ENVIANDO).select_related('plantilla_email', 'plantilla_wa', 'linea'):
        if dif.canal == Difusion.CANAL_EMAIL:
            tanda = max(config.por_minuto, 1)
        elif dif.canal == Difusion.CANAL_AGENTE:
            tanda = TANDA_AGENTE
        else:
            tanda = TANDA_WHATSAPP if cupo_wa is None else min(TANDA_WHATSAPP, cupo_wa)
            if tanda <= 0:
                continue  # llegó al tope diario de WhatsApp: las pendientes siguen mañana
        with transaction.atomic():
            ids = list(dif.destinatarios.select_for_update(skip_locked=True)
                       .filter(estado=DifusionDestinatario.PENDIENTE).order_by('pk').values_list('pk', flat=True)[:tanda])
        conexion = None
        if dif.canal == Difusion.CANAL_EMAIL and ids:
            from .services import conexion_email
            conexion = conexion_email()
        for dest in DifusionDestinatario.objects.filter(pk__in=ids).select_related('contacto', 'oportunidad__embudo',
                                                                                   'oportunidad__agente'):
            try:
                if _enviar_uno(dif, dest, conexion) == SIN_CUPO:
                    break  # las líneas del agente llegaron al tope de hoy: el resto sigue pendiente
                enviados += 1
                if dif.canal == Difusion.CANAL_WHATSAPP and cupo_wa is not None:
                    cupo_wa -= 1  # consume cupo diario del cliente (afecta a las demás difusiones del tick)
            except Exception as e:  # un error con una persona no frena la difusión
                logger.warning('Difusión %s: error con %s: %s', dif.pk, dest.contacto_id, e)
                DifusionDestinatario.objects.filter(pk=dest.pk).update(estado=DifusionDestinatario.ERROR,
                                                                       detalle=str(e)[:300])
        if not dif.destinatarios.filter(estado=DifusionDestinatario.PENDIENTE).exists():
            Difusion.objects.filter(pk=dif.pk).update(estado=Difusion.FINALIZADA, finalizada_at=timezone.now())
            if dif.creada_por:
                from apps.users.services import notificar
                notificar(dif.creada_por, 'sistema', f'Difusión "{dif}" terminada', '', f'/automatizaciones/difusiones/{dif.pk}/')
    return enviados


def resultados(dif, horas_respuesta=48):
    """Contadores de la difusión: por estado, y de lo enviado: entregados / leídos / respondidos o abiertos / clics / bajas."""
    from apps.whatsapp.models import Mensaje
    D = DifusionDestinatario
    por_estado = dict(dif.destinatarios.values_list('estado').annotate(n=Count('pk')).values_list('estado', 'n'))
    r = {'total': sum(por_estado.values()), 'pendientes': por_estado.get(D.PENDIENTE, 0),
         'enviados': por_estado.get(D.ENVIADO, 0), 'omitidos': por_estado.get(D.OMITIDO, 0),
         'errores': por_estado.get(D.ERROR, 0)}
    enviados = dif.destinatarios.filter(estado=D.ENVIADO)
    if dif.canal == Difusion.CANAL_EMAIL:
        agg = enviados.aggregate(abiertos=Count('pk', filter=Q(email__abierto_at__isnull=False)),
                                 clics=Count('pk', filter=Q(email__clic_at__isnull=False)),
                                 bajas=Count('pk', filter=Q(email__baja_at__isnull=False)))
    else:
        resp = Mensaje.objects.filter(conversacion=OuterRef('mensaje__conversacion'), direccion=Mensaje.DIR_ENTRANTE,
                                      timestamp__gt=OuterRef('mensaje__timestamp'),
                                      timestamp__lte=OuterRef('mensaje__timestamp') + timedelta(hours=horas_respuesta))
        agg = enviados.annotate(resp=Exists(resp)).aggregate(
            entregados=Count('pk', filter=Q(mensaje__status__in=[Mensaje.STATUS_ENTREGADO, Mensaje.STATUS_LEIDO])),
            leidos=Count('pk', filter=Q(mensaje__status=Mensaje.STATUS_LEIDO)),
            fallidos=Count('pk', filter=Q(mensaje__status=Mensaje.STATUS_FALLIDO)),
            respondidos=Count('pk', filter=Q(resp=True)))
    r.update(agg)
    base = r['enviados'] or 0
    r['pct'] = {k: round(v * 100 / base, 1) if base else 0 for k, v in agg.items()}
    return r


# Filtro por resultado (lo que pasó después de enviado). clave → etiqueta
RESULTADOS_WA = [('entregado', 'Entregados'), ('leido', 'Leídos'), ('respondio', 'Respondieron'),
                 ('fallido', 'Fallidos'), ('sin_confirmar', 'Sin confirmar')]
RESULTADOS_EMAIL = [('abierto', 'Abrieron'), ('clic', 'Hicieron clic'), ('sin_abrir', 'Sin abrir'), ('baja', 'Se dieron de baja')]


def resultados_de(dif):
    return RESULTADOS_EMAIL if dif.canal == Difusion.CANAL_EMAIL else RESULTADOS_WA


def con_respuesta(qs, horas_respuesta=48):
    from apps.whatsapp.models import Mensaje
    resp = Mensaje.objects.filter(conversacion=OuterRef('mensaje__conversacion'), direccion=Mensaje.DIR_ENTRANTE,
                                  timestamp__gt=OuterRef('mensaje__timestamp'),
                                  timestamp__lte=OuterRef('mensaje__timestamp') + timedelta(hours=horas_respuesta))
    return qs.annotate(resp=Exists(resp))


def filtrar_resultado(qs, dif, resultado):
    """Destinatarios enviados con ese resultado (los mismos criterios que los contadores de resultados())."""
    from apps.whatsapp.models import Mensaje
    D = DifusionDestinatario
    env = qs.filter(estado=D.ENVIADO)
    if dif.canal == Difusion.CANAL_EMAIL:
        return {'abierto': env.filter(email__abierto_at__isnull=False), 'clic': env.filter(email__clic_at__isnull=False),
                'sin_abrir': env.filter(email__abierto_at__isnull=True),
                'baja': env.filter(email__baja_at__isnull=False)}.get(resultado, qs)
    filtrado = {
        'entregado': env.filter(mensaje__status__in=[Mensaje.STATUS_ENTREGADO, Mensaje.STATUS_LEIDO]),
        'leido': env.filter(mensaje__status=Mensaje.STATUS_LEIDO),
        'fallido': env.filter(mensaje__status=Mensaje.STATUS_FALLIDO),
        'sin_confirmar': env.filter(Q(mensaje__isnull=True) | Q(mensaje__status__in=[Mensaje.STATUS_PENDIENTE, Mensaje.STATUS_ENVIADO])),
    }.get(resultado) if resultado != 'respondio' else (
        env if 'resp' in env.query.annotations else con_respuesta(env)).filter(resp=True)
    return filtrado if filtrado is not None else qs


def resultado_texto(d, dif):
    """Resultado legible de un destinatario (para la tabla y el Excel)."""
    D = DifusionDestinatario
    if d.estado != D.ENVIADO:
        return d.detalle or d.get_estado_display()
    if dif.canal == Difusion.CANAL_EMAIL:
        if d.email is None:
            return 'Enviado'
        t = 'Hizo clic' if d.email.clic_at else 'Abrió' if d.email.abierto_at else 'Sin abrir'
        return t + (' · se dio de baja' if d.email.baja_at else '')
    if d.mensaje is None:
        return 'Enviado'
    t = d.mensaje.get_status_display()
    if d.mensaje.status == d.mensaje.STATUS_FALLIDO and d.mensaje.error:
        t += f': {d.mensaje.error}'
    if getattr(d, 'resp', False):
        t += ' · respondió'
    return t


def omitidos_previstos(dif):
    """Cuántos se van a omitir por falta de email/teléfono, baja o "no contactar" (para avisar antes de enviar)."""
    qs = dif.destinatarios.filter(estado=DifusionDestinatario.PENDIENTE)
    if dif.canal == Difusion.CANAL_EMAIL:
        return qs.filter(Q(contacto__email='') | Q(contacto__no_email=True) | Q(contacto__no_contactar=True)).count()
    return qs.filter(Q(contacto__telefono='') | Q(contacto__no_contactar=True)).count()
