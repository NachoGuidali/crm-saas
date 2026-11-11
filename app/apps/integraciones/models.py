import hashlib
import secrets

from django.conf import settings
from django.db import models

from core.cifrado import CampoCifrado


def hash_clave(clave: str) -> str:
    return hashlib.sha256(clave.encode()).hexdigest()


class ApiKey(models.Model):
    """Clave para sistemas externos (landings, n8n, sistema del centro médico). Se guarda solo el hash."""
    nombre = models.CharField(max_length=100)
    prefijo = models.CharField(max_length=12, editable=False)
    hash = models.CharField(max_length=64, unique=True, editable=False)
    embudo = models.ForeignKey('crm.Embudo', null=True, blank=True, on_delete=models.SET_NULL, related_name='+',
                               help_text='Embudo por defecto para los leads que entran con esta clave.')
    fuente = models.CharField(max_length=150, blank=True, help_text='Texto que queda como "fuente" del lead.')
    activa = models.BooleanField(default=True)
    ultimo_uso_at = models.DateTimeField(null=True, blank=True)
    creada_por = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['nombre']
        verbose_name = 'API key'
        verbose_name_plural = 'API keys'

    def __str__(self):
        return f'{self.nombre} ({self.prefijo}…)'

    @classmethod
    def generar(cls, **kwargs):
        clave = 'crm_' + secrets.token_urlsafe(32)
        obj = cls.objects.create(prefijo=clave[:10], hash=hash_clave(clave), **kwargs)
        return obj, clave


class LogIntegracion(models.Model):
    api_key = models.ForeignKey(ApiKey, null=True, blank=True, on_delete=models.SET_NULL, related_name='logs')
    endpoint = models.CharField(max_length=200)
    metodo = models.CharField(max_length=10)
    status = models.PositiveSmallIntegerField()
    ip = models.CharField(max_length=60, blank=True)
    request = models.TextField(blank=True)
    response = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']


class ConfigSMS(models.Model):
    """SMS por Twilio (único). Necesita un número de Twilio con SMS habilitado."""
    activo = models.BooleanField(default=False)
    account_sid = models.CharField(max_length=64, blank=True, verbose_name='Account SID')
    auth_token = CampoCifrado(blank=True, verbose_name='Auth token')
    numero = models.CharField(max_length=30, blank=True, verbose_name='Número remitente',
                              help_text='El número de Twilio con SMS, en formato +1… / +54…, o un Messaging Service SID (MG…).')
    webhook_token = models.CharField(max_length=40, blank=True, editable=False)

    class Meta:
        verbose_name = 'Configuración SMS'

    @classmethod
    def get(cls):
        import secrets
        obj, _ = cls.objects.get_or_create(pk=1, defaults={'webhook_token': secrets.token_urlsafe(24)})
        if not obj.webhook_token:
            obj.webhook_token = secrets.token_urlsafe(24)
            obj.save(update_fields=['webhook_token'])
        return obj

    @property
    def operativo(self):
        return self.activo and self.account_sid and self.auth_token and self.numero


class WebhookDestino(models.Model):
    """Sistema externo al que el CRM le manda datos (CRM de cerradores, videollamada de validación, ERP…)."""
    AUTH_BEARER = 'bearer'
    AUTH_HEADER = 'header'
    AUTH_NINGUNA = 'ninguna'
    AUTH_CHOICES = [(AUTH_BEARER, 'Authorization: Bearer <token>'), (AUTH_HEADER, 'Header propio con el token'),
                    (AUTH_NINGUNA, 'Sin autenticación')]

    nombre = models.CharField(max_length=100, help_text='Ej: "CRM cerradores", "Videollamada de validación".')
    url = models.URLField(max_length=500)
    autenticacion = models.CharField(max_length=10, choices=AUTH_CHOICES, default=AUTH_BEARER)
    header_nombre = models.CharField(max_length=60, blank=True, default='X-API-Key',
                                     verbose_name='Nombre del header', help_text='Solo para "Header propio".')
    token = CampoCifrado(blank=True)
    activo = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['nombre']
        verbose_name = 'Webhook saliente'
        verbose_name_plural = 'Webhooks salientes'

    def __str__(self):
        return self.nombre

    def headers(self):
        h = {'Content-Type': 'application/json', 'User-Agent': 'SupregCRM-Webhooks/1.0'}
        if self.token and self.autenticacion == self.AUTH_BEARER:
            h['Authorization'] = f'Bearer {self.token}'
        elif self.token and self.autenticacion == self.AUTH_HEADER and self.header_nombre:
            h[self.header_nombre] = self.token
        return h


class EnvioWebhook(models.Model):
    """Cada envío a un destino: qué se mandó, qué respondió, cuántos intentos."""
    PENDIENTE, OK, ERROR = 'pendiente', 'ok', 'error'
    ESTADO_CHOICES = [(PENDIENTE, 'Pendiente'), (OK, 'Enviado'), (ERROR, 'Falló')]

    destino = models.ForeignKey(WebhookDestino, on_delete=models.CASCADE, related_name='envios')
    oportunidad = models.ForeignKey('crm.Oportunidad', null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    origen = models.CharField(max_length=150, blank=True, help_text='Automatización / derivación del agente / prueba.')
    payload = models.JSONField(default=dict)
    estado = models.CharField(max_length=10, choices=ESTADO_CHOICES, default=PENDIENTE, db_index=True)
    intentos = models.PositiveSmallIntegerField(default=0)
    respuesta_status = models.PositiveSmallIntegerField(null=True, blank=True)
    respuesta = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    enviado_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
