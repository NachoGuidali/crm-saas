"""
Agente IA de ventas: atiende los chats de WhatsApp de las líneas donde está prendido, califica, cotiza con la
tabla de precios, junta datos y deriva al cerrador (CRM externo por webhook). Función extra 'agente_ia'.
"""
import secrets

from django.db import models
from django.utils import timezone

from core.cifrado import CampoCifrado

MODELOS = [
    ('Anthropic (Claude)', [
        ('claude-opus-5-5', 'Claude Opus 5.5 (el más capaz)'),
        ('claude-sonnet-5-5', 'Claude Sonnet 5.5 (equilibrado)'),
        ('claude-haiku-5-5', 'Claude Haiku 5.5 (el más económico)'),
    ]),
    ('OpenAI', [
        ('gpt-6-astra', 'GPT-6 Astra (el más capaz de OpenAI)'),
        ('gpt-6.1-sol', 'GPT-6.1 Sol (equilibrado)'),
        ('gpt-6-luna', 'GPT-6 Luna (el más económico)'),
        ('gpt-5.1', 'GPT-5.1 (deprecado: OpenAI lo apaga el 1/4/2027)'),
    ]),
]
# US$ por millón de tokens: entrada, salida, lectura de caché, escritura de caché
PRECIOS_MODELO = {
    'claude-opus-5-5': (4.00, 20.00, 0.20, 5.00),
    'claude-sonnet-5-5': (2.00, 10.00, 0.10, 2.50),
    'claude-haiku-5-5': (0.10, 0.50, 0.01, 0.125),
    # OpenAI (tarifa estándar, contexto corto; la caché no cobra escritura)
    'gpt-6-astra': (10.00, 50.00, 1.00, 10.00),
    'gpt-6.1-sol': (2.00, 10.00, 0.10, 2.00),
    'gpt-6-luna': (0.10, 0.50, 0.01, 0.10),
    'gpt-5.1': (1.25, 10.00, 0.125, 1.25),
}


def proveedor_de(modelo):
    return 'openai' if (modelo or '').startswith('gpt-') else 'anthropic'
ESFUERZOS = [('low', 'Bajo (más rápido y barato)'), ('medium', 'Medio (recomendado)'), ('high', 'Alto')]

# Datos del contacto que el agente puede completar además de los campos personalizados.
CAMPOS_BASE_AGENTE = [
    ('nombre', 'Nombre y apellido'), ('email', 'Email'), ('dni', 'DNI'),
    ('fecha_nacimiento', 'Fecha de nacimiento'), ('localidad', 'Localidad'), ('provincia', 'Provincia'),
]


class ConfigAgente(models.Model):
    """Una sola fila por cliente."""
    activo = models.BooleanField(default=False, help_text='Interruptor general: apagado, el agente no responde a nadie.')
    nombre = models.CharField(max_length=60, default='Clara', verbose_name='Nombre del agente')
    empresa = models.CharField(max_length=120, blank=True, verbose_name='Empresa / marca que representa')
    modelo = models.CharField(max_length=40, choices=MODELOS, default='claude-opus-5-5')
    esfuerzo = models.CharField(max_length=10, choices=ESFUERZOS, default='medium',
                                help_text='Cuánto "piensa" antes de responder. Más esfuerzo = mejor en objeciones, más caro.')
    instrucciones = models.TextField(
        blank=True, verbose_name='Guion y tono',
        help_text='Cómo habla, cómo saluda, qué ofrece primero, cómo maneja objeciones, qué nunca debe decir.')
    criterios = models.TextField(
        blank=True, verbose_name='Criterios de calificación',
        help_text='Cuándo un lead está listo para pasar al asesor y cuándo descartarlo. Ej: "Tiene recibo de sueldo, '
                  'entre 18 y 64 años, vive en AMBA → derivar".')
    campos_base = models.JSONField(default=list, blank=True, verbose_name='Datos del contacto a completar')
    campos = models.ManyToManyField('crm.CampoPersonalizado', blank=True, related_name='+',
                                    verbose_name='Campos personalizados a completar')

    embudo = models.ForeignKey('crm.Embudo', null=True, blank=True, on_delete=models.SET_NULL, related_name='+',
                               help_text='Embudo donde trabaja el agente (para mover etapas).')
    etapa_calificando = models.ForeignKey('crm.Etapa', null=True, blank=True, on_delete=models.SET_NULL,
                                          related_name='+', verbose_name='Etapa mientras conversa')
    etapa_derivado = models.ForeignKey('crm.Etapa', null=True, blank=True, on_delete=models.SET_NULL,
                                       related_name='+', verbose_name='Etapa al derivar al asesor')
    etapa_descartado = models.ForeignKey('crm.Etapa', null=True, blank=True, on_delete=models.SET_NULL,
                                         related_name='+', verbose_name='Etapa al descartar')
    tipificacion_descartado = models.ForeignKey('crm.Tipificacion', null=True, blank=True, on_delete=models.SET_NULL,
                                                related_name='+', verbose_name='Motivo de cierre al descartar',
                                                help_text='Si la etapa de descarte es un cierre, hace falta un motivo.')

    derivar_url = models.CharField(max_length=500, blank=True, verbose_name='Webhook del CRM externo',
                                   help_text='Al derivar se le manda el lead por POST (JSON). Vacío = solo queda en este CRM.')
    derivar_token = CampoCifrado(blank=True, verbose_name='Token del webhook',
                                 help_text='Se manda como "Authorization: Bearer …".')
    mensaje_derivacion = models.TextField(
        blank=True, default='¡Genial! Ya le pasé tus datos a un asesor, en breve se comunica con vos 🙌',
        verbose_name='Mensaje al derivar', help_text='Lo que el agente le dice al lead cuando lo pasa al asesor.')

    demora_segundos = models.PositiveSmallIntegerField(
        default=12, verbose_name='Espera antes de responder (seg)',
        help_text='Junta los mensajes seguidos del lead antes de contestar, como haría una persona.')
    max_turnos = models.PositiveSmallIntegerField(
        default=40, verbose_name='Máximo de respuestas por chat',
        help_text='Al llegar, pasa el chat a una persona (evita charlas eternas).')
    presupuesto_diario = models.DecimalField(max_digits=8, decimal_places=2, default=20,
                                             verbose_name='Tope de gasto por día (US$)',
                                             help_text='Al llegar, el agente deja de responder hasta el día siguiente.')
    anthropic_api_key = CampoCifrado(blank=True, verbose_name='API key de Anthropic',
                                     help_text='Opcional: la key propia de esta empresa (si el plan lo permite).')
    openai_api_key = CampoCifrado(blank=True, verbose_name='API key de OpenAI',
                                  help_text='Opcional: para modelos GPT y para transcribir audios.')
    precios_columnas = models.JSONField(default=list, blank=True)
    precios_filas = models.JSONField(default=list, blank=True)
    precios_url = models.CharField(max_length=500, blank=True, verbose_name='Link de Google Sheets',
                                   help_text='Compartido como "cualquiera con el link". Se sincroniza cada 30 minutos.')
    precios_actualizado_at = models.DateTimeField(null=True, blank=True)
    precios_error = models.CharField(max_length=300, blank=True)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Configuración del agente IA'

    def __str__(self):
        return f'Agente {self.nombre}'

    @classmethod
    def get(cls):
        obj = cls.objects.first()
        return obj or cls.objects.create()

    @property
    def proveedor(self):
        return proveedor_de(self.modelo)

    @property
    def precios(self):
        return PRECIOS_MODELO.get(self.modelo, PRECIOS_MODELO['claude-opus-5-5'])


class DocumentoAgente(models.Model):
    """Información que el agente conoce (planes, coberturas, requisitos, FAQ, objeciones)."""
    titulo = models.CharField(max_length=150)
    texto = models.TextField(blank=True, help_text='Texto que lee el agente. Si subís un archivo, se completa solo.')
    archivo = models.FileField(upload_to='agente/documentos/', blank=True)
    enviable = models.BooleanField(default=False, verbose_name='El agente puede mandar el archivo al lead',
                                   help_text='Ej: folleto o cartilla en PDF.')
    activo = models.BooleanField(default=True)
    orden = models.PositiveSmallIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['orden', 'titulo']
        verbose_name = 'Documento del agente'

    def __str__(self):
        return self.titulo


class TurnoAgente(models.Model):
    """Cada vez que el agente pensó y respondió: qué hizo y cuánto costó."""
    MODO_RESPUESTA = 'respuesta'
    MODO_INICIO = 'inicio'
    MODO_SEGUIMIENTO = 'seguimiento'
    MODO_CHOICES = [(MODO_RESPUESTA, 'Respuesta'), (MODO_INICIO, 'Primer contacto'), (MODO_SEGUIMIENTO, 'Seguimiento')]

    conversacion = models.ForeignKey('whatsapp.Conversacion', null=True, blank=True, on_delete=models.CASCADE,
                                     related_name='turnos_agente')
    oportunidad = models.ForeignKey('crm.Oportunidad', null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name='turnos_agente')
    modo = models.CharField(max_length=12, choices=MODO_CHOICES, default=MODO_RESPUESTA)
    modelo = models.CharField(max_length=40)
    respuesta = models.TextField(blank=True)
    herramientas = models.JSONField(default=list, blank=True)
    tokens_entrada = models.PositiveIntegerField(default=0)
    tokens_salida = models.PositiveIntegerField(default=0)
    tokens_cache_lectura = models.PositiveIntegerField(default=0)
    tokens_cache_escritura = models.PositiveIntegerField(default=0)
    costo = models.DecimalField(max_digits=10, decimal_places=5, default=0)
    duracion_ms = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True)
    request_id = models.CharField(max_length=100, blank=True)
    simulado = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ['-created_at']


class SeguimientoAgente(models.Model):
    """Recordatorio que se programa el agente ("le escribo mañana si no contesta")."""
    conversacion = models.ForeignKey('whatsapp.Conversacion', on_delete=models.CASCADE, related_name='seguimientos_agente')
    cuando = models.DateTimeField(db_index=True)
    motivo = models.CharField(max_length=300)
    hecho = models.BooleanField(default=False)
    resultado = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['cuando']


def _slug():
    return secrets.token_urlsafe(5).lower().replace('_', '').replace('-', '')[:8]


class EnlaceRotativo(models.Model):
    """Link para anuncios que reparte a los leads entre las líneas activas (si bloquean una, sigue con las otras)."""
    nombre = models.CharField(max_length=100, help_text='Ej: "Campaña Meta noviembre".')
    slug = models.SlugField(max_length=40, unique=True, default=_slug)
    mensaje = models.CharField(max_length=300, blank=True, verbose_name='Mensaje precargado',
                               help_text='Lo que el lead ve escrito al abrir WhatsApp. Ej: "Hola, quiero info de los planes".')
    lineas = models.ManyToManyField('whatsapp.LineaWhatsApp', blank=True, related_name='+',
                                    help_text='Vacío = todas las líneas con el agente prendido.')
    clics = models.PositiveIntegerField(default=0)
    activo = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.nombre
