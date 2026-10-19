from django.db import models

from core.cifrado import CampoCifrado


class AccionEtapa(models.Model):
    """
    'Cuando una oportunidad entra a la etapa X → hacer Y' (opcionalmente con demora).
    Cubre los mensajes automáticos del circuito: bienvenida, seguimiento, confirmación y despedida.
    """
    TIPO_WHATSAPP = 'whatsapp'
    TIPO_EMAIL = 'email'
    TIPO_TAREA = 'tarea'
    TIPO_NOTIF_AGENTE = 'notif_agente'
    TIPO_NOTIF_SUPERVISORES = 'notif_supervisores'
    TIPO_EMBUDO = 'embudo'
    TIPO_ETAPA = 'etapa'
    TIPO_SMS = 'sms'
    TIPO_REASIGNAR = 'reasignar'
    TIPO_PRIORIDAD = 'prioridad'
    TIPO_ETIQUETA = 'etiqueta'
    DISP_ENTRADA = 'entrada'
    DISP_SIN_RESPUESTA = 'sin_respuesta'
    DISP_RESPUESTA = 'respuesta'
    DISP_SIN_ACTIVIDAD = 'sin_actividad'
    DISP_DESPUES_DE = 'despues_de'
    DISP_REINGRESO = 'reingreso'
    DISP_CAMPO = 'campo'
    DISPARADORES = [
        (DISP_ENTRADA, 'Al entrar a la etapa'),
        (DISP_SIN_RESPUESTA, 'Si el cliente no responde (en el tiempo de demora desde que entró a la etapa)'),
        (DISP_RESPUESTA, 'Cuando el cliente responde (WhatsApp o llamada atendida) estando en la etapa'),
        (DISP_SIN_ACTIVIDAD, 'Si no hay ninguna actividad durante el tiempo de demora'),
        (DISP_DESPUES_DE, 'Después de que se ejecutó otra automatización (secuencia)'),
        (DISP_REINGRESO, 'Cuando el lead reingresa (vuelve a escribir, llenar un formulario o se importa de nuevo)'),
        (DISP_CAMPO, 'Cuando se completa o cambia un campo del contacto'),
    ]
    TIPO_CHOICES = [
        (TIPO_WHATSAPP, 'Enviar WhatsApp al prospecto'),
        (TIPO_EMAIL, 'Enviar email al prospecto'),
        (TIPO_SMS, 'Enviar SMS al prospecto'),
        (TIPO_TAREA, 'Crear tarea para el agente'),
        (TIPO_NOTIF_AGENTE, 'Notificar al agente'),
        (TIPO_NOTIF_SUPERVISORES, 'Notificar a supervisión'),
        (TIPO_EMBUDO, 'Pasar a otro embudo'),
        (TIPO_ETAPA, 'Mover a otra etapa / cerrar'),
        (TIPO_REASIGNAR, 'Reasignar a otro vendedor (reproceso)'),
        (TIPO_PRIORIDAD, 'Marcar como prioridad'),
        (TIPO_ETIQUETA, 'Poner una etiqueta al contacto'),
    ]
    REPARTO_EMBUDO = 'embudo'
    REPARTO_MENOR_CARGA = 'menor_carga'
    REPARTO_CHOICES = [(REPARTO_MENOR_CARGA, 'Al que tenga menos prospectos abiertos (parejo)'),
                       (REPARTO_EMBUDO, 'Según la regla del embudo (rotativa / menor carga)')]
    MODO_CREAR = 'crear'
    MODO_MOVER = 'mover'
    MODO_VOLVER = 'volver'
    MODO_CHOICES = [
        (MODO_CREAR, 'Crear una oportunidad nueva en otro embudo (la actual queda como está)'),
        (MODO_MOVER, 'Mover esta misma tarjeta a otro embudo'),
        (MODO_VOLVER, 'Volver al embudo del que vino'),
    ]
    VOLVER_MISMA = 'misma'
    VOLVER_SIGUIENTE = 'siguiente'
    VOLVER_CHOICES = [(VOLVER_MISMA, 'A la etapa donde estaba'), (VOLVER_SIGUIENTE, 'A la etapa siguiente')]
    ASIGNAR_CHOICES = [('mismo', 'Al mismo agente'), ('embudo', 'Según la regla del embudo de destino'),
                       ('usuario', 'A un usuario fijo')]
    ICONOS = {TIPO_WHATSAPP: 'whatsapp', TIPO_EMAIL: 'envelope', TIPO_TAREA: 'calendar-plus',
              TIPO_NOTIF_AGENTE: 'bell', TIPO_NOTIF_SUPERVISORES: 'megaphone', TIPO_EMBUDO: 'signpost-split', TIPO_ETAPA: 'arrow-right-circle', TIPO_SMS: 'phone',
              TIPO_REASIGNAR: 'arrow-left-right', TIPO_PRIORIDAD: 'fire', TIPO_ETIQUETA: 'tag'}

    embudo = models.ForeignKey('crm.Embudo', on_delete=models.CASCADE, related_name='acciones')
    etapa = models.ForeignKey('crm.Etapa', on_delete=models.CASCADE, related_name='acciones',
                              verbose_name='Cuando entra a la etapa')
    otras_etapas = models.ManyToManyField('crm.Etapa', blank=True, related_name='+',
                                          verbose_name='También en estas etapas')
    todas_las_etapas = models.BooleanField(default=False, verbose_name='En todas las etapas abiertas del embudo')
    nombre = models.CharField(max_length=120)
    disparador = models.CharField(max_length=15, choices=DISPARADORES, default=DISP_ENTRADA, verbose_name='Cuándo')
    tipo = models.CharField(max_length=20, choices=TIPO_CHOICES, default=TIPO_WHATSAPP)
    activa = models.BooleanField(default=True)
    demora_minutos = models.PositiveIntegerField(default=0, verbose_name='Demora (minutos)',
                                                 help_text='0 = inmediato. Ej: 1440 = al día siguiente.')
    solo_si_sigue_en_etapa = models.BooleanField(
        default=True, verbose_name='Solo si sigue en esa etapa al momento de ejecutar',
        help_text='Evita mandar un seguimiento si el prospecto ya avanzó o se cerró.',
    )
    solo_en_horario = models.BooleanField(
        default=True, verbose_name='Ejecutar solo en el horario de atención del embudo',
        help_text='Si cae fuera de horario se posterga hasta la próxima apertura (no se escribe de madrugada).',
    )

    # WhatsApp
    plantilla = models.ForeignKey('whatsapp.Plantilla', null=True, blank=True, on_delete=models.SET_NULL,
                                  related_name='+', help_text='Obligatoria para líneas oficiales (Meta/Twilio).')
    texto = models.TextField(blank=True, help_text='Mensaje libre (líneas Evolution / email). '
                                                   'Variables: {nombre} {primer_nombre} {agente} {embudo} {etapa}')
    linea = models.ForeignKey('whatsapp.LineaWhatsApp', null=True, blank=True, on_delete=models.SET_NULL,
                              related_name='+', help_text='Vacío = la del chat existente o la del embudo.')
    accion_previa = models.ForeignKey('self', null=True, blank=True, on_delete=models.CASCADE, related_name='siguientes',
                                      verbose_name='Después de la automatización')
    # Email
    plantilla_email = models.ForeignKey('PlantillaEmail', null=True, blank=True, on_delete=models.SET_NULL,
                                        related_name='+', verbose_name='Plantilla de email')
    email_asunto = models.CharField(max_length=200, blank=True)
    # Tarea
    tarea_titulo = models.CharField(max_length=200, blank=True)
    tarea_vence_horas = models.PositiveSmallIntegerField(default=24)

    # Mover a otra etapa / cerrar (mismo embudo)
    mover_a = models.ForeignKey('crm.Etapa', null=True, blank=True, on_delete=models.SET_NULL, related_name='+',
                                verbose_name='Mover a la etapa')
    tipificacion = models.ForeignKey('crm.Tipificacion', null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name='+', verbose_name='Tipificación (si cierra)')
    # Pasar a otro embudo
    modo_embudo = models.CharField(max_length=10, choices=MODO_CHOICES, default=MODO_CREAR, verbose_name='Cómo')
    embudo_destino = models.ForeignKey('crm.Embudo', null=True, blank=True, on_delete=models.SET_NULL,
                                       related_name='+', verbose_name='Embudo de destino')
    etapa_destino = models.ForeignKey('crm.Etapa', null=True, blank=True, on_delete=models.SET_NULL, related_name='+',
                                      verbose_name='Etapa de destino', help_text='Vacío = la primera etapa.')
    volver_a = models.CharField(max_length=10, choices=VOLVER_CHOICES, default=VOLVER_MISMA,
                                verbose_name='Al volver, ir')
    asignar_destino = models.CharField(max_length=10, choices=ASIGNAR_CHOICES, default='mismo',
                                       verbose_name='Asignar')
    usuario_destino = models.ForeignKey('users.User', null=True, blank=True, on_delete=models.SET_NULL,
                                        related_name='+', verbose_name='Usuario')

    # Condiciones: solo para ciertas campañas / etiquetas
    solo_pautas = models.ManyToManyField('pautas.Pauta', blank=True, related_name='+',
                                         verbose_name='Solo leads de estas pautas / campañas')
    solo_etiquetas = models.ManyToManyField('crm.Etiqueta', blank=True, related_name='+',
                                            verbose_name='Solo si el contacto tiene alguna de estas etiquetas')
    excluir_etiquetas = models.ManyToManyField('crm.Etiqueta', blank=True, related_name='+',
                                               verbose_name='Nunca si tiene alguna de estas etiquetas')

    # Reasignar (reproceso)
    reparto = models.CharField(max_length=12, choices=REPARTO_CHOICES, default=REPARTO_MENOR_CARGA,
                               verbose_name='Repartir')
    volver_a_etapa = models.ForeignKey('crm.Etapa', null=True, blank=True, on_delete=models.SET_NULL, related_name='+',
                                       verbose_name='Y volverla a la etapa',
                                       help_text='Opcional (ej. "Nuevo"), para que se trabaje de cero.')
    # Etiqueta
    etiqueta = models.ForeignKey('crm.Etiqueta', null=True, blank=True, on_delete=models.SET_NULL, related_name='+')

    orden = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['embudo', 'etapa__orden', 'orden', 'pk']
        verbose_name = 'Automatización'
        verbose_name_plural = 'Automatizaciones'

    def __str__(self):
        return self.nombre

    @property
    def icono(self):
        return self.ICONOS.get(self.tipo, 'lightning')

    def ids_etapas(self):
        """Etapas donde aplica: la principal, las adicionales o todas las abiertas del embudo."""
        from apps.crm.models import Etapa
        if self.todas_las_etapas:
            return list(Etapa.objects.filter(embudo_id=self.embudo_id, tipo=Etapa.TIPO_NORMAL).values_list('pk', flat=True))
        return [self.etapa_id] + [e.pk for e in self.otras_etapas.all()]

    def aplica_a(self, etapa_id):
        return etapa_id in self.ids_etapas()

    @classmethod
    def q_etapa(cls, etapa_id, embudo_id):
        from django.db.models import Q
        from apps.crm.models import Etapa
        normal = Etapa.objects.filter(pk=etapa_id, tipo=Etapa.TIPO_NORMAL).exists()
        q = Q(etapa_id=etapa_id) | Q(otras_etapas__id=etapa_id)
        return q | Q(todas_las_etapas=True, embudo_id=embudo_id) if normal else q

    def condiciones_ok(self, op):
        """(cumple, motivo) según las condiciones de pauta, etiquetas y campos del contacto."""
        pautas = {p.pk for p in self.solo_pautas.all()}
        if pautas and op.pauta_id not in pautas:
            return False, 'El lead no es de las pautas elegidas.'
        etiquetas = set(op.contacto.etiquetas.values_list('pk', flat=True))
        solo = {e.pk for e in self.solo_etiquetas.all()}
        if solo and not (solo & etiquetas):
            return False, 'El contacto no tiene las etiquetas requeridas.'
        if {e.pk for e in self.excluir_etiquetas.all()} & etiquetas:
            return False, 'El contacto tiene una etiqueta excluida.'
        for c in self.condiciones_campo.all():
            if not c.cumple(op.contacto):
                return False, f'No cumple la condición del campo "{c.campo}".'
        return True, ''

    def resumen_condiciones(self):
        partes = []
        for titulo, rel in (('pautas', self.solo_pautas), ('con etiqueta', self.solo_etiquetas),
                            ('sin etiqueta', self.excluir_etiquetas)):
            nombres = [str(x) for x in rel.all()]
            if nombres:
                partes.append(f'{titulo}: {", ".join(nombres)}')
        campos = [str(c) for c in self.condiciones_campo.all()]
        if campos:
            partes.append('si ' + ' y '.join(campos))
        return ' · '.join(partes)

    def resumen_etapas(self):
        if self.todas_las_etapas:
            return 'todas las etapas'
        otras = [e.nombre for e in self.otras_etapas.all()]
        return ', '.join([str(self.etapa)] + otras)

    def resumen_embudo(self):
        if self.tipo == self.TIPO_REASIGNAR:
            return 'Reasigna a otro vendedor' + (f' y vuelve a {self.volver_a_etapa}' if self.volver_a_etapa_id else '')
        if self.tipo == self.TIPO_ETIQUETA:
            return f'Etiqueta «{self.etiqueta}»'
        if self.tipo == self.TIPO_ETAPA:
            return f'Pasa a {self.mover_a}' + (f' ({self.tipificacion})' if self.tipificacion_id else '')
        if self.tipo != self.TIPO_EMBUDO:
            return ''
        if self.modo_embudo == self.MODO_VOLVER:
            return f'Vuelve al embudo de origen ({self.get_volver_a_display().lower()})'
        destino = f'{self.embudo_destino or "?"}' + (f' · {self.etapa_destino}' if self.etapa_destino_id else '')
        return ('Nueva oportunidad en ' if self.modo_embudo == self.MODO_CREAR else 'Pasa a ') + destino

    def demora_display(self):
        m = self.demora_minutos
        if not m:
            return 'Inmediato'
        if m % 1440 == 0:
            return f'{m // 1440} día{"s" if m // 1440 > 1 else ""} después'
        if m % 60 == 0:
            return f'{m // 60} h después'
        return f'{m} min después'


class CondicionCampo(models.Model):
    """
    Condición sobre un campo personalizado del contacto (ej. "es socio = Sí"). Varias en la misma automatización
    se combinan con Y: tienen que cumplirse todas. Se usan como filtro de cualquier automatización y, en especial,
    con el disparador "Cuando cambia un campo".
    """
    OP_IGUAL = '='
    OP_DISTINTO = '!='
    OP_CONTIENE = 'contiene'
    OP_LLENO = 'lleno'
    OP_VACIO = 'vacio'
    OPERADORES = [
        (OP_IGUAL, 'es igual a'),
        (OP_DISTINTO, 'es distinto de'),
        (OP_CONTIENE, 'contiene'),
        (OP_LLENO, 'tiene algún valor'),
        (OP_VACIO, 'está vacío'),
    ]
    SIN_VALOR = (OP_LLENO, OP_VACIO)

    accion = models.ForeignKey(AccionEtapa, on_delete=models.CASCADE, related_name='condiciones_campo')
    campo = models.ForeignKey('crm.CampoPersonalizado', on_delete=models.CASCADE, related_name='+')
    operador = models.CharField(max_length=10, choices=OPERADORES, default=OP_IGUAL)
    valor = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ['pk']

    @staticmethod
    def _norm(v):
        """Normaliza para comparar: Sí/No a 'true'/'false', el resto a texto en minúsculas."""
        if isinstance(v, bool):
            return 'true' if v else 'false'
        s = str(v).strip().lower() if v is not None else ''
        return {'sí': 'true', 'si': 'true', 'yes': 'true', '1': 'true',
                'no': 'false', '0': 'false'}.get(s, s)

    def cumple(self, contacto):
        actual = (contacto.datos_extra or {}).get(self.campo.slug)
        vacio = actual in (None, '', [], {})
        if self.operador == self.OP_VACIO:
            return vacio
        if self.operador == self.OP_LLENO:
            return not vacio
        a, b = self._norm(actual), self._norm(self.valor)
        if self.operador == self.OP_IGUAL:
            return a == b
        if self.operador == self.OP_DISTINTO:
            return a != b
        if self.operador == self.OP_CONTIENE:
            return bool(b) and b in a
        return False

    def __str__(self):
        etq = dict(self.OPERADORES).get(self.operador, self.operador)
        return f'{self.campo} {etq}' + ('' if self.operador in self.SIN_VALOR else f' {self.valor}')


class EjecucionAccion(models.Model):
    ESTADO_PROGRAMADA = 'programada'
    ESTADO_EJECUTADA = 'ejecutada'
    ESTADO_OMITIDA = 'omitida'
    ESTADO_ERROR = 'error'
    ESTADO_CHOICES = [(ESTADO_PROGRAMADA, 'Programada'), (ESTADO_EJECUTADA, 'Ejecutada'),
                      (ESTADO_OMITIDA, 'Omitida'), (ESTADO_ERROR, 'Error')]

    accion = models.ForeignKey(AccionEtapa, on_delete=models.CASCADE, related_name='ejecuciones')
    oportunidad = models.ForeignKey('crm.Oportunidad', on_delete=models.CASCADE, related_name='ejecuciones')
    historial = models.ForeignKey('crm.HistorialEtapa', null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    estado = models.CharField(max_length=12, choices=ESTADO_CHOICES, default=ESTADO_PROGRAMADA, db_index=True)
    programada_para = models.DateTimeField()
    ejecutada_at = models.DateTimeField(null=True, blank=True)
    detalle = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            # Idempotencia: una acción se programa una sola vez por cada entrada a la etapa.
            models.UniqueConstraint(fields=['accion', 'historial'], name='ejecucion_unica_por_entrada'),
        ]
        indexes = [models.Index(fields=['estado', 'programada_para'])]


class EmailEnviado(models.Model):
    """Email automático enviado, con seguimiento de aperturas (píxel) y clics (links redirigidos)."""
    accion = models.ForeignKey(AccionEtapa, null=True, blank=True, on_delete=models.SET_NULL, related_name='emails')
    oportunidad = models.ForeignKey('crm.Oportunidad', null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    contacto = models.ForeignKey('crm.Contacto', null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    enviado_por = models.ForeignKey('users.User', null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    difusion = models.ForeignKey('Difusion', null=True, blank=True, on_delete=models.SET_NULL, related_name='emails')
    plantilla = models.ForeignKey('PlantillaEmail', null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    baja_at = models.DateTimeField(null=True, blank=True)
    para = models.EmailField()
    asunto = models.CharField(max_length=200)
    token = models.CharField(max_length=40, unique=True)
    enviado_at = models.DateTimeField(auto_now_add=True, db_index=True)
    abierto_at = models.DateTimeField(null=True, blank=True)
    aperturas = models.PositiveIntegerField(default=0)
    clic_at = models.DateTimeField(null=True, blank=True)
    clics = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['-enviado_at']



def _ruta_imagen_email(instance, filename):
    import os
    import secrets
    ext = os.path.splitext(filename)[1].lower() or '.jpg'
    # Carpeta "email/": core.views.media_protegida la sirve pública (sin login), para que la abran los correos.
    return f'email/{secrets.token_hex(8)}{ext}'


class ImagenEmail(models.Model):
    """Imagen pública (flyers) para pegar en emails/difusiones. Se sirve sin login; aislada por cliente."""
    nombre = models.CharField(max_length=160, blank=True)
    archivo = models.ImageField(upload_to=_ruta_imagen_email)
    creada_por = models.ForeignKey('users.User', null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Imagen de email'
        verbose_name_plural = 'Imágenes de email'

    def __str__(self):
        return self.nombre or self.archivo.name

    @property
    def url(self):
        from core.tenancy import url_sitio
        return f'{url_sitio()}{self.archivo.url}'


class PlantillaEmail(models.Model):
    """Email reutilizable: en la ficha, en automatizaciones y en difusiones. Variables: {nombre} {primer_nombre}…"""
    nombre = models.CharField(max_length=120, unique=True)
    asunto = models.CharField(max_length=200)
    cuerpo = models.TextField(help_text='Texto del email. Los links se miden solos. Variables: {nombre} {primer_nombre} '
                                        '{agente} {embudo} {etapa} y los campos personalizados.')
    es_html = models.BooleanField(default=False, verbose_name='El cuerpo es HTML',
                                  help_text='Marcalo si pegaste HTML (diseño, imágenes, botones). Se respeta tal cual; '
                                            'igual se agregan el seguimiento y el link de baja.')
    activa = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['nombre']

    def __str__(self):
        return self.nombre


class ConfigEmail(models.Model):
    """Servidor de correo (SMTP) configurable desde la pantalla. Si está inactivo se usa el del archivo .env."""
    activo = models.BooleanField(default=False, verbose_name='Usar esta configuración')
    host = models.CharField(max_length=200, blank=True, verbose_name='Servidor SMTP')
    puerto = models.PositiveIntegerField(default=587)
    usuario = models.CharField(max_length=200, blank=True)
    password = CampoCifrado(blank=True, verbose_name='Contraseña')
    seguridad = models.CharField(max_length=5, default='tls', choices=[('tls', 'STARTTLS (587)'), ('ssl', 'SSL (465)'),
                                                                      ('', 'Ninguna')])
    remitente = models.CharField(max_length=200, blank=True, verbose_name='Remitente',
                                 help_text='Ej: "Ventas <ventas@tuempresa.com>". Tiene que estar autorizado en el servidor.')
    por_minuto = models.PositiveSmallIntegerField(default=30, verbose_name='Emails por minuto en difusiones',
                                                  help_text='Gmail: no más de ~500 por día. Brevo / SES / Mailgun: más.')

    class Meta:
        verbose_name = 'Configuración de email'

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class Difusion(models.Model):
    """Envío masivo a un grupo de leads (elegidos con los filtros de la lista), por email o WhatsApp."""
    CANAL_EMAIL = 'email'
    CANAL_WHATSAPP = 'whatsapp'
    CANALES = [(CANAL_EMAIL, 'Email'), (CANAL_WHATSAPP, 'WhatsApp')]
    BORRADOR, PROGRAMADA, ENVIANDO, FINALIZADA, CANCELADA = 'borrador', 'programada', 'enviando', 'finalizada', 'cancelada'
    ESTADOS = [(BORRADOR, 'Borrador'), (PROGRAMADA, 'Programada'), (ENVIANDO, 'Enviando'), (FINALIZADA, 'Finalizada'),
               (CANCELADA, 'Cancelada')]

    nombre = models.CharField(max_length=150)
    canal = models.CharField(max_length=10, choices=CANALES, default=CANAL_EMAIL)
    plantilla_email = models.ForeignKey(PlantillaEmail, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    plantilla_wa = models.ForeignKey('whatsapp.Plantilla', null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name='+', verbose_name='Plantilla de WhatsApp')
    texto_wa = models.TextField(blank=True, verbose_name='Texto (solo líneas no oficiales, sin plantilla)')
    linea = models.ForeignKey('whatsapp.LineaWhatsApp', null=True, blank=True, on_delete=models.SET_NULL, related_name='+',
                              help_text='Vacío = la del chat existente o la del embudo de cada lead.')
    estado = models.CharField(max_length=12, choices=ESTADOS, default=BORRADOR, db_index=True)
    programada_para = models.DateTimeField(null=True, blank=True)
    descripcion_filtro = models.CharField(max_length=500, blank=True)
    creada_por = models.ForeignKey('users.User', null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    iniciada_at = models.DateTimeField(null=True, blank=True)
    finalizada_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.nombre


class DifusionDestinatario(models.Model):
    PENDIENTE, ENVIADO, OMITIDO, ERROR = 'pendiente', 'enviado', 'omitido', 'error'
    ESTADOS = [(PENDIENTE, 'Pendiente'), (ENVIADO, 'Enviado'), (OMITIDO, 'Omitido'), (ERROR, 'Error')]
    difusion = models.ForeignKey(Difusion, on_delete=models.CASCADE, related_name='destinatarios')
    contacto = models.ForeignKey('crm.Contacto', on_delete=models.CASCADE, related_name='+')
    oportunidad = models.ForeignKey('crm.Oportunidad', null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    estado = models.CharField(max_length=10, choices=ESTADOS, default=PENDIENTE, db_index=True)
    detalle = models.CharField(max_length=300, blank=True)
    mensaje = models.ForeignKey('whatsapp.Mensaje', null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    email = models.ForeignKey(EmailEnviado, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    enviado_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['difusion', 'contacto'], name='difusion_contacto_unico')]
        indexes = [models.Index(fields=['difusion', 'estado'])]
