"""
Clientes del SaaS. Cada cliente es un "tenant": tiene su propio schema de Postgres con todas las tablas
del CRM (usuarios, embudos, contactos, líneas, etc.), así que sus datos nunca se mezclan con los de otro.

Estas tablas viven solo en el schema público (las administra el panel de la plataforma).
"""
from django.conf import settings
from django.db import models
from django.utils import timezone
from django_tenants.models import DomainMixin, TenantMixin

# Subdominios que no se le pueden dar a un cliente (son de la plataforma).
SUBDOMINIOS_RESERVADOS = frozenset({
    'www', 'app', 'api', 'admin', 'panel', 'public', 'static', 'media', 'mail', 'smtp', 'ftp',
    'soporte', 'ayuda', 'status', 'blog', 'ingresar', 'login', 'evolution', 'test', 'demo',
})

# ── Funciones extra (opcionales, para habilitar o cobrar aparte) ─────────────
# Funciones que NO vienen por defecto: se prenden por plan (para todos sus clientes)
# o por cliente puntual (solo para uno). Para sumar una función nueva:
#   1) Agregá una línea acá: ('clave', 'Nombre que ves en el panel').
#   2) En el código, encerrá la función con `tiene_funcion('clave')` (core.tenancy),
#      o en un template con `{% if 'clave' in funciones_cliente %}`.
#   3) Tildala en el plan o en la ficha del cliente desde el panel.
# Con la lista vacía no hay funciones extra: el CRM funciona igual para todos.
FUNCIONES_EXTRA = [
    ('postventa', 'Clientes / Postventa (cuentas, proyectos y tareas)'),
    # ('reportes_avanzados', 'Reportes avanzados'),
    # ('marca_blanca', 'Marca blanca (logo propio en el CRM)'),
]


class Plan(models.Model):
    nombre = models.CharField(max_length=60, unique=True)
    precio_mensual = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    max_usuarios = models.PositiveIntegerField(default=5, help_text='0 = sin límite.')
    max_lineas_whatsapp = models.PositiveIntegerField(default=1, help_text='0 = sin límite.')
    max_wa_dia = models.PositiveIntegerField(default=0, verbose_name='WhatsApp por día',
                                             help_text='Mensajes de WhatsApp automáticos y difusiones por día. 0 = sin límite. Las respuestas 1 a 1 de los agentes no cuentan.')
    funciones_extra = models.JSONField(default=list, blank=True,
                                       help_text='Funciones opcionales incluidas en este plan.')
    activo = models.BooleanField(default=True, help_text='Si se puede elegir para clientes nuevos.')

    class Meta:
        ordering = ['precio_mensual', 'nombre']
        verbose_name = 'Plan'
        verbose_name_plural = 'Planes'

    def __str__(self):
        return self.nombre


class Cliente(TenantMixin):
    ESTADO_PRUEBA = 'prueba'
    ESTADO_ACTIVO = 'activo'
    ESTADO_SUSPENDIDO = 'suspendido'
    ESTADO_BAJA = 'baja'
    ESTADO_CHOICES = [
        (ESTADO_PRUEBA, 'En prueba'),
        (ESTADO_ACTIVO, 'Activo'),
        (ESTADO_SUSPENDIDO, 'Suspendido'),
        (ESTADO_BAJA, 'Dado de baja'),
    ]
    ESTADOS_CON_ACCESO = (ESTADO_PRUEBA, ESTADO_ACTIVO)

    COBRO_MANUAL = 'manual'
    COBRO_MERCADOPAGO = 'mercadopago'
    COBRO_CHOICES = [(COBRO_MANUAL, 'Manual (transferencia / efectivo)'), (COBRO_MERCADOPAGO, 'Mercado Pago (débito automático)')]

    SUSPENSION_MANUAL = 'manual'
    SUSPENSION_FALTA_PAGO = 'falta_pago'

    nombre = models.CharField(max_length=150, verbose_name='Empresa')
    estado = models.CharField(max_length=15, choices=ESTADO_CHOICES, default=ESTADO_PRUEBA, db_index=True)
    plan = models.ForeignKey(Plan, null=True, blank=True, on_delete=models.PROTECT, related_name='clientes')
    # Límites propios: si quedan vacíos rigen los del plan (permite excepciones por cliente).
    max_usuarios = models.PositiveIntegerField(null=True, blank=True, help_text='Vacío = el del plan. 0 = sin límite.')
    max_lineas_whatsapp = models.PositiveIntegerField(null=True, blank=True,
                                                      help_text='Vacío = el del plan. 0 = sin límite.')
    max_wa_dia = models.PositiveIntegerField(null=True, blank=True, verbose_name='WhatsApp por día',
                                             help_text='Vacío = el del plan. 0 = sin límite.')
    # Funciones extra habilitadas solo a este cliente: se SUMAN a las del plan (no lo pisan).
    funciones_extra = models.JSONField(default=list, blank=True,
                                       help_text='Funciones extra para este cliente (además de las del plan).')
    contacto_nombre = models.CharField(max_length=150, blank=True)
    contacto_email = models.EmailField(blank=True)
    contacto_telefono = models.CharField(max_length=40, blank=True)
    prueba_hasta = models.DateField(null=True, blank=True)

    # ── Cobro ──
    cobro = models.CharField('Medio de cobro', max_length=15, choices=COBRO_CHOICES, default=COBRO_MANUAL)
    precio_acordado = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True,
                                          help_text='Vacío = el precio del plan.')
    pagado_hasta = models.DateField(null=True, blank=True, help_text='Fecha hasta la que tiene el servicio pago.')
    suspension_automatica = models.BooleanField(
        default=True, help_text='Suspender solo si vence y no paga (pasados los días de gracia).')
    motivo_suspension = models.CharField(max_length=15, blank=True)
    aviso_vencimiento_enviado = models.DateField(null=True, blank=True, editable=False,
                                                 help_text='Vencimiento para el que ya se mandó el aviso por email.')
    # Suscripción de Mercado Pago (preapproval)
    mp_suscripcion_id = models.CharField(max_length=64, blank=True, db_index=True)
    mp_suscripcion_estado = models.CharField(max_length=20, blank=True)
    mp_suscripcion_link = models.URLField(max_length=500, blank=True)
    mp_email_pagador = models.EmailField(blank=True)

    notas = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # El schema se crea y migra al guardar el cliente por primera vez.
    auto_create_schema = True
    # Al borrar un cliente NO se borra su schema (se hace a mano, a propósito, después de un backup).
    auto_drop_schema = False

    class Meta:
        ordering = ['nombre']
        verbose_name = 'Cliente'
        verbose_name_plural = 'Clientes'

    def __str__(self):
        return self.nombre

    @property
    def es_publico(self):
        from django_tenants.utils import get_public_schema_name
        return self.schema_name == get_public_schema_name()

    @property
    def tiene_acceso(self):
        return self.es_publico or self.estado in self.ESTADOS_CON_ACCESO

    @property
    def dominio_principal(self):
        dom = self.domains.filter(is_primary=True).first() or self.domains.first()
        return dom.domain if dom else ''

    # ── Cobro ──
    @property
    def precio(self):
        if self.precio_acordado is not None:
            return self.precio_acordado
        return self.plan.precio_mensual if self.plan_id else 0

    @property
    def vencimiento(self):
        """Hasta cuándo puede usar el CRM: lo pagado o, si nunca pagó y está en prueba, el fin de la prueba."""
        if self.pagado_hasta:
            return self.pagado_hasta
        return self.prueba_hasta if self.estado == self.ESTADO_PRUEBA else None

    @property
    def dias_para_vencer(self):
        """Días hasta el vencimiento (negativo = vencido hace N días). None si no tiene fecha."""
        venc = self.vencimiento
        return (venc - timezone.localdate()).days if venc else None

    @property
    def dias_vencido(self):
        dias = self.dias_para_vencer
        return -dias if dias is not None and dias < 0 else 0

    @property
    def estado_cobro(self):
        """al_dia | por_vencer | en_gracia | vencido | sin_fecha | '' (sin acceso: no aplica)."""
        if self.estado not in self.ESTADOS_CON_ACCESO:
            return ''
        dias = self.dias_para_vencer
        if dias is None:
            return 'sin_fecha'
        if dias > settings.SAAS_DIAS_AVISO:
            return 'al_dia'
        if dias >= 0:
            return 'por_vencer'
        return 'en_gracia' if -dias <= settings.SAAS_DIAS_GRACIA else 'vencido'

    def _limite(self, campo):
        propio = getattr(self, campo)
        if propio is not None:
            return propio
        return getattr(self.plan, campo) if self.plan_id else 0

    @property
    def limite_usuarios(self):
        """0 = sin límite."""
        return self._limite('max_usuarios')

    @property
    def limite_lineas_whatsapp(self):
        """0 = sin límite."""
        return self._limite('max_lineas_whatsapp')

    @property
    def limite_wa_dia(self):
        """Mensajes de WhatsApp automáticos/difusiones por día. 0 = sin límite."""
        return self._limite('max_wa_dia')

    # ── Funciones extra ──
    @property
    def funciones_habilitadas(self):
        """Claves de funciones extra activas: las del plan + las habilitadas al cliente."""
        claves = set(self.funciones_extra or [])
        if self.plan_id:
            claves |= set(self.plan.funciones_extra or [])
        return claves

    def tiene_funcion(self, clave):
        return clave in self.funciones_habilitadas


class Dominio(DomainMixin):
    class Meta:
        verbose_name = 'Dominio'
        verbose_name_plural = 'Dominios'


class Pago(models.Model):
    """Pago de un cliente: cada uno extiende `Cliente.pagado_hasta`. Los de Mercado Pago entran solos por webhook."""
    MEDIO_TRANSFERENCIA = 'transferencia'
    MEDIO_EFECTIVO = 'efectivo'
    MEDIO_MERCADOPAGO = 'mercadopago'
    MEDIO_OTRO = 'otro'
    MEDIO_CHOICES = [(MEDIO_TRANSFERENCIA, 'Transferencia'), (MEDIO_EFECTIVO, 'Efectivo'),
                     (MEDIO_MERCADOPAGO, 'Mercado Pago'), (MEDIO_OTRO, 'Otro')]

    cliente = models.ForeignKey(Cliente, on_delete=models.CASCADE, related_name='pagos')
    fecha = models.DateField(default=timezone.localdate)
    monto = models.DecimalField(max_digits=12, decimal_places=2)
    medio = models.CharField(max_length=15, choices=MEDIO_CHOICES, default=MEDIO_TRANSFERENCIA)
    periodo_desde = models.DateField()
    periodo_hasta = models.DateField()
    referencia = models.CharField(max_length=120, blank=True, help_text='Nº de operación, comprobante, etc.')
    # Id del pago en Mercado Pago: evita registrar dos veces el mismo pago si el webhook llega repetido.
    mp_pago_id = models.CharField(max_length=40, blank=True)
    notas = models.CharField(max_length=255, blank=True)
    registrado_por = models.CharField(max_length=150, blank=True)
    anulado = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-fecha', '-pk']
        constraints = [models.UniqueConstraint(fields=['mp_pago_id'], condition=~models.Q(mp_pago_id=''),
                                               name='pago_mp_unico')]

    def __str__(self):
        return f'{self.cliente} · {self.monto} · {self.fecha:%d/%m/%Y}'
