import re
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from core import tenancy
from core.cifrado import CampoCifrado

User = settings.AUTH_USER_MODEL


def _nueva_clave():
    return secrets.token_urlsafe(24)


class LineaWhatsApp(models.Model):
    """Un número de WhatsApp conectado. Cada línea elige su proveedor y tiene sus propias credenciales."""
    PROV_EVOLUTION = 'evolution'
    PROV_META = 'meta'
    PROV_TWILIO = 'twilio'
    PROV_DEMO = 'demo'
    PROVEEDOR_CHOICES = [
        (PROV_EVOLUTION, 'WhatsApp por QR (común o Business)'),
        (PROV_META, 'Meta Cloud API (oficial)'),
        (PROV_TWILIO, 'Twilio (oficial vía BSP)'),
        (PROV_DEMO, 'Simulado (demostración, no envía)'),
    ]
    ESTADO_CONECTADA = 'conectada'
    ESTADO_DESCONECTADA = 'desconectada'
    ESTADO_CONECTANDO = 'conectando'
    ESTADO_ERROR = 'error'
    ESTADO_CHOICES = [
        (ESTADO_CONECTADA, 'Conectada'), (ESTADO_DESCONECTADA, 'Desconectada'),
        (ESTADO_CONECTANDO, 'Conectando'), (ESTADO_ERROR, 'Error de credenciales'),
    ]

    nombre = models.CharField(max_length=100, help_text='Ej: "Ventas Doctor Flex", "Ventas 2".')
    proveedor = models.CharField(max_length=15, choices=PROVEEDOR_CHOICES, default=PROV_EVOLUTION)
    telefono = models.CharField(max_length=20, blank=True, help_text='Número de la línea (informativo).')
    activa = models.BooleanField(default=True)
    orden = models.PositiveSmallIntegerField(default=0)
    estado = models.CharField(max_length=15, choices=ESTADO_CHOICES, default=ESTADO_DESCONECTADA)
    estado_detalle = models.CharField(max_length=300, blank=True)
    estado_actualizado_at = models.DateTimeField(null=True, blank=True)

    embudo = models.ForeignKey(
        'crm.Embudo', null=True, blank=True, on_delete=models.SET_NULL, related_name='lineas_entrantes',
        verbose_name='Embudo para contactos nuevos',
        help_text='Si escribe alguien que no está en el CRM, se crea una oportunidad en este embudo. '
                  'Vacío = solo se crea el contacto y el chat.',
    )
    agentes = models.ManyToManyField(
        User, blank=True, related_name='lineas_whatsapp',
        help_text='Quiénes pueden usar esta línea. Vacío = todos los usuarios.',
    )
    webhook_key = models.CharField(max_length=64, unique=True, default=_nueva_clave, editable=False)
    min_segundos_entre_envios = models.PositiveSmallIntegerField(
        default=6, verbose_name='Segundos mínimos entre envíos automáticos',
        help_text='Anti-bloqueo para envíos masivos/automáticos. Sugerido: por QR 6–15 s, Meta/Twilio 1 s.',
    )

    # Conexión por QR
    evolution_instancia = models.SlugField(max_length=100, blank=True, verbose_name='Nombre de instancia')
    evolution_api_url = models.CharField(max_length=300, blank=True, help_text='Vacío = valor global del .env')
    evolution_api_key = CampoCifrado(blank=True, help_text='Vacío = valor global del .env')

    # Meta Cloud API
    meta_phone_number_id = models.CharField(max_length=100, blank=True, verbose_name='Phone Number ID')
    meta_waba_id = models.CharField(max_length=100, blank=True, verbose_name='WhatsApp Business Account ID')
    meta_access_token = CampoCifrado(blank=True, verbose_name='Access token permanente')
    meta_app_secret = CampoCifrado(blank=True, verbose_name='App secret')
    meta_verify_token = CampoCifrado(blank=True, verbose_name='Verify token del webhook')
    meta_api_version = models.CharField(max_length=10, default='v21.0')

    # Twilio
    twilio_account_sid = models.CharField(max_length=100, blank=True, verbose_name='Account SID')
    twilio_auth_token = CampoCifrado(blank=True, verbose_name='Auth token')
    twilio_from = models.CharField(max_length=40, blank=True, verbose_name='Número remitente',
                                   help_text='Número habilitado en Twilio, ej: +14155238886')

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['orden', 'nombre']
        verbose_name = 'Línea de WhatsApp'
        verbose_name_plural = 'Líneas de WhatsApp'

    def __str__(self):
        return f'{self.nombre}{f" ({self.telefono})" if self.telefono else ""}'

    @property
    def es_oficial(self):
        """Meta y Twilio aplican la regla de las 24 h: fuera de la ventana solo se puede mandar plantilla."""
        return self.proveedor in (self.PROV_META, self.PROV_TWILIO)

    @property
    def webhook_url(self):
        return tenancy.url_sitio() + reverse('whatsapp:webhook', args=[self.proveedor, self.webhook_key])

    def save(self, *args, **kwargs):
        # Meta pide un "verify token" para suscribir el webhook: si no se cargó, se genera uno.
        if self.proveedor == self.PROV_META and not self.meta_verify_token:
            self.meta_verify_token = _nueva_clave()[:32]
        super().save(*args, **kwargs)

    def get_evolution_url(self):
        return (self.evolution_api_url or settings.EVOLUTION_API_URL).rstrip('/')

    def usa_evolution_compartido(self):
        """Si usa el servidor Evolution de la plataforma (compartido entre clientes) y no uno propio."""
        return not self.evolution_api_url

    def get_evolution_key(self):
        # La clave global solo viaja al servidor de la plataforma, nunca a una URL que cargó el cliente.
        if self.usa_evolution_compartido():
            return self.evolution_api_key or settings.EVOLUTION_API_KEY
        return self.evolution_api_key

    def usable_por(self, user):
        if user.ve_todo:
            return True
        return not self.agentes.exists() or self.agentes.filter(pk=user.pk).exists()

    @classmethod
    def para_usuario(cls, user):
        qs = cls.objects.filter(activa=True)
        if user.ve_todo:
            return qs
        return qs.filter(Q(agentes=user) | Q(agentes__isnull=True)).distinct()


class ConversacionQuerySet(models.QuerySet):
    def visibles_para(self, user):
        """Supervisión ve todo. El agente ve sus chats y los sin asignar de las líneas que usa (para tomarlos)."""
        if user.ve_todo:
            return self
        lineas = LineaWhatsApp.para_usuario(user).values_list('pk', flat=True)
        return self.filter(Q(agente=user) | Q(agente__isnull=True, linea_id__in=list(lineas)))


class Conversacion(models.Model):
    ESTADO_PENDIENTE = 'pendiente'
    ESTADO_ABIERTA = 'abierta'
    ESTADO_CERRADA = 'cerrada'
    ESTADO_CHOICES = [(ESTADO_PENDIENTE, 'Sin atender'), (ESTADO_ABIERTA, 'En atención'), (ESTADO_CERRADA, 'Cerrada')]

    linea = models.ForeignKey(LineaWhatsApp, on_delete=models.PROTECT, related_name='conversaciones')
    telefono = models.CharField(max_length=20)
    nombre_perfil = models.CharField(max_length=200, blank=True)
    contacto = models.ForeignKey('crm.Contacto', null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name='conversaciones')
    agente = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name='conversaciones')
    estado = models.CharField(max_length=10, choices=ESTADO_CHOICES, default=ESTADO_PENDIENTE)
    no_leidos = models.PositiveIntegerField(default=0)
    ultimo_mensaje_at = models.DateTimeField(null=True, blank=True)
    ultimo_entrante_at = models.DateTimeField(null=True, blank=True)
    ultimo_mensaje_texto = models.CharField(max_length=200, blank=True)
    archivada = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = ConversacionQuerySet.as_manager()

    class Meta:
        ordering = ['-ultimo_mensaje_at']
        constraints = [models.UniqueConstraint(fields=['linea', 'telefono'], name='conversacion_unica_por_linea')]
        indexes = [
            models.Index(fields=['agente', 'archivada', '-ultimo_mensaje_at']),
            models.Index(fields=['archivada', '-ultimo_mensaje_at']),
        ]
        verbose_name = 'Conversación'
        verbose_name_plural = 'Conversaciones'

    def __str__(self):
        return self.nombre_mostrar

    @property
    def nombre_mostrar(self):
        if self.contacto_id:
            return self.contacto.nombre
        return self.nombre_perfil or self.telefono

    @property
    def ventana_abierta(self):
        if not self.linea.es_oficial:
            return True
        return bool(self.ultimo_entrante_at and timezone.now() - self.ultimo_entrante_at < timedelta(hours=24))

    @property
    def ventana_restante(self):
        if not self.ultimo_entrante_at:
            return None
        restante = self.ultimo_entrante_at + timedelta(hours=24) - timezone.now()
        return restante if restante.total_seconds() > 0 else None


class Mensaje(models.Model):
    DIR_ENTRANTE = 'in'
    DIR_SALIENTE = 'out'
    DIR_CHOICES = [(DIR_ENTRANTE, 'Entrante'), (DIR_SALIENTE, 'Saliente')]
    TIPO_TEXTO = 'text'
    TIPO_IMAGEN = 'image'
    TIPO_AUDIO = 'audio'
    TIPO_VIDEO = 'video'
    TIPO_DOCUMENTO = 'document'
    TIPO_PLANTILLA = 'template'
    TIPO_CHOICES = [(TIPO_TEXTO, 'Texto'), (TIPO_IMAGEN, 'Imagen'), (TIPO_AUDIO, 'Audio'), (TIPO_VIDEO, 'Video'),
                    (TIPO_DOCUMENTO, 'Documento'), (TIPO_PLANTILLA, 'Plantilla')]
    TIPOS_MEDIA = (TIPO_IMAGEN, TIPO_AUDIO, TIPO_VIDEO, TIPO_DOCUMENTO)
    STATUS_PENDIENTE = 'pending'
    STATUS_ENVIADO = 'sent'
    STATUS_ENTREGADO = 'delivered'
    STATUS_LEIDO = 'read'
    STATUS_FALLIDO = 'failed'
    STATUS_CHOICES = [(STATUS_PENDIENTE, 'Pendiente'), (STATUS_ENVIADO, 'Enviado'), (STATUS_ENTREGADO, 'Entregado'),
                      (STATUS_LEIDO, 'Leído'), (STATUS_FALLIDO, 'Fallido')]
    STATUS_ORDEN = {STATUS_PENDIENTE: 0, STATUS_ENVIADO: 1, STATUS_ENTREGADO: 2, STATUS_LEIDO: 3, STATUS_FALLIDO: 9}

    conversacion = models.ForeignKey(Conversacion, on_delete=models.CASCADE, related_name='mensajes')
    direccion = models.CharField(max_length=3, choices=DIR_CHOICES)
    tipo = models.CharField(max_length=10, choices=TIPO_CHOICES, default=TIPO_TEXTO)
    contenido = models.TextField(blank=True)
    media_url = models.CharField(max_length=1000, blank=True)
    media_mime = models.CharField(max_length=100, blank=True)
    media_filename = models.CharField(max_length=255, blank=True)
    media_id = models.CharField(max_length=200, blank=True)
    wa_id = models.CharField(max_length=200, blank=True, db_index=True, help_text='ID del mensaje en el proveedor')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDIENTE)
    error = models.TextField(blank=True)
    enviado_por = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    plantilla = models.ForeignKey('Plantilla', null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    plantilla_valores = models.JSONField(default=list, blank=True)
    automatico = models.BooleanField(default=False)
    accion = models.ForeignKey('automatizaciones.AccionEtapa', null=True, blank=True, on_delete=models.SET_NULL,
                               related_name='mensajes', help_text='Automatización que lo envió (para medir respuesta).')
    timestamp = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['timestamp', 'pk']
        constraints = [
            # Idempotencia: los proveedores reintentan webhooks; el mismo mensaje no se guarda dos veces.
            models.UniqueConstraint(fields=['wa_id'], condition=~Q(wa_id=''), name='mensaje_wa_id_unico'),
        ]
        indexes = [models.Index(fields=['conversacion', 'timestamp'])]

    def __str__(self):
        return f'[{self.direccion}] {self.contenido[:40]}'


class Plantilla(models.Model):
    """
    Mensaje predefinido. En líneas oficiales (Meta/Twilio) es la plantilla aprobada; en Evolution se envía
    como texto. Las variables {{1}}, {{2}}… se completan con los campos elegidos en `variables`.
    """
    CATEGORIA_CHOICES = [('MARKETING', 'Marketing'), ('UTILITY', 'Utilidad'), ('AUTHENTICATION', 'Autenticación')]
    ESTADO_LOCAL = 'local'
    ESTADO_PENDIENTE = 'PENDING'
    ESTADO_APROBADA = 'APPROVED'
    ESTADO_RECHAZADA = 'REJECTED'
    ESTADO_CHOICES = [(ESTADO_LOCAL, 'Solo local'), (ESTADO_PENDIENTE, 'En revisión en Meta'),
                      (ESTADO_APROBADA, 'Aprobada'), (ESTADO_RECHAZADA, 'Rechazada')]
    VARIABLES_DISPONIBLES = [
        ('nombre', 'Nombre completo'), ('primer_nombre', 'Primer nombre'), ('telefono', 'Teléfono'),
        ('email', 'Email'), ('embudo', 'Embudo / campaña'), ('etapa', 'Etapa'), ('agente', 'Nombre del agente'),
        ('fecha_atencion', 'Fecha de atención'),
    ]

    nombre = models.CharField(max_length=100, unique=True)
    cuerpo = models.TextField(help_text='Usá {{1}}, {{2}}… para variables.')
    variables = models.JSONField(default=list, blank=True, help_text='Campo para cada variable, en orden.')
    linea = models.ForeignKey(LineaWhatsApp, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name='plantillas', help_text='Vacío = disponible en todas las líneas.')
    categoria = models.CharField(max_length=20, choices=CATEGORIA_CHOICES, default='MARKETING')
    idioma = models.CharField(max_length=10, default='es_AR')
    meta_nombre = models.CharField(max_length=512, blank=True, help_text='Nombre técnico en Meta (snake_case).')
    meta_estado = models.CharField(max_length=10, choices=ESTADO_CHOICES, default=ESTADO_LOCAL)
    meta_rechazo = models.TextField(blank=True)
    twilio_content_sid = models.CharField(max_length=64, blank=True, verbose_name='Twilio ContentSid (HX…)')
    activa = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['nombre']

    def __str__(self):
        return self.nombre

    def get_meta_nombre(self):
        return self.meta_nombre or re.sub(r'[^a-z0-9_]', '_', self.nombre.strip().lower())

    def cantidad_variables(self):
        nums = [int(n) for n in re.findall(r'\{\{(\d+)\}\}', self.cuerpo)]
        return max(nums) if nums else 0

    @classmethod
    def variables_disponibles(cls):
        from apps.crm.models import CampoPersonalizado
        return cls.VARIABLES_DISPONIBLES + [(c.slug, f'★ {c.nombre}') for c in CampoPersonalizado.activos() if not c.es_archivo]

    def valores_para(self, contacto, oportunidad=None, agente=None):
        ctx = contexto_variables(contacto, oportunidad, agente)
        variables = list(self.variables or [])
        return [ctx.get(variables[i], '') if i < len(variables) else '' for i in range(self.cantidad_variables())]

    def renderizar(self, valores):
        texto = self.cuerpo
        for i, val in enumerate(valores, start=1):
            texto = texto.replace(f'{{{{{i}}}}}', str(val))
        return texto

    def disponible_en(self, linea):
        if self.linea_id and self.linea_id != linea.pk:
            return False
        if linea.proveedor == LineaWhatsApp.PROV_META:
            return self.meta_estado == self.ESTADO_APROBADA
        if linea.proveedor == LineaWhatsApp.PROV_TWILIO:
            return bool(self.twilio_content_sid)
        return True


def contexto_variables(contacto, oportunidad=None, agente=None):
    agente = agente or (oportunidad.agente if oportunidad else None)
    nombre = (contacto.nombre or '').strip() if contacto else ''
    return {
        'nombre': nombre,
        'primer_nombre': nombre.split()[0].title() if nombre else '',
        'telefono': contacto.telefono if contacto else '',
        'email': contacto.email if contacto else '',
        'embudo': oportunidad.embudo.nombre if oportunidad else '',
        'etapa': oportunidad.etapa.nombre if oportunidad else '',
        'agente': agente.first_name or agente.display_name if agente else '',
        'fecha_atencion': contacto.fecha_atencion.strftime('%d/%m/%Y') if contacto and contacto.fecha_atencion else '',
        **_extra_para_mensajes(contacto),
    }


def _extra_para_mensajes(contacto):
    """Los campos personalizados también se pueden usar en mensajes: {slug}."""
    if not contacto or not contacto.datos_extra:
        return {}
    from apps.crm.models import CampoPersonalizado
    campos = {c.slug: c for c in CampoPersonalizado.activos() if not c.es_archivo}
    return {slug: campos[slug].valor_display(v) for slug, v in contacto.datos_extra.items() if slug in campos}


def reemplazar_variables_texto(texto, contacto, oportunidad=None, agente=None):
    """Textos libres (mensajes automáticos, respuestas rápidas) aceptan {nombre}, {primer_nombre}, {agente}…"""
    ctx = contexto_variables(contacto, oportunidad, agente)
    return re.sub(r'\{(\w+)\}', lambda m: str(ctx.get(m.group(1), m.group(0))), texto or '')


class RespuestaRapida(models.Model):
    atajo = models.SlugField(max_length=30, unique=True, help_text='Se usa escribiendo /atajo en el chat.')
    titulo = models.CharField(max_length=100)
    texto = models.TextField(help_text='Acepta {primer_nombre}, {agente}, {embudo}…')
    activa = models.BooleanField(default=True)

    class Meta:
        ordering = ['atajo']

    def __str__(self):
        return f'/{self.atajo}'


class LogAPIWhatsApp(models.Model):
    linea = models.ForeignKey(LineaWhatsApp, null=True, blank=True, on_delete=models.CASCADE, related_name='logs')
    endpoint = models.CharField(max_length=300)
    metodo = models.CharField(max_length=10)
    status = models.IntegerField(null=True)
    request = models.TextField(blank=True)
    response = models.TextField(blank=True)
    exitoso = models.BooleanField(default=True)
    duracion_ms = models.IntegerField(null=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
