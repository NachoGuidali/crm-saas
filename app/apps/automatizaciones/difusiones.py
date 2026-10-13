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
    from apps.whatsapp.services import enviar_automatico
    if op is None:
        return _omitir(dest, 'Sin oportunidad')
    msg, detalle = enviar_automatico(op, plantilla=dif.plantilla_wa, texto=dif.texto_wa, linea=dif.linea)
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
                _enviar_uno(dif, dest, conexion)
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


def omitidos_previstos(dif):
    """Cuántos se van a omitir por falta de email/teléfono, baja o "no contactar" (para avisar antes de enviar)."""
    qs = dif.destinatarios.filter(estado=DifusionDestinatario.PENDIENTE)
    if dif.canal == Difusion.CANAL_EMAIL:
        return qs.filter(Q(contacto__email='') | Q(contacto__no_email=True) | Q(contacto__no_contactar=True)).count()
    return qs.filter(Q(contacto__telefono='') | Q(contacto__no_contactar=True)).count()
