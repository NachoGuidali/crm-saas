import secrets

from django.conf import settings
from django.core.cache import cache
from django.db import models
from django.urls import reverse
from django.utils import timezone

from core import tenancy
from core.cifrado import CampoCifrado

User = settings.AUTH_USER_MODEL


def _token():
    return secrets.token_urlsafe(24)


class ConfigAnura(models.Model):
    """Configuración única de la integración con Anura."""
    AUTH_BEARER = 'bearer'
    AUTH_BASIC = 'basic'
    AUTH_HEADER = 'header'
    AUTH_CHOICES = [(AUTH_BEARER, 'Bearer token'), (AUTH_BASIC, 'Usuario y contraseña (Basic)'),
                    (AUTH_HEADER, 'Header personalizado')]

    activo = models.BooleanField(default=False, verbose_name='Integración activa')
    modo_demo = models.BooleanField(
        default=True, verbose_name='Modo demostración',
        help_text='Simula las llamadas sin conectarse a Anura (para capacitación y presentaciones).',
    )
    click2dial_url = models.CharField(max_length=300, default='https://api.anura.com.ar/adapter/default/click2call',
                                      verbose_name='URL de Click2Dial',
                                      help_text='Argentina: api.anura.com.ar · Perú: api.anura.pe')
    click2dial_token = CampoCifrado(blank=True, verbose_name='Token de Click2Dial',
                                        help_text='Se genera en el panel de Anura: Integraciones → Click 2 Dial.')
    webphone_url = models.CharField(
        max_length=300, blank=True, verbose_name='Dirección del Anura web de los agentes',
        help_text='La que usan los agentes para atender y cortar en el navegador. Muestra el botón "Ir a Anura" '
                  'en el aviso de llamada. Vacío = sin botón.',
    )
    api_url = models.CharField(max_length=300, blank=True, verbose_name='URL de la API de tenant (opcional)',
                               help_text='Solo si Anura habilita la API de tenant (CDRs, números bloqueados).')
    auth_tipo = models.CharField(max_length=10, choices=AUTH_CHOICES, default=AUTH_BEARER)
    api_token = CampoCifrado(blank=True)
    api_usuario = models.CharField(max_length=150, blank=True)
    api_password = CampoCifrado(blank=True)
    auth_header_nombre = models.CharField(max_length=60, blank=True, default='X-Api-Key')
    tenant_id = models.CharField(max_length=100, blank=True)
    account_id = models.CharField(max_length=100, blank=True, verbose_name='Account ID')
    webhook_token = models.CharField(max_length=64, default=_token, editable=False)

    embudo_entrantes = models.ForeignKey(
        'crm.Embudo', null=True, blank=True, on_delete=models.SET_NULL, related_name='+',
        verbose_name='Embudo para llamadas de números desconocidos',
        help_text='Si el DID/cola no tiene un embudo mapeado, las llamadas nuevas entran acá.',
    )
    crear_tarjeta_salientes = models.BooleanField(
        default=True, verbose_name='Crear tarjeta en llamadas salientes a números nuevos',
        help_text='Si un agente llama desde el softphone a un número que no está en el CRM, se crea la oportunidad.',
    )
    solo_eventos_propios = models.BooleanField(
        default=False, verbose_name='Procesar solo llamadas de internos o rutas del CRM',
        help_text='Ignora las llamadas de la central que no pasan por un interno asociado a un usuario, una ruta '
                  '(DID/cola) cargada acá o un "Llamar" del CRM. Útil si la cuenta de Anura se comparte con otras áreas.',
    )
    tarea_llamada_perdida = models.BooleanField(default=True, verbose_name='Crear tarea "Devolver llamada" si no se atiende')
    descargar_grabaciones = models.BooleanField(default=True)
    polling_activo = models.BooleanField(default=False, verbose_name='Polling de respaldo de CDRs',
                                         help_text='Requiere la API de tenant. Cada 5 min recupera llamadas cuyo evento no llegó.')
    ultimo_polling_ok = models.DateTimeField(null=True, blank=True)

    # Teléfono dentro del CRM (terminal SIP por WebSocket con JsSIP)
    softphone_activo = models.BooleanField(
        default=False, verbose_name='Teléfono dentro del CRM',
        help_text='Los agentes con credenciales SIP cargadas atienden, llaman y cortan desde el CRM, sin abrir Anura. '
                  'La terminal "CRM" de cada interno tiene que ser la principal.')
    softphone_ws = models.CharField(max_length=200, default='wss://sipwss.anura.dev', verbose_name='Servidor WebSocket')
    sip_dominio = models.CharField(max_length=200, blank=True, verbose_name='Dominio de la central',
                                   help_text='Ej.: miempresa.grancentral.com.ar')
    sip_puerto = models.PositiveIntegerField(null=True, blank=True, default=55090, verbose_name='Puerto de la central')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Configuración Anura'

    def __str__(self):
        return 'Configuración Anura'

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)
        cache.delete('config_anura')

    @classmethod
    def get(cls):
        obj = cache.get('config_anura')
        if obj is None:
            obj, _ = cls.objects.get_or_create(pk=1)
            cache.set('config_anura', obj, 120)
        return obj

    @property
    def webhook_url(self):
        return tenancy.url_sitio() + reverse('telefonia:webhook', args=[self.webhook_token])

    @property
    def operativa(self):
        return self.activo and (self.modo_demo or bool(self.click2dial_token))

    @property
    def puede_cortar(self):
        """Anura no expone el corte por API; en modo demo se simula."""
        return self.modo_demo


class InternoAnura(models.Model):
    """Mapeo usuario del CRM → terminal/interno en Anura (necesario para click2call y discador)."""
    usuario = models.OneToOneField(User, on_delete=models.CASCADE, related_name='interno_anura')
    interno = models.CharField(max_length=50, unique=True, verbose_name='Interno / terminal',
                               help_text='Identificador que se usa para originar y cortar llamadas en Anura.')
    alias = models.CharField(
        max_length=200, blank=True, verbose_name='Alias en Anura',
        help_text='Otros identificadores con los que Anura puede nombrar este teléfono en los avisos '
                  '(usuario SIP, ID de terminal…). Varios separados por coma.',
    )
    activo = models.BooleanField(default=True)
    sip_usuario = models.CharField(max_length=100, blank=True, verbose_name='Usuario SIP de la terminal CRM')
    sip_password_cifrada = models.CharField(max_length=500, blank=True, editable=False)

    class Meta:
        verbose_name = 'Interno de Anura'
        verbose_name_plural = 'Internos de Anura'
        ordering = ['interno']

    def __str__(self):
        return f'{self.interno} → {self.usuario}'

    @staticmethod
    def limpiar_alias(texto):
        vistos = []
        for a in (texto or '').split(','):
            a = a.strip()
            if a and a not in vistos:
                vistos.append(a)
        return vistos

    @property
    def identificadores(self):
        return [self.interno] + [a for a in self.limpiar_alias(self.alias) if a != self.interno]

    def save(self, *args, **kwargs):
        self.interno = self.interno.strip()
        self.alias = ', '.join(self.limpiar_alias(self.alias))
        super().save(*args, **kwargs)
        cache.delete('anura_mapa_identificadores')

    def delete(self, *args, **kwargs):
        super().delete(*args, **kwargs)
        cache.delete('anura_mapa_identificadores')

    @classmethod
    def mapa_identificadores(cls):
        """{identificador (interno o alias): usuario_id} de los internos activos. Cacheado."""
        mapa = cache.get('anura_mapa_identificadores')
        if mapa is None:
            mapa = {}
            for i in cls.objects.filter(activo=True):
                for ident in i.identificadores:
                    mapa.setdefault(ident, i.usuario_id)
            cache.set('anura_mapa_identificadores', mapa, 300)
        return mapa

    @classmethod
    def conflicto(cls, identificadores, usuario):
        """Devuelve (identificador, InternoAnura de otro usuario) si alguno ya está en uso, o None."""
        for i in cls.objects.exclude(usuario=usuario):
            repetidos = set(identificadores) & set(i.identificadores)
            if repetidos:
                return sorted(repetidos)[0], i
        return None

    @property
    def sip_password(self):
        from core.cifrado import descifrar
        return descifrar(self.sip_password_cifrada)

    @sip_password.setter
    def sip_password(self, valor):
        from core.cifrado import cifrar
        self.sip_password_cifrada = cifrar(valor)

    @property
    def tiene_softphone(self):
        return bool(self.activo and self.sip_usuario and self.sip_password_cifrada)


class RutaAnura(models.Model):
    """Mapeo DID / cola de Anura → embudo (y opcionalmente agente) para las llamadas entrantes."""
    TIPO_DID = 'did'
    TIPO_COLA = 'cola'
    TIPO_CHOICES = [(TIPO_DID, 'Número marcado (DID)'), (TIPO_COLA, 'Cola (queueId)')]

    tipo = models.CharField(max_length=5, choices=TIPO_CHOICES, default=TIPO_DID)
    valor = models.CharField(max_length=60, help_text='El DID tal como lo manda Anura (campo "called") o el queueId.')
    descripcion = models.CharField(max_length=150, blank=True)
    embudo = models.ForeignKey('crm.Embudo', on_delete=models.CASCADE, related_name='rutas_anura')
    agente = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name='+',
                               help_text='Opcional: asignar siempre a este agente.')

    class Meta:
        unique_together = [('tipo', 'valor')]
        verbose_name = 'Ruta de Anura'
        verbose_name_plural = 'Rutas de Anura'

    def __str__(self):
        return f'{self.get_tipo_display()} {self.valor} → {self.embudo}'


class Llamada(models.Model):
    DIR_ENTRANTE = 'IN'
    DIR_SALIENTE = 'OUT'
    DIR_CHOICES = [(DIR_ENTRANTE, 'Entrante'), (DIR_SALIENTE, 'Saliente')]

    ESTADO_DISCANDO = 'discando'
    ESTADO_SONANDO = 'sonando'
    ESTADO_EN_CURSO = 'en_curso'
    ESTADO_ATENDIDA = 'atendida'
    ESTADO_NO_ATENDIDA = 'no_atendida'
    ESTADO_OCUPADO = 'ocupado'
    ESTADO_FALLIDA = 'fallida'
    ESTADO_CHOICES = [
        (ESTADO_DISCANDO, 'Discando'), (ESTADO_SONANDO, 'Sonando'), (ESTADO_EN_CURSO, 'En curso'),
        (ESTADO_ATENDIDA, 'Atendida'), (ESTADO_NO_ATENDIDA, 'No atendida'), (ESTADO_OCUPADO, 'Ocupado'),
        (ESTADO_FALLIDA, 'Fallida'),
    ]
    ESTADOS_VIVOS = (ESTADO_DISCANDO, ESTADO_SONANDO, ESTADO_EN_CURSO)

    GRAB_NO = ''
    GRAB_PENDIENTE = 'pendiente'
    GRAB_OK = 'ok'
    GRAB_ERROR = 'error'

    call_id = models.CharField(max_length=100, unique=True, null=True, blank=True,
                               help_text='callId de Anura: garantiza que una llamada no se procese dos veces.')
    anura_uuid = models.CharField(max_length=100, blank=True, db_index=True)
    direccion = models.CharField(max_length=3, choices=DIR_CHOICES)
    estado = models.CharField(max_length=12, choices=ESTADO_CHOICES, default=ESTADO_DISCANDO, db_index=True)
    numero = models.CharField(max_length=20, blank=True, db_index=True, help_text='Número del cliente, normalizado')
    numero_crudo = models.CharField(max_length=60, blank=True)
    did = models.CharField(max_length=60, blank=True, help_text='Número/servicio marcado (called)')
    queue_id = models.CharField(max_length=60, blank=True)
    interno = models.CharField(max_length=50, blank=True)
    agente = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name='llamadas')
    contacto = models.ForeignKey('crm.Contacto', null=True, blank=True, on_delete=models.SET_NULL, related_name='llamadas')
    oportunidad = models.ForeignKey('crm.Oportunidad', null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name='llamadas')
    campania_contacto = models.ForeignKey('CampaniaContacto', null=True, blank=True, on_delete=models.SET_NULL,
                                          related_name='llamadas')
    inicio_at = models.DateTimeField(default=timezone.now, db_index=True)
    atendida_at = models.DateTimeField(null=True, blank=True)
    fin_at = models.DateTimeField(null=True, blank=True)
    duracion_seg = models.PositiveIntegerField(default=0, help_text='Segundos facturados (billSeconds)')
    grabada = models.BooleanField(default=False)
    grabacion = models.FileField(upload_to='grabaciones/%Y/%m/', blank=True)
    grabacion_estado = models.CharField(max_length=10, blank=True)
    origen_registro = models.CharField(max_length=15, blank=True,
                                       help_text='webhook / polling / click2call / discador / demo')
    procesada = models.BooleanField(default=False, help_text='Se aplicó la lógica de fin de llamada.')
    # Resultado de la gestión (lo carga el agente al cortar)
    resultado = models.ForeignKey('crm.ResultadoGestion', null=True, blank=True, on_delete=models.PROTECT,
                                  related_name='llamadas')
    resultado_nota = models.TextField(blank=True)
    calificada_at = models.DateTimeField(null=True, blank=True)
    calificada_por = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-inicio_at']
        indexes = [models.Index(fields=['agente', 'estado']), models.Index(fields=['contacto', '-inicio_at'])]

    @property
    def id_grabacion(self):
        """Identificador de la llamada/grabación en Anura (cdrid), para buscarla allá."""
        return self.call_id or self.anura_uuid

    def __str__(self):
        return f'{self.get_direccion_display()} {self.numero} ({self.get_estado_display()})'

    @property
    def viva(self):
        return self.estado in self.ESTADOS_VIVOS

    @property
    def icono(self):
        if self.estado in (self.ESTADO_NO_ATENDIDA, self.ESTADO_OCUPADO, self.ESTADO_FALLIDA):
            return 'telephone-x'
        return 'telephone-inbound' if self.direccion == self.DIR_ENTRANTE else 'telephone-outbound'


class CampaniaDiscado(models.Model):
    """Discador progresivo: una llamada por agente libre, en secuencia (Anura no expone discado predictivo)."""
    ESTADO_BORRADOR = 'borrador'
    ESTADO_ACTIVA = 'activa'
    ESTADO_PAUSADA = 'pausada'
    ESTADO_FINALIZADA = 'finalizada'
    ESTADO_CHOICES = [(ESTADO_BORRADOR, 'Borrador'), (ESTADO_ACTIVA, 'Activa'), (ESTADO_PAUSADA, 'Pausada'),
                      (ESTADO_FINALIZADA, 'Finalizada')]

    nombre = models.CharField(max_length=120)
    embudo = models.ForeignKey('crm.Embudo', null=True, blank=True, on_delete=models.SET_NULL, related_name='campanias')
    estado = models.CharField(max_length=10, choices=ESTADO_CHOICES, default=ESTADO_BORRADOR)
    agentes = models.ManyToManyField(User, blank=True, related_name='campanias_discado')
    max_intentos = models.PositiveSmallIntegerField(default=3, verbose_name='Máximo de intentos por contacto')
    minutos_entre_intentos = models.PositiveIntegerField(default=120, verbose_name='Minutos entre reintentos')
    hora_desde = models.TimeField(default='09:00')
    hora_hasta = models.TimeField(default='20:00')
    dias = models.JSONField(default=list, blank=True, help_text='0=lunes … 6=domingo. Vacío = lunes a viernes.')
    segundos_entre_llamadas = models.PositiveSmallIntegerField(
        default=15, verbose_name='Pausa del agente entre llamadas (s)',
        help_text='Tiempo para tipificar antes de que el discador le pase la próxima llamada.')
    creada_por = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Campaña de discado'
        verbose_name_plural = 'Campañas de discado'

    def __str__(self):
        return self.nombre

    def en_horario(self, momento=None):
        momento = timezone.localtime(momento or timezone.now())
        dias = self.dias or [0, 1, 2, 3, 4]
        desde, hasta = self.hora_desde, self.hora_hasta
        if isinstance(desde, str):
            from datetime import time
            desde = time(*map(int, desde.split(':')[:2]))
            hasta = time(*map(int, hasta.split(':')[:2]))
        return momento.weekday() in dias and desde <= momento.time() <= hasta

    def resumen(self):
        from django.db.models import Count
        data = dict(self.contactos.values_list('estado').annotate(n=Count('pk')).values_list('estado', 'n'))
        data['total'] = sum(data.values())
        return data


class CampaniaContacto(models.Model):
    ESTADO_PENDIENTE = 'pendiente'
    ESTADO_EN_CURSO = 'en_curso'
    ESTADO_REINTENTAR = 'reintentar'
    ESTADO_CONTACTADO = 'contactado'
    ESTADO_NO_CONTESTA = 'no_contesta'
    ESTADO_DESCARTADO = 'descartado'
    ESTADO_CHOICES = [
        (ESTADO_PENDIENTE, 'Pendiente'), (ESTADO_EN_CURSO, 'En curso'), (ESTADO_REINTENTAR, 'A reintentar'),
        (ESTADO_CONTACTADO, 'Contactado'), (ESTADO_NO_CONTESTA, 'No contesta (agotó intentos)'),
        (ESTADO_DESCARTADO, 'Descartado'),
    ]

    campania = models.ForeignKey(CampaniaDiscado, on_delete=models.CASCADE, related_name='contactos')
    contacto = models.ForeignKey('crm.Contacto', null=True, blank=True, on_delete=models.CASCADE, related_name='+')
    oportunidad = models.ForeignKey('crm.Oportunidad', null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name='campanias_discado')
    telefono = models.CharField(max_length=20)
    estado = models.CharField(max_length=12, choices=ESTADO_CHOICES, default=ESTADO_PENDIENTE)
    intentos = models.PositiveSmallIntegerField(default=0)
    ultimo_intento_at = models.DateTimeField(null=True, blank=True)
    proximo_intento_at = models.DateTimeField(null=True, blank=True)
    agente = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    prioridad = models.SmallIntegerField(default=0)
    detalle = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-prioridad', 'pk']
        unique_together = [('campania', 'telefono')]
        indexes = [models.Index(fields=['campania', 'estado', 'proximo_intento_at'])]


class AgenteDiscador(models.Model):
    """Agente conectado al discador de una campaña (solo se le disca si está conectado y no en pausa)."""
    agente = models.OneToOneField(User, on_delete=models.CASCADE, related_name='sesion_discador')
    campania = models.ForeignKey(CampaniaDiscado, on_delete=models.CASCADE, related_name='sesiones')
    en_pausa = models.BooleanField(default=False)
    libre_desde = models.DateTimeField(default=timezone.now)
    conectado_at = models.DateTimeField(default=timezone.now)


class EventoAnura(models.Model):
    """
    Bitácora de diagnóstico: cada aviso (webhook/demo/polling) que recibe el CRM, con su resultado
    ("registrada / ignorada y por qué"). Herramienta clave para saber si un aviso llegó, con qué nombre vino
    el agente y por qué se ignoró. Se poda a los últimos MAX_EVENTOS.
    """
    MAX_EVENTOS = 500

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    evento = models.CharField(max_length=20, blank=True)       # START / TALK / END
    direccion = models.CharField(max_length=4, blank=True)     # IN / OUT
    call_id = models.CharField(max_length=100, blank=True, db_index=True)
    de = models.CharField(max_length=60, blank=True)           # calling
    a = models.CharField(max_length=60, blank=True)            # called
    origen = models.CharField(max_length=12, default='webhook')  # webhook / demo / polling / softphone
    resultado = models.CharField(max_length=200, blank=True)
    llamada = models.ForeignKey('Llamada', null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    payload = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Aviso de Anura'
        verbose_name_plural = 'Avisos de Anura'

    def __str__(self):
        return f'{self.created_at:%d/%m %H:%M} {self.evento} {self.direccion} {self.resultado}'


class PausaTelefono(models.Model):
    """Tramo en que un agente puso el teléfono en 'No disponible': la cola no le ofrece llamadas.
    `visto_at` avanza con cada latido del teléfono; si se cierra el navegador en pausa, el tramo deja de crecer."""
    agente = models.ForeignKey(User, on_delete=models.CASCADE, related_name='pausas_telefono')
    inicio_at = models.DateTimeField(default=timezone.now, db_index=True)
    fin_at = models.DateTimeField(null=True, blank=True)
    visto_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-inicio_at']

    @property
    def abierta(self):
        return self.fin_at is None

    def segundos(self, desde=None):
        fin = self.fin_at or self.visto_at
        ini = max(self.inicio_at, desde) if desde else self.inicio_at
        return max(0, int((fin - ini).total_seconds()))
