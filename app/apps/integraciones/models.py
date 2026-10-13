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
