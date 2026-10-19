from django.conf import settings

from core.tenancy import cliente_actual


def crm(request):
    cliente = cliente_actual()
    ctx = {'CRM_NOMBRE': settings.CRM_NOMBRE, 'SAAS_DOMINIO': settings.SAAS_DOMINIO,
           # En el CRM de cada cliente, debajo del logo va el nombre de su empresa.
           'CRM_SUBTITULO': cliente.nombre if cliente is not None else settings.CRM_SUBTITULO}
    user = getattr(request, 'user', None)
    if not user or not user.is_authenticated or cliente is None:
        return ctx  # dominio principal (panel de la plataforma): no hay tablas del CRM
    from apps.users.services import contar_no_leidas
    from apps.crm.services import contar_tareas_hoy
    from apps.whatsapp.services import contar_no_leidos_usuario
    from apps.telefonia.services import usuario_tiene_interno
    ctx.update({
        'perms_crm': user.permisos_efectivos,
        'notif_no_leidas': contar_no_leidas(user),
        'tareas_hoy': contar_tareas_hoy(user),
        'wa_no_leidos': contar_no_leidos_usuario(user),
        'tiene_interno_anura': usuario_tiene_interno(user),
        # Funciones extra habilitadas: en templates -> {% if 'clave' in funciones_cliente %}
        'funciones_cliente': cliente.funciones_habilitadas,
    })
    if user.is_admin:
        ctx['aviso_suscripcion'] = _aviso_suscripcion(cliente)
    return ctx


def _aviso_suscripcion(cliente):
    """Banner para los administradores del cliente cuando la suscripción está por vencer o vencida."""
    from apps.clientes.cobros import debita_solo
    if cliente.estado_cobro not in ('por_vencer', 'en_gracia', 'vencido') or debita_solo(cliente):
        return None
    return {'vencimiento': cliente.vencimiento, 'vencido': cliente.dias_vencido > 0,
            'prueba': cliente.estado == cliente.ESTADO_PRUEBA and not cliente.pagado_hasta,
            'datos_pago': settings.SAAS_DATOS_PAGO}
