from django.conf import settings
from django.contrib.postgres.indexes import GinIndex
from django.db import models
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify

from core.phone import normalizar_telefono

User = settings.AUTH_USER_MODEL

COLORES = [
    ('#3B7EF6', 'Azul'), ('#7C5CFC', 'Violeta'), ('#06B6D4', 'Celeste'), ('#22C97A', 'Verde'),
    ('#F59E0B', 'Ámbar'), ('#F97316', 'Naranja'), ('#EF4444', 'Rojo'), ('#EC4899', 'Rosa'),
    ('#64748B', 'Gris'),
]


def _slug_unico(modelo, texto, pk=None, campo='slug'):
    base = slugify(texto)[:45] or 'item'
    slug, n = base, 1
    while modelo.objects.filter(**{campo: slug}).exclude(pk=pk).exists():
        n += 1
        slug = f'{base}-{n}'
    return slug


# ═══════════════════════════════════════════════════════════════════════════
# Embudos (campañas comerciales), etapas y tipificaciones
# ═══════════════════════════════════════════════════════════════════════════

class Embudo(models.Model):
    ASIG_ROUND_ROBIN = 'round_robin'
    ASIG_MENOR_CARGA = 'menor_carga'
    ASIG_MANUAL = 'manual'
    ASIGNACION_CHOICES = [
        (ASIG_ROUND_ROBIN, 'Rotativa (round robin) entre agentes habilitados'),
        (ASIG_MENOR_CARGA, 'Al agente con menos prospectos abiertos'),
        (ASIG_MANUAL, 'Manual (la asigna un supervisor)'),
    ]
    ENTRE_TODOS = 'todos'
    ENTRE_CONECTADOS = 'conectados'
    ENTRE_CHOICES = [
        (ENTRE_TODOS, 'Todos los agentes habilitados'),
        (ENTRE_CONECTADOS, 'Solo los agentes conectados en ese momento'),
    ]
    SIN_CONECTADOS_ENCOLAR = 'encolar'
    SIN_CONECTADOS_TODOS = 'todos'
    SIN_CONECTADOS_CHOICES = [
        (SIN_CONECTADOS_ENCOLAR, 'Esperar y asignarlo apenas alguien se conecte'),
        (SIN_CONECTADOS_TODOS, 'Asignarlo igual entre todos'),
    ]
    FUERA_HORARIO_ASIGNAR = 'asignar'
    FUERA_HORARIO_ENCOLAR = 'encolar'
    FUERA_HORARIO_CHOICES = [
        (FUERA_HORARIO_ENCOLAR, 'Dejar en cola y asignar al abrir el horario'),
        (FUERA_HORARIO_ASIGNAR, 'Asignar igual'),
    ]
    REINGRESO_NUEVA = 'nueva'
    REINGRESO_IGNORAR = 'ignorar'
    REINGRESO_CHOICES = [
        (REINGRESO_NUEVA, 'Abrir una oportunidad nueva'),
        (REINGRESO_IGNORAR, 'No abrir: solo registrar el reingreso en la ficha'),
    ]

    nombre = models.CharField(max_length=120, unique=True)
    slug = models.SlugField(max_length=60, unique=True, editable=False)
    descripcion = models.TextField(blank=True, verbose_name='Descripción')
    color = models.CharField(max_length=10, choices=COLORES, default='#3B7EF6')
    activo = models.BooleanField(default=True)
    orden = models.PositiveSmallIntegerField(default=0)

    agentes = models.ManyToManyField(
        User, blank=True, related_name='embudos',
        verbose_name='Agentes habilitados', help_text='Reciben prospectos de este embudo y lo ven en su tablero.',
    )
    supervisores = models.ManyToManyField(
        User, blank=True, related_name='embudos_supervisados',
        help_text='Reciben avisos de ventas y prospectos estancados. Vacío = todos los supervisores.',
    )

    # Asignación automática
    modo_asignacion = models.CharField(max_length=20, choices=ASIGNACION_CHOICES, default=ASIG_ROUND_ROBIN)
    ultimo_asignado = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
                                        related_name='+', editable=False)
    asignar_entre = models.CharField(max_length=12, choices=ENTRE_CHOICES, default=ENTRE_TODOS,
                                     verbose_name='Repartir entre')
    sin_conectados = models.CharField(max_length=10, choices=SIN_CONECTADOS_CHOICES, default=SIN_CONECTADOS_ENCOLAR,
                                      verbose_name='Si no hay nadie conectado')
    respetar_horario = models.BooleanField(default=False, verbose_name='Asignar solo en horario de atención')
    horario_desde = models.TimeField(default='09:00')
    horario_hasta = models.TimeField(default='18:00')
    dias_habiles = models.JSONField(default=list, blank=True, help_text='0=lunes … 6=domingo')
    fuera_de_horario = models.CharField(max_length=10, choices=FUERA_HORARIO_CHOICES, default=FUERA_HORARIO_ENCOLAR)
    SLA_AVISAR = 'avisar'
    SLA_REASIGNAR = 'reasignar'
    SLA_ACCIONES = [(SLA_AVISAR, 'Avisar al vendedor y a supervisión'),
                    (SLA_REASIGNAR, 'Avisar y reasignar a otro vendedor')]
    sla_minutos = models.PositiveIntegerField(
        default=0, verbose_name='SLA de primer contacto (minutos)',
        help_text='Tiempo máximo desde que se asigna el lead hasta la primera gestión (llamada, mensaje o intento). '
                  '0 = sin SLA.')
    sla_accion = models.CharField(max_length=10, choices=SLA_ACCIONES, default=SLA_AVISAR,
                                  verbose_name='Si se vence el SLA')
    sla_max_reasignaciones = models.PositiveSmallIntegerField(
        default=1, verbose_name='Reasignar como máximo', help_text='Veces por lead (evita que dé vueltas).')
    crear_tarea_al_asignar = models.BooleanField(
        default=True, verbose_name='Crear tarea "Contactar prospecto" al asignar')

    # Canal por defecto para mensajes automáticos
    linea_whatsapp = models.ForeignKey(
        'whatsapp.LineaWhatsApp', null=True, blank=True, on_delete=models.SET_NULL, related_name='embudos',
        verbose_name='Línea de WhatsApp por defecto',
    )

    # Reglas operativas
    max_intentos_sin_respuesta = models.PositiveSmallIntegerField(
        default=5, verbose_name='Intentos antes de sugerir "Sin respuesta"')
    dias_inactividad_recordatorio = models.PositiveSmallIntegerField(
        default=3, verbose_name='Días sin actividad para recordar al agente', help_text='0 = desactivado')
    dias_estancado_alerta = models.PositiveSmallIntegerField(
        default=7, verbose_name='Días en la misma etapa para avisar a supervisión', help_text='0 = desactivado')
    notificar_venta_supervisores = models.BooleanField(default=True, verbose_name='Avisar a supervisión cada venta')
    reingreso_perdidos = models.CharField(
        max_length=10, choices=REINGRESO_CHOICES, default=REINGRESO_NUEVA,
        verbose_name='Si reingresa un prospecto que se perdió antes',
    )
    exigir_resultado = models.BooleanField(
        default=True, verbose_name='Pedir el resultado de la gestión al terminar cada llamada atendida',
        help_text='Al cortar se abre "Resultado de la gestión" y no se puede seguir (ni recibir la próxima llamada del '
                  'discador) sin cargarlo.')

    # Vencimiento de la ficha (cartera): pasado el plazo sin cerrarse, vuelve al reparto
    VENCE_REASIGNAR = 'reasignar'
    VENCE_LIBERAR = 'liberar'
    VENCE_AVISAR = 'avisar'
    VENCE_CHOICES = [
        (VENCE_REASIGNAR, 'Reasignarla a otro vendedor (reparto parejo)'),
        (VENCE_LIBERAR, 'Dejarla sin asignar (la reparte supervisión)'),
        (VENCE_AVISAR, 'Solo avisar al vendedor y a supervisión'),
    ]
    vence_dias = models.PositiveSmallIntegerField(
        default=0, verbose_name='La ficha vence a los (días)',
        help_text='Días desde que se le asignó al vendedor sin cerrarse (venta / no venta). 0 = no vence.')
    vence_accion = models.CharField(max_length=10, choices=VENCE_CHOICES, default=VENCE_REASIGNAR,
                                    verbose_name='Al vencer')
    vence_hasta_etapa = models.ForeignKey(
        'Etapa', null=True, blank=True, on_delete=models.SET_NULL, related_name='+',
        verbose_name='No vence desde la etapa',
        help_text='Desde esta etapa en adelante la ficha queda siempre del vendedor (ej. Preventa).')

    # Clientes (socios): a quién van sus mensajes, llamadas perdidas y reingresos
    SOCIOS_VENDEDOR = 'vendedor'
    SOCIOS_USUARIO = 'usuario'
    SOCIOS_NADIE = ''
    SOCIOS_CHOICES = [
        (SOCIOS_VENDEDOR, 'Al vendedor que hizo la venta'),
        (SOCIOS_USUARIO, 'A un usuario fijo (postventa / experiencia del socio)'),
        (SOCIOS_NADIE, 'A nadie (quedan sin asignar)'),
    ]
    socios_a = models.CharField(max_length=10, choices=SOCIOS_CHOICES, blank=True, default=SOCIOS_VENDEDOR,
                                verbose_name='Si escribe o llama alguien que ya es cliente, va')
    socios_usuario = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name='+',
                                       verbose_name='Usuario de postventa')
    clientes_de_otros = models.BooleanField(
        default=False, verbose_name='Abrir tarjeta aunque ya sea cliente de otro embudo',
        help_text='Desmarcado: si el número ya tiene una venta en cualquier embudo, no se abre tarjeta ni se reparte '
                  '(queda como reingreso del cliente). Marcalo en embudos de upgrade / venta cruzada.')
    etiqueta_venta = models.ForeignKey(
        'Etiqueta', null=True, blank=True, on_delete=models.SET_NULL, related_name='+',
        verbose_name='Etiqueta al cerrar como venta', help_text='Ej. "Socio": se le pone al contacto al ganar.')

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['orden', 'nombre']
        verbose_name = 'Embudo'
        verbose_name_plural = 'Embudos'

    def __str__(self):
        return self.nombre

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = _slug_unico(Embudo, self.nombre, self.pk)
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse('crm:tablero') + f'?embudo={self.pk}'

    @property
    def etapa_inicial(self):
        return self.etapas.filter(tipo=Etapa.TIPO_NORMAL).order_by('orden', 'pk').first()

    @property
    def etapa_ganado(self):
        return self.etapas.filter(tipo=Etapa.TIPO_GANADO).order_by('orden').first()

    @property
    def etapa_perdido(self):
        return self.etapas.filter(tipo=Etapa.TIPO_PERDIDO).order_by('orden').first()

    def en_horario(self, momento=None) -> bool:
        if not self.respetar_horario:
            return True
        momento = timezone.localtime(momento or timezone.now())
        dias = self.dias_habiles if self.dias_habiles else [0, 1, 2, 3, 4]
        if momento.weekday() not in dias:
            return False
        return _hora(self.horario_desde) <= momento.time() <= _hora(self.horario_hasta)

    def responsable_de_cliente(self, venta):
        """Quién atiende a un cliente (venta cerrada) que vuelve a escribir o llamar."""
        if self.socios_a == self.SOCIOS_USUARIO and self.socios_usuario_id and self.socios_usuario.is_active:
            return self.socios_usuario
        if self.socios_a == self.SOCIOS_VENDEDOR and venta is not None and venta.agente_id and venta.agente.is_active:
            return venta.agente
        return None

    def tipificaciones_disponibles(self):
        return Tipificacion.objects.filter(Q(embudo=self) | Q(embudo__isnull=True), activa=True)


def _hora(valor):
    if isinstance(valor, str):
        from datetime import time
        h, m = valor.split(':')[:2]
        return time(int(h), int(m))
    return valor


class Etapa(models.Model):
    TIPO_NORMAL = 'normal'
    TIPO_GANADO = 'ganado'
    TIPO_PERDIDO = 'perdido'
    TIPO_CHOICES = [
        (TIPO_NORMAL, 'En proceso'),
        (TIPO_GANADO, 'Cierre ganado (Venta)'),
        (TIPO_PERDIDO, 'Cierre perdido (No venta)'),
    ]

    embudo = models.ForeignKey(Embudo, on_delete=models.CASCADE, related_name='etapas')
    nombre = models.CharField(max_length=80)
    descripcion = models.CharField(max_length=300, blank=True)
    orden = models.PositiveSmallIntegerField(default=0)
    color = models.CharField(max_length=10, choices=COLORES, default='#3B7EF6')
    tipo = models.CharField(max_length=10, choices=TIPO_CHOICES, default=TIPO_NORMAL)
    marca_contacto_efectivo = models.BooleanField(
        default=False, verbose_name='Llegar acá cuenta como "contacto efectivo"',
        help_text='Se usa para medir tasa de contactabilidad.',
    )
    campos_requeridos = models.JSONField(
        default=list, blank=True, verbose_name='Campos obligatorios desde esta etapa',
        help_text='Claves de campos fijos (email, dni, valor…) o "cp:<slug>" de campos personalizados. '
                  'Se exigen al pasar a esta etapa o a cualquiera posterior (incluida Venta; no para No venta).',
    )

    class Meta:
        ordering = ['embudo', 'orden', 'pk']
        unique_together = [('embudo', 'nombre')]
        verbose_name = 'Etapa'
        verbose_name_plural = 'Etapas'

    def __str__(self):
        return self.nombre

    @property
    def es_cierre(self):
        return self.tipo in (self.TIPO_GANADO, self.TIPO_PERDIDO)

    @property
    def es_ganado(self):
        return self.tipo == self.TIPO_GANADO

    @property
    def es_perdido(self):
        return self.tipo == self.TIPO_PERDIDO


# Campos fijos que se pueden exigir por etapa: (clave, etiqueta, dónde vive, tipo para el formulario)
CAMPOS_FIJOS_REQUERIBLES = [
    ('email', 'Email', 'contacto', 'email'),
    ('dni', 'DNI', 'contacto', 'texto'),
    ('fecha_atencion', 'Fecha de atención', 'contacto', 'fecha'),
    ('especialidad_atencion', 'Motivo / especialidad', 'contacto', 'texto'),
    ('fecha_nacimiento', 'Fecha de nacimiento', 'contacto', 'fecha'),
    ('localidad', 'Localidad', 'contacto', 'texto'),
    ('provincia', 'Provincia', 'contacto', 'texto'),
    ('telefono_alternativo', 'Teléfono alternativo', 'contacto', 'telefono'),
    ('valor', 'Valor / cuota ($)', 'oportunidad', 'numero'),
]


class Tipificacion(models.Model):
    RESULTADO_VENTA = 'venta'
    RESULTADO_NO_VENTA = 'no_venta'
    RESULTADO_CHOICES = [
        (RESULTADO_VENTA, 'Venta (ganado)'),
        (RESULTADO_NO_VENTA, 'No venta (perdido)'),
    ]
    ACCION_NINGUNA = ''
    ACCION_POSTERGAR = 'postergar'
    ACCION_NO_CONTACTAR = 'no_contactar'
    ACCION_DATO_ERRONEO = 'dato_erroneo'
    ACCION_CHOICES = [
        (ACCION_NINGUNA, 'Ninguna'),
        (ACCION_POSTERGAR, 'Postergar: no cierra, pausa y agenda recontacto'),
        (ACCION_NO_CONTACTAR, 'Marcar contacto como "no contactar" (bloquea mensajes y discador)'),
        (ACCION_DATO_ERRONEO, 'Marcar teléfono como inválido'),
    ]

    embudo = models.ForeignKey(Embudo, null=True, blank=True, on_delete=models.CASCADE,
                               related_name='tipificaciones', help_text='Vacío = aplica a todos los embudos.')
    resultado = models.CharField(max_length=10, choices=RESULTADO_CHOICES)
    categoria = models.CharField(max_length=80, blank=True, verbose_name='Categoría',
                                 help_text='Agrupa motivos en reportes (ej: "Sin interés", "Motivo económico").')
    nombre = models.CharField(max_length=120)
    descripcion = models.CharField(max_length=300, blank=True, verbose_name='Cuándo se aplica')
    accion = models.CharField(max_length=20, choices=ACCION_CHOICES, blank=True, default=ACCION_NINGUNA)
    requiere_nota = models.BooleanField(default=False, verbose_name='Nota obligatoria')
    orden = models.PositiveSmallIntegerField(default=0)
    activa = models.BooleanField(default=True)

    class Meta:
        ordering = ['resultado', 'orden', 'categoria', 'nombre']
        verbose_name = 'Tipificación'
        verbose_name_plural = 'Tipificaciones'

    def __str__(self):
        return self.nombre

    @property
    def es_postergacion(self):
        return self.accion == self.ACCION_POSTERGAR


class ResultadoGestion(models.Model):
    """Resultado de cada gestión (llamada atendida): lo elige la vendedora al cortar. Obligatorio."""
    embudo = models.ForeignKey(Embudo, null=True, blank=True, on_delete=models.CASCADE, related_name='resultados',
                               help_text='Vacío = aplica a todos los embudos.')
    nombre = models.CharField(max_length=80)
    contactado = models.BooleanField(default=True, verbose_name='Habló con el cliente',
                                     help_text='Cuenta como contacto efectivo en los reportes.')
    pide_fecha = models.BooleanField(default=False, verbose_name='Pide fecha para volver a llamar',
                                     help_text='Crea la tarea de rellamado para esa fecha.')
    mover_a = models.ForeignKey('Etapa', null=True, blank=True, on_delete=models.SET_NULL, related_name='+',
                                verbose_name='Mover la tarjeta a', help_text='Opcional. Solo etapas abiertas.')
    orden = models.PositiveSmallIntegerField(default=0)
    activo = models.BooleanField(default=True)

    class Meta:
        ordering = ['orden', 'nombre']
        verbose_name = 'Resultado de gestión'
        verbose_name_plural = 'Resultados de gestión'

    def __str__(self):
        return self.nombre

    @classmethod
    def para(cls, embudo):
        qs = cls.objects.filter(activo=True)
        return qs.filter(Q(embudo=embudo) | Q(embudo__isnull=True)) if embudo else qs.filter(embudo__isnull=True)


# ═══════════════════════════════════════════════════════════════════════════
# Contactos (personas, únicos por teléfono) y oportunidades (tarjetas)
# ═══════════════════════════════════════════════════════════════════════════

class CampoPersonalizado(models.Model):
    """Campo extra del contacto, definido desde Configuración. El valor se guarda en Contacto.datos_extra[slug]."""
    TIPO_TEXTO = 'texto'
    TIPO_TEXTO_LARGO = 'texto_largo'
    TIPO_NUMERO = 'numero'
    TIPO_FECHA = 'fecha'
    TIPO_SINO = 'sino'
    TIPO_LISTA = 'lista'
    TIPO_EMAIL = 'email'
    TIPO_TELEFONO = 'telefono'
    TIPO_URL = 'url'
    TIPO_ARCHIVO = 'archivo'
    TIPO_CHOICES = [
        (TIPO_TEXTO, 'Texto corto'), (TIPO_TEXTO_LARGO, 'Texto largo'), (TIPO_NUMERO, 'Número'),
        (TIPO_FECHA, 'Fecha'), (TIPO_SINO, 'Sí / No'), (TIPO_LISTA, 'Lista de opciones'),
        (TIPO_EMAIL, 'Email'), (TIPO_TELEFONO, 'Teléfono'), (TIPO_URL, 'Link (URL)'),
        (TIPO_ARCHIVO, 'Archivo / documento (ej. recibo de sueldo, DNI)'),
    ]

    nombre = models.CharField(max_length=80)
    slug = models.SlugField(max_length=60, unique=True, editable=False,
                            help_text='Clave interna: se usa en la API, en importaciones y como variable {slug} en mensajes.')
    tipo = models.CharField(max_length=15, choices=TIPO_CHOICES, default=TIPO_TEXTO)
    opciones = models.JSONField(default=list, blank=True, help_text='Para "Lista de opciones".')
    ayuda = models.CharField(max_length=200, blank=True, verbose_name='Texto de ayuda')
    requerido = models.BooleanField(default=False, verbose_name='Obligatorio al cargar a mano')
    embudo = models.ForeignKey(Embudo, null=True, blank=True, on_delete=models.CASCADE, related_name='campos',
                               help_text='Vacío = aplica a todos los contactos.')
    mostrar_en_tarjeta = models.BooleanField(default=False, verbose_name='Mostrar en la tarjeta del tablero')
    mostrar_en_lista = models.BooleanField(default=False, verbose_name='Mostrar como columna en listas')
    filtrable = models.BooleanField(default=False, verbose_name='Permitir filtrar por este campo')
    orden = models.PositiveSmallIntegerField(default=0)
    activo = models.BooleanField(default=True)

    class Meta:
        ordering = ['orden', 'nombre']
        verbose_name = 'Campo personalizado'
        verbose_name_plural = 'Campos personalizados'

    def __str__(self):
        return self.nombre

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = _slug_unico(CampoPersonalizado, self.nombre, self.pk).replace('-', '_')
        if self.tipo != self.TIPO_LISTA:
            self.opciones = []
        super().save(*args, **kwargs)

    @property
    def es_archivo(self):
        return self.tipo == self.TIPO_ARCHIVO

    def valor_display(self, valor):
        if valor in (None, '', []):
            return ''
        if self.tipo == self.TIPO_ARCHIVO:
            n = len(valor) if isinstance(valor, list) else 1
            return f'{n} archivo{"s" if n != 1 else ""}'
        if self.tipo == self.TIPO_SINO:
            return 'Sí' if str(valor).lower() in ('true', '1', 'si', 'sí', 'yes') else 'No'
        if self.tipo == self.TIPO_FECHA:
            try:
                from datetime import date
                return date.fromisoformat(str(valor)[:10]).strftime('%d/%m/%Y')
            except ValueError:
                return str(valor)
        return str(valor)

    @classmethod
    def activos(cls, embudo=None):
        qs = cls.objects.filter(activo=True)
        if embudo is not None:
            qs = qs.filter(Q(embudo=embudo) | Q(embudo__isnull=True))
        return list(qs)


class Etiqueta(models.Model):
    nombre = models.CharField(max_length=50, unique=True)
    color = models.CharField(max_length=10, choices=COLORES, default='#64748B')

    class Meta:
        ordering = ['nombre']

    def __str__(self):
        return self.nombre


class ContactoQuerySet(models.QuerySet):
    def visibles_para(self, user):
        if user.ve_todo:
            return self
        return self.filter(Q(oportunidades__agente=user) | Q(creado_por=user)).distinct()


class Contacto(models.Model):
    nombre = models.CharField(max_length=200, verbose_name='Nombre y apellido')
    telefono = models.CharField(max_length=20, blank=True, db_index=True, verbose_name='Teléfono',
                                help_text='Se normaliza a formato internacional (+549…). Es la clave anti-duplicados.')
    telefono_alternativo = models.CharField(max_length=20, blank=True, db_index=True)
    email = models.EmailField(blank=True, db_index=True)
    dni = models.CharField(max_length=12, blank=True, db_index=True, verbose_name='DNI')
    fecha_nacimiento = models.DateField(null=True, blank=True)
    localidad = models.CharField(max_length=100, blank=True)
    provincia = models.CharField(max_length=100, blank=True)

    # Datos propios del segmento Prospecto (base del centro médico)
    fecha_atencion = models.DateField(null=True, blank=True, verbose_name='Fecha de atención en el centro médico')
    especialidad_atencion = models.CharField(
        max_length=120, blank=True, verbose_name='Motivo / especialidad de la consulta',
        help_text='Opcional: permite segmentar el discurso comercial.',
    )

    etiquetas = models.ManyToManyField(Etiqueta, blank=True, related_name='contactos')
    datos_extra = models.JSONField(default=dict, blank=True,
                                   help_text='Columnas adicionales de importaciones / integraciones.')

    no_contactar = models.BooleanField(default=False, db_index=True, verbose_name='No contactar',
                                       help_text='Pidió no recibir comunicaciones: se bloquean mensajes y discador.')
    no_contactar_desde = models.DateTimeField(null=True, blank=True)
    no_email = models.BooleanField(default=False, verbose_name='No recibir emails masivos',
                                   help_text='Se dio de baja desde un email: no recibe difusiones ni emails automáticos.')
    no_email_at = models.DateTimeField(null=True, blank=True)
    telefono_invalido = models.BooleanField(default=False, verbose_name='Teléfono inválido')

    creado_por = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = ContactoQuerySet.as_manager()

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Contacto'
        verbose_name_plural = 'Contactos'
        constraints = [
            models.UniqueConstraint(fields=['telefono'], condition=~Q(telefono=''), name='contacto_telefono_unico'),
        ]
        indexes = [
            # Búsqueda por nombre/email con ILIKE rápida aunque haya cientos de miles de contactos.
            GinIndex(fields=['nombre'], name='contacto_nombre_trgm', opclasses=['gin_trgm_ops']),
            GinIndex(fields=['email'], name='contacto_email_trgm', opclasses=['gin_trgm_ops']),
        ]

    def __str__(self):
        return self.nombre or self.telefono

    def save(self, *args, **kwargs):
        self.telefono = normalizar_telefono(self.telefono)
        self.telefono_alternativo = normalizar_telefono(self.telefono_alternativo)
        self.email = (self.email or '').strip().lower()
        if self.no_contactar and not self.no_contactar_desde:
            self.no_contactar_desde = timezone.now()
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse('crm:contacto_detalle', args=[self.pk])

    @property
    def puede_recibir_mensajes(self):
        return bool(self.telefono) and not self.no_contactar and not self.telefono_invalido


class OportunidadQuerySet(models.QuerySet):
    def visibles_para(self, user):
        if user.ve_todo:
            return self
        return self.filter(agente=user)

    def activas(self):
        return self.filter(estado__in=Oportunidad.ESTADOS_ACTIVOS)


class Oportunidad(models.Model):
    ESTADO_ABIERTA = 'abierta'
    ESTADO_PAUSADA = 'pausada'
    ESTADO_GANADA = 'ganada'
    ESTADO_PERDIDA = 'perdida'
    ESTADO_CHOICES = [
        (ESTADO_ABIERTA, 'En curso'),
        (ESTADO_PAUSADA, 'Pausada'),
        (ESTADO_GANADA, 'Venta'),
        (ESTADO_PERDIDA, 'No venta'),
    ]
    ESTADOS_ACTIVOS = (ESTADO_ABIERTA, ESTADO_PAUSADA)

    ORIGEN_IMPORTACION = 'importacion'
    ORIGEN_API = 'api'
    ORIGEN_WHATSAPP = 'whatsapp'
    ORIGEN_LLAMADA_ENTRANTE = 'llamada_entrante'
    ORIGEN_LLAMADA_SALIENTE = 'llamada_saliente'
    ORIGEN_MANUAL = 'manual'
    ORIGEN_WEB = 'web'
    ORIGEN_CHOICES = [
        (ORIGEN_IMPORTACION, 'Importación de base'),
        (ORIGEN_API, 'Integración / API'),
        (ORIGEN_WEB, 'Formulario web'),
        (ORIGEN_WHATSAPP, 'WhatsApp entrante'),
        (ORIGEN_LLAMADA_ENTRANTE, 'Llamada entrante'),
        (ORIGEN_LLAMADA_SALIENTE, 'Llamada saliente'),
        (ORIGEN_MANUAL, 'Carga manual'),
    ]

    contacto = models.ForeignKey(Contacto, on_delete=models.CASCADE, related_name='oportunidades')
    embudo = models.ForeignKey(Embudo, on_delete=models.PROTECT, related_name='oportunidades')
    etapa = models.ForeignKey(Etapa, on_delete=models.PROTECT, related_name='oportunidades')
    estado = models.CharField(max_length=10, choices=ESTADO_CHOICES, default=ESTADO_ABIERTA)
    agente = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name='oportunidades')
    pendiente_asignacion = models.BooleanField(default=False, help_text='Ingresó fuera de horario y espera asignación.')

    origen = models.CharField(max_length=20, choices=ORIGEN_CHOICES, default=ORIGEN_MANUAL)
    fuente = models.CharField(max_length=150, blank=True, help_text='Detalle del origen: nombre de la base, landing, DID…')
    lote = models.ForeignKey('ImportacionLote', null=True, blank=True, on_delete=models.SET_NULL,
                             related_name='oportunidades')
    pauta = models.ForeignKey('pautas.Pauta', null=True, blank=True, on_delete=models.SET_NULL,
                              related_name='oportunidades')
    # Pase entre embudos: de dónde vino la tarjeta (para "volver al embudo anterior") o qué oportunidad la originó
    embudo_previo = models.ForeignKey('Embudo', null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    etapa_previa = models.ForeignKey('Etapa', null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    oportunidad_origen = models.ForeignKey('self', null=True, blank=True, on_delete=models.SET_NULL,
                                           related_name='derivadas')
    origen_pauta = models.CharField(max_length=200, blank=True, db_index=True, verbose_name='Origen (pauta)',
                                    help_text='Texto de origen tal cual llegó (formulario, utm_campaign…).')
    valor = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True,
                                verbose_name='Valor / cuota ($)')

    # Cierre
    tipificacion = models.ForeignKey(Tipificacion, null=True, blank=True, on_delete=models.PROTECT,
                                     related_name='oportunidades')
    nota_cierre = models.TextField(blank=True)
    cerrada_at = models.DateTimeField(null=True, blank=True, db_index=True)
    cerrada_por = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')

    # Gestión
    intentos_contacto = models.PositiveSmallIntegerField(default=0)
    puntaje = models.IntegerField(default=0, db_index=True, help_text='Lead scoring según las reglas configuradas.')
    ingresos = models.PositiveIntegerField(default=1, help_text='Veces que la persona entró por esta oportunidad '
                                                                '(primer ingreso + reingresos).')
    ultimo_intento_at = models.DateTimeField(null=True, blank=True)
    contacto_efectivo_at = models.DateTimeField(null=True, blank=True)
    ultima_actividad_at = models.DateTimeField(default=timezone.now)
    etapa_desde = models.DateTimeField(default=timezone.now)
    proximo_contacto_at = models.DateTimeField(null=True, blank=True,
                                               help_text='Pausada hasta / fecha de recontacto.')
    motivo_pausa = models.CharField(max_length=200, blank=True)
    asignada_at = models.DateTimeField(null=True, blank=True)
    primer_contacto_at = models.DateTimeField(null=True, blank=True, db_index=True,
                                              help_text='Primera gestión del vendedor: llamada, mensaje o intento.')
    prioritaria = models.BooleanField(default=False, db_index=True,
                                      help_text='Marcada como prioridad (ej. reingresó): aparece primero.')
    prioridad_motivo = models.CharField(max_length=150, blank=True)
    vencida_at = models.DateTimeField(null=True, blank=True, editable=False,
                                      help_text='Cuándo se procesó el vencimiento de la ficha (se limpia al reasignar).')
    sla_alerta_at = models.DateTimeField(null=True, blank=True, editable=False)
    reasignaciones_sla = models.PositiveSmallIntegerField(default=0, editable=False)
    recordatorio_enviado_at = models.DateTimeField(null=True, blank=True, editable=False)
    alerta_estancado_at = models.DateTimeField(null=True, blank=True, editable=False)

    datos_extra = models.JSONField(default=dict, blank=True)
    creado_por = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = OportunidadQuerySet.as_manager()

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Oportunidad'
        verbose_name_plural = 'Oportunidades'
        constraints = [
            # Anti-duplicado: una sola oportunidad activa por persona y embudo.
            models.UniqueConstraint(fields=['contacto', 'embudo'], condition=Q(estado__in=['abierta', 'pausada']),
                                    name='una_oportunidad_activa_por_embudo'),
        ]
        indexes = [
            models.Index(fields=['embudo', 'etapa', 'estado']),
            models.Index(fields=['agente', 'estado']),
            models.Index(fields=['estado', 'ultima_actividad_at']),
            models.Index(fields=['estado', 'proximo_contacto_at']),
            models.Index(fields=['pendiente_asignacion', 'embudo']),
            models.Index(fields=['pauta', 'created_at']),
        ]

    def __str__(self):
        return f'{self.contacto} · {self.embudo}'

    def get_absolute_url(self):
        return reverse('crm:oportunidad_detalle', args=[self.pk])

    @property
    def activa(self):
        return self.estado in self.ESTADOS_ACTIVOS

    @property
    def dias_en_etapa(self):
        return (timezone.now() - self.etapa_desde).days if self.etapa_desde else 0

    @property
    def dias_sin_actividad(self):
        return (timezone.now() - self.ultima_actividad_at).days if self.ultima_actividad_at else 0

    @property
    def llego_max_intentos(self):
        return self.intentos_contacto >= self.embudo.max_intentos_sin_respuesta > 0

    def get_estado_badge(self):
        return {
            self.ESTADO_ABIERTA: 'primary', self.ESTADO_PAUSADA: 'warning',
            self.ESTADO_GANADA: 'success', self.ESTADO_PERDIDA: 'danger',
        }.get(self.estado, 'secondary')


class HistorialEtapa(models.Model):
    oportunidad = models.ForeignKey(Oportunidad, on_delete=models.CASCADE, related_name='historial')
    etapa_anterior = models.ForeignKey(Etapa, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    etapa_nueva = models.ForeignKey(Etapa, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    estado_anterior = models.CharField(max_length=10, blank=True)
    estado_nuevo = models.CharField(max_length=10, blank=True)
    usuario = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    nota = models.CharField(max_length=300, blank=True)
    segundos_en_etapa_anterior = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['oportunidad', '-created_at'])]


class Actividad(models.Model):
    TIPO_NOTA = 'nota'
    TIPO_LLAMADA = 'llamada'
    TIPO_WHATSAPP = 'whatsapp'
    TIPO_EMAIL = 'email'
    TIPO_ETAPA = 'etapa'
    TIPO_ASIGNACION = 'asignacion'
    TIPO_INTENTO = 'intento'
    TIPO_TAREA = 'tarea'
    TIPO_PAUSA = 'pausa'
    TIPO_CIERRE = 'cierre'
    TIPO_REINGRESO = 'reingreso'
    TIPO_SISTEMA = 'sistema'
    TIPO_CAMBIO = 'cambio'
    TIPO_SMS = 'sms'
    TIPO_CHOICES = [
        (TIPO_NOTA, 'Nota'), (TIPO_LLAMADA, 'Llamada'), (TIPO_WHATSAPP, 'WhatsApp'), (TIPO_EMAIL, 'Email'),
        (TIPO_ETAPA, 'Cambio de etapa'), (TIPO_ASIGNACION, 'Asignación'), (TIPO_INTENTO, 'Intento de contacto'),
        (TIPO_TAREA, 'Tarea'), (TIPO_PAUSA, 'Pausa / reactivación'), (TIPO_CIERRE, 'Cierre'),
        (TIPO_REINGRESO, 'Reingreso del dato'), (TIPO_SISTEMA, 'Sistema'), (TIPO_CAMBIO, 'Cambio de datos'), (TIPO_SMS, 'SMS'),
    ]
    ICONOS = {
        TIPO_NOTA: 'sticky', TIPO_LLAMADA: 'telephone', TIPO_WHATSAPP: 'whatsapp', TIPO_EMAIL: 'envelope',
        TIPO_ETAPA: 'arrow-right-circle', TIPO_ASIGNACION: 'person-check', TIPO_INTENTO: 'telephone-x',
        TIPO_TAREA: 'calendar-check', TIPO_PAUSA: 'pause-circle', TIPO_CIERRE: 'flag',
        TIPO_REINGRESO: 'arrow-repeat', TIPO_SISTEMA: 'gear', TIPO_CAMBIO: 'pencil-square', TIPO_SMS: 'phone',
    }

    contacto = models.ForeignKey(Contacto, on_delete=models.CASCADE, related_name='actividades')
    oportunidad = models.ForeignKey(Oportunidad, null=True, blank=True, on_delete=models.CASCADE,
                                    related_name='actividades')
    tipo = models.CharField(max_length=15, choices=TIPO_CHOICES)
    texto = models.TextField(blank=True)
    usuario = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    llamada = models.ForeignKey('telefonia.Llamada', null=True, blank=True, on_delete=models.SET_NULL,
                                related_name='actividades')
    datos = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['contacto', '-created_at']),
            models.Index(fields=['oportunidad', '-created_at']),
        ]
        verbose_name = 'Actividad'
        verbose_name_plural = 'Actividades'

    @property
    def icono(self):
        return self.ICONOS.get(self.tipo, 'dot')


class Tarea(models.Model):
    TIPO_LLAMADA = 'llamada'
    TIPO_WHATSAPP = 'whatsapp'
    TIPO_EMAIL = 'email'
    TIPO_SEGUIMIENTO = 'seguimiento'
    TIPO_REACTIVACION = 'reactivacion'
    TIPO_OTRO = 'otro'
    TIPO_CHOICES = [
        (TIPO_LLAMADA, 'Llamar'), (TIPO_WHATSAPP, 'WhatsApp'), (TIPO_EMAIL, 'Email'),
        (TIPO_SEGUIMIENTO, 'Seguimiento'), (TIPO_REACTIVACION, 'Reactivación'), (TIPO_OTRO, 'Otro'),
    ]
    ICONOS = {TIPO_LLAMADA: 'telephone', TIPO_WHATSAPP: 'whatsapp', TIPO_EMAIL: 'envelope',
              TIPO_SEGUIMIENTO: 'arrow-repeat', TIPO_REACTIVACION: 'alarm', TIPO_OTRO: 'check2-square'}
    PRIORIDAD_ALTA = 'alta'
    PRIORIDAD_NORMAL = 'normal'
    PRIORIDAD_BAJA = 'baja'
    PRIORIDAD_CHOICES = [(PRIORIDAD_ALTA, 'Alta'), (PRIORIDAD_NORMAL, 'Normal'), (PRIORIDAD_BAJA, 'Baja')]
    ESTADO_PENDIENTE = 'pendiente'
    ESTADO_COMPLETADA = 'completada'
    ESTADO_CANCELADA = 'cancelada'
    ESTADO_CHOICES = [(ESTADO_PENDIENTE, 'Pendiente'), (ESTADO_COMPLETADA, 'Completada'),
                      (ESTADO_CANCELADA, 'Cancelada')]

    oportunidad = models.ForeignKey(Oportunidad, null=True, blank=True, on_delete=models.CASCADE, related_name='tareas')
    contacto = models.ForeignKey(Contacto, null=True, blank=True, on_delete=models.CASCADE, related_name='tareas')
    asignado_a = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name='tareas')
    creada_por = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    tipo = models.CharField(max_length=15, choices=TIPO_CHOICES, default=TIPO_LLAMADA)
    titulo = models.CharField(max_length=200)
    descripcion = models.TextField(blank=True)
    vence_at = models.DateTimeField(verbose_name='Vence')
    prioridad = models.CharField(max_length=10, choices=PRIORIDAD_CHOICES, default=PRIORIDAD_NORMAL)
    estado = models.CharField(max_length=12, choices=ESTADO_CHOICES, default=ESTADO_PENDIENTE)
    resultado = models.TextField(blank=True)
    completada_at = models.DateTimeField(null=True, blank=True)
    automatica = models.BooleanField(default=False)
    notificada_vencida = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['vence_at']
        indexes = [models.Index(fields=['asignado_a', 'estado', 'vence_at'])]
        verbose_name = 'Tarea'
        verbose_name_plural = 'Tareas'

    def __str__(self):
        return self.titulo

    @property
    def vencida(self):
        return self.estado == self.ESTADO_PENDIENTE and self.vence_at < timezone.now()

    @property
    def icono(self):
        return self.ICONOS.get(self.tipo, 'check2-square')


class ImportacionLote(models.Model):
    ESTADO_PENDIENTE = 'pendiente'
    ESTADO_PROCESANDO = 'procesando'
    ESTADO_COMPLETADO = 'completado'
    ESTADO_ERROR = 'error'
    ESTADO_CHOICES = [(ESTADO_PENDIENTE, 'Pendiente'), (ESTADO_PROCESANDO, 'Procesando'),
                      (ESTADO_COMPLETADO, 'Completado'), (ESTADO_ERROR, 'Error')]

    archivo = models.FileField(upload_to='importaciones/%Y/%m/')
    nombre_archivo = models.CharField(max_length=255)
    embudo = models.ForeignKey(Embudo, on_delete=models.PROTECT, related_name='importaciones')
    etapa = models.ForeignKey(Etapa, null=True, blank=True, on_delete=models.SET_NULL, related_name='+',
                              help_text='Vacío = etapa inicial del embudo.')
    fuente = models.CharField(max_length=150, blank=True, help_text='Ej: "Base centro médico – septiembre".')
    pauta = models.ForeignKey('pautas.Pauta', null=True, blank=True, on_delete=models.SET_NULL, related_name='+',
                              help_text='Pauta de toda la base (si no viene una columna de origen por fila).')
    mapeo = models.JSONField(default=dict, blank=True, help_text='{columna_archivo: campo_crm}')
    asignar_automaticamente = models.BooleanField(default=True)
    agente_fijo = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    disparar_automatizaciones = models.BooleanField(default=True,
                                                    help_text='Mensajes de bienvenida y demás acciones de la etapa.')
    estado = models.CharField(max_length=12, choices=ESTADO_CHOICES, default=ESTADO_PENDIENTE)
    total = models.PositiveIntegerField(default=0)
    procesados = models.PositiveIntegerField(default=0)
    creados = models.PositiveIntegerField(default=0, help_text='Oportunidades nuevas')
    contactos_nuevos = models.PositiveIntegerField(default=0)
    ya_existentes = models.PositiveIntegerField(default=0, help_text='Ya tenían una oportunidad activa: no se duplicó')
    errores = models.PositiveIntegerField(default=0)
    errores_detalle = models.JSONField(default=list, blank=True)
    creado_por = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    finalizado_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Importación'
        verbose_name_plural = 'Importaciones'

    def __str__(self):
        return f'{self.nombre_archivo} ({self.created_at:%d/%m/%Y})'

    @property
    def porcentaje(self):
        return int(self.procesados * 100 / self.total) if self.total else 0


class ReglaAsignacion(models.Model):
    """
    Excepción a la asignación del embudo según el origen del lead. Se evalúan en orden; gana la primera que
    coincide. Sin regla que coincida, se usa la asignación general del embudo.
    """
    ACCION_AGENTES = 'agentes'
    ACCION_SIN_ASIGNAR = 'sin_asignar'
    ACCION_CHOICES = [
        (ACCION_AGENTES, 'Asignar solo a estos agentes'),
        (ACCION_SIN_ASIGNAR, 'No asignar (queda para asignación manual)'),
    ]
    ENTRE_EMBUDO = 'embudo'
    ENTRE_CHOICES = [(ENTRE_EMBUDO, 'Como el embudo')] + Embudo.ENTRE_CHOICES
    SI_NO_HAY_ENCOLAR = 'encolar'
    SI_NO_HAY_TODOS = 'todos'
    SI_NO_HAY_EMBUDO = 'embudo'
    SI_NO_HAY_CHOICES = [
        (SI_NO_HAY_ENCOLAR, 'Esperar a que uno de ellos se conecte / esté disponible'),
        (SI_NO_HAY_TODOS, 'Asignar igual entre ellos aunque no estén conectados'),
        (SI_NO_HAY_EMBUDO, 'Usar la asignación general del embudo'),
    ]

    embudo = models.ForeignKey(Embudo, on_delete=models.CASCADE, related_name='reglas_asignacion')
    nombre = models.CharField(max_length=120)
    orden = models.PositiveSmallIntegerField(default=0)
    activa = models.BooleanField(default=True)

    # Condiciones (todas las cargadas tienen que cumplirse; dentro de cada una alcanza con una opción)
    canales = models.JSONField(default=list, blank=True, verbose_name='Canal de ingreso',
                               help_text='Vacío = cualquier canal.')
    pautas = models.ManyToManyField('pautas.Pauta', blank=True, related_name='reglas_asignacion',
                                    help_text='Vacío = cualquier pauta.')
    textos_origen = models.JSONField(default=list, blank=True, verbose_name='El origen contiene',
                                     help_text='Vacío = cualquier origen.')

    accion = models.CharField(max_length=12, choices=ACCION_CHOICES, default=ACCION_AGENTES)
    agentes = models.ManyToManyField(User, blank=True, related_name='+')
    asignar_entre = models.CharField(max_length=12, choices=ENTRE_CHOICES, default=ENTRE_EMBUDO,
                                     verbose_name='Repartir entre')
    si_no_hay = models.CharField(max_length=10, choices=SI_NO_HAY_CHOICES, default=SI_NO_HAY_ENCOLAR,
                                 verbose_name='Si ninguno de ellos puede recibir')
    ultimo_asignado = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name='+',
                                        editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['embudo', 'orden', 'pk']
        verbose_name = 'Regla de asignación'
        verbose_name_plural = 'Reglas de asignación'

    def __str__(self):
        return self.nombre

    def coincide(self, op):
        from apps.pautas.models import normalizar_clave
        if self.canales and op.origen not in self.canales:
            return False
        if self.pk and self.pautas.exists() and not self.pautas.filter(pk=op.pauta_id or 0).exists():
            return False
        if self.textos_origen:
            textos = [normalizar_clave(t) for t in (op.origen_pauta, op.fuente, op.pauta.nombre if op.pauta_id else '')]
            textos = [t for t in textos if t]
            if not any(normalizar_clave(b) in t for b in self.textos_origen for t in textos if normalizar_clave(b)):
                return False
        return True

    def resumen_condiciones(self):
        partes = []
        if self.canales:
            nombres = dict(Oportunidad.ORIGEN_CHOICES)
            partes.append('canal ' + ' o '.join(str(nombres.get(c, c)) for c in self.canales))
        pautas = list(self.pautas.all())
        if pautas:
            partes.append('pauta ' + ' o '.join(p.nombre for p in pautas))
        if self.textos_origen:
            partes.append('origen contiene ' + ' o '.join(f'"{t}"' for t in self.textos_origen))
        return ' y '.join(partes) or 'todos los leads'


class ReglaPuntaje(models.Model):
    """Lead scoring por reglas: si el lead cumple la condición, suma (o resta) puntos."""
    COND_PAUTA = 'pauta'
    COND_CANAL = 'canal'
    COND_ORIGEN = 'origen_contiene'
    COND_CAMPO = 'campo'
    COND_EMAIL = 'tiene_email'
    COND_RESPONDIO = 'respondio'
    COND_ETAPA = 'etapa'
    COND_INTENTOS = 'intentos_min'
    COND_INACTIVO = 'sin_actividad_dias'
    COND_REINGRESOS = 'ingresos_min'
    CONDICIONES = [
        (COND_RESPONDIO, 'El cliente respondió alguna vez (WhatsApp, SMS o llamada atendida)'),
        (COND_PAUTA, 'Vino de la pauta…'), (COND_CANAL, 'Entró por el canal…'),
        (COND_ORIGEN, 'El origen contiene el texto…'), (COND_CAMPO, 'El campo … tiene el valor … (vacío = tiene dato)'),
        (COND_EMAIL, 'Tiene email'), (COND_ETAPA, 'Está en la etapa…'),
        (COND_REINGRESOS, 'Ingresó al menos N veces'), (COND_INTENTOS, 'Lleva al menos N intentos sin contacto efectivo'),
        (COND_INACTIVO, 'Lleva al menos N días sin actividad'),
    ]
    embudo = models.ForeignKey(Embudo, null=True, blank=True, on_delete=models.CASCADE, related_name='reglas_puntaje',
                               help_text='Vacío = todos los embudos.')
    condicion = models.CharField(max_length=20, choices=CONDICIONES)
    pauta = models.ForeignKey('pautas.Pauta', null=True, blank=True, on_delete=models.CASCADE, related_name='+')
    etapa = models.ForeignKey(Etapa, null=True, blank=True, on_delete=models.CASCADE, related_name='+')
    campo = models.CharField(max_length=80, blank=True, help_text='Clave del campo (ej. localidad, o la de un campo personalizado).')
    valor = models.CharField(max_length=200, blank=True)
    numero = models.PositiveIntegerField(default=0)
    puntos = models.IntegerField(help_text='Positivo suma, negativo resta.')
    activa = models.BooleanField(default=True)

    class Meta:
        ordering = ['embudo__nombre', '-puntos', 'pk']

    def __str__(self):
        return f'{self.descripcion()} → {self.puntos:+d}'

    def descripcion(self):
        c = self.condicion
        if c == self.COND_PAUTA:
            return f'Pauta {self.pauta}'
        if c == self.COND_CANAL:
            return f'Canal {dict(Oportunidad.ORIGEN_CHOICES).get(self.valor, self.valor)}'
        if c == self.COND_ORIGEN:
            return f'Origen contiene "{self.valor}"'
        if c == self.COND_CAMPO:
            return f'{self.campo} = {self.valor}' if self.valor else f'{self.campo} cargado'
        if c == self.COND_ETAPA:
            return f'En {self.etapa}'
        if c in (self.COND_INTENTOS, self.COND_INACTIVO, self.COND_REINGRESOS):
            return {self.COND_INTENTOS: f'{self.numero}+ intentos sin contacto',
                    self.COND_INACTIVO: f'{self.numero}+ días sin actividad',
                    self.COND_REINGRESOS: f'Ingresó {self.numero}+ veces'}[c]
        return self.get_condicion_display()
