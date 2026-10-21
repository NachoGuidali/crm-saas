from django.contrib.auth.models import AbstractUser, UserManager
from django.db import models
from django.utils.text import slugify

# ── Permisos granulares ─────────────────────────────────────────────────────
# (slug, etiqueta, grupo). Los roles base traen un set por defecto y los roles
# personalizados eligen explícitamente cuáles tienen.
PERMISOS = [
    ('ver_todo', 'Ver todas las oportunidades, contactos y chats (no solo los propios)', 'Visibilidad'),
    ('supervision', 'Panel de supervisión y carga por agente', 'Visibilidad'),
    ('reportes', 'Ver reportes y métricas', 'Visibilidad'),
    ('pautas', 'Análisis de pautas: crear pautas y cargar inversión', 'Marketing'),
    ('difusiones', 'Enviar difusiones masivas (email / WhatsApp)', 'Marketing'),
    ('reasignar', 'Reasignar oportunidades y chats a otros agentes', 'Gestión comercial'),
    ('importar', 'Importar bases de prospectos (Excel / CSV)', 'Gestión comercial'),
    ('exportar', 'Exportar datos a Excel / CSV', 'Gestión comercial'),
    ('eliminar', 'Eliminar contactos y oportunidades', 'Gestión comercial'),
    ('reabrir', 'Reabrir oportunidades cerradas (ganadas / perdidas)', 'Gestión comercial'),
    ('discador', 'Gestionar campañas del discador progresivo', 'Telefonía'),
    ('telefonia', 'Configurar Anura (credenciales, internos, DIDs)', 'Telefonía'),
    ('plantillas', 'Crear y editar plantillas de WhatsApp', 'WhatsApp'),
    ('lineas_whatsapp', 'Conectar y configurar líneas de WhatsApp', 'WhatsApp'),
    ('embudos', 'Configurar embudos, etapas y tipificaciones', 'Configuración'),
    ('campos', 'Configurar campos personalizados de los contactos', 'Configuración'),
    ('automatizaciones', 'Configurar automatizaciones y mensajes automáticos', 'Configuración'),
    ('integraciones', 'API keys e integraciones externas', 'Configuración'),
    ('postventa', 'Clientes / Postventa: cuentas, proyectos y tareas', 'Postventa'),
    ('usuarios', 'Alta, baja y edición de usuarios', 'Administración'),
]
PERMISOS_DICT = {slug: label for slug, label, _ in PERMISOS}
TODOS_LOS_PERMISOS = frozenset(PERMISOS_DICT)


def permisos_agrupados():
    grupos = {}
    for slug, label, grupo in PERMISOS:
        grupos.setdefault(grupo, []).append((slug, label))
    return list(grupos.items())


class RolPersonalizado(models.Model):
    nombre = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=100, unique=True, editable=False)
    descripcion = models.TextField(blank=True, verbose_name='Descripción')
    permisos = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['nombre']
        verbose_name = 'Rol personalizado'
        verbose_name_plural = 'Roles personalizados'

    def __str__(self):
        return self.nombre

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.nombre) or 'rol'
            slug, n = base, 1
            while RolPersonalizado.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                n += 1
                slug = f'{base}-{n}'
            self.slug = slug
        self.permisos = sorted(p for p in (self.permisos or []) if p in TODOS_LOS_PERMISOS)
        super().save(*args, **kwargs)

    def get_permisos_display(self):
        return [PERMISOS_DICT[p] for p in self.permisos if p in PERMISOS_DICT]


class UsuarioManager(UserManager):
    def activos(self):
        return self.filter(is_active=True)

    def asignables(self):
        """Usuarios que pueden recibir oportunidades automáticamente."""
        return self.filter(is_active=True, disponible=True)


class User(AbstractUser):
    ROL_ADMIN = 'admin'
    ROL_SUPERVISOR = 'supervisor'
    ROL_AGENTE = 'agente'
    ROL_CHOICES = [
        (ROL_ADMIN, 'Administrador'),
        (ROL_SUPERVISOR, 'Supervisor'),
        (ROL_AGENTE, 'Agente / Vendedor'),
    ]
    PERMISOS_POR_ROL = {
        ROL_ADMIN: TODOS_LOS_PERMISOS,
        ROL_SUPERVISOR: frozenset({
            'ver_todo', 'supervision', 'reportes', 'reasignar', 'importar', 'exportar',
            'reabrir', 'discador', 'plantillas', 'pautas', 'difusiones',
        }),
        ROL_AGENTE: frozenset(),
    }

    rol = models.CharField(max_length=20, choices=ROL_CHOICES, default=ROL_AGENTE)
    rol_custom = models.ForeignKey(
        RolPersonalizado, null=True, blank=True, on_delete=models.SET_NULL, related_name='usuarios',
        verbose_name='Rol personalizado',
        help_text='Si se elige, define exactamente qué permisos tiene (reemplaza a los del rol base).',
    )
    email = models.EmailField('email', blank=True, db_index=True)
    telefono = models.CharField(max_length=30, blank=True, verbose_name='Teléfono / interno')
    avatar = models.ImageField(upload_to='avatars/', blank=True, null=True)
    disponible = models.BooleanField(
        default=True, verbose_name='Disponible para asignaciones',
        help_text='Desactivar en vacaciones / licencia: deja de recibir prospectos nuevos.',
    )
    debe_cambiar_password = models.BooleanField(
        default=False, verbose_name='Debe cambiar la contraseña al ingresar',
    )

    objects = UsuarioManager()

    class Meta:
        verbose_name = 'Usuario'
        verbose_name_plural = 'Usuarios'
        ordering = ['first_name', 'last_name', 'username']

    def __str__(self):
        return self.display_name

    @property
    def display_name(self):
        return self.get_full_name() or self.username

    @property
    def iniciales(self):
        partes = (self.get_full_name() or self.username).split()
        return ''.join(p[0] for p in partes[:2]).upper()

    @property
    def is_admin(self):
        return self.is_superuser or self.rol == self.ROL_ADMIN

    @property
    def is_supervisor(self):
        return self.rol == self.ROL_SUPERVISOR

    @property
    def is_agente(self):
        return self.rol == self.ROL_AGENTE and not self.is_superuser

    def get_rol_display_full(self):
        if self.rol_custom_id:
            return self.rol_custom.nombre
        return self.get_rol_display()

    @property
    def permisos_efectivos(self) -> frozenset:
        cache_attr = '_permisos_cache'
        if not hasattr(self, cache_attr):
            if self.is_admin:
                permisos = TODOS_LOS_PERMISOS
            elif self.rol_custom_id:
                permisos = frozenset(self.rol_custom.permisos or [])
            else:
                permisos = self.PERMISOS_POR_ROL.get(self.rol, frozenset())
            setattr(self, cache_attr, permisos)
        return getattr(self, cache_attr)

    def tiene_permiso(self, slug: str) -> bool:
        return self.is_active and (self.is_admin or slug in self.permisos_efectivos)

    @property
    def ve_todo(self):
        return self.tiene_permiso('ver_todo')


class NotificacionInterna(models.Model):
    TIPO_ASIGNACION = 'asignacion'
    TIPO_TAREA = 'tarea'
    TIPO_LLAMADA_PERDIDA = 'llamada_perdida'
    TIPO_VENTA = 'venta'
    TIPO_ESTANCADO = 'estancado'
    TIPO_MENCION = 'mencion'
    TIPO_WHATSAPP = 'whatsapp'
    TIPO_LLAMADA_AJENA = 'llamada_ajena'
    TIPO_SISTEMA = 'sistema'
    TIPO_CHOICES = [
        (TIPO_ASIGNACION, 'Prospecto asignado'),
        (TIPO_TAREA, 'Tarea'),
        (TIPO_LLAMADA_PERDIDA, 'Llamada perdida'),
        (TIPO_VENTA, 'Venta concretada'),
        (TIPO_ESTANCADO, 'Prospecto estancado'),
        (TIPO_MENCION, 'Mención'),
        (TIPO_WHATSAPP, 'WhatsApp'),
        (TIPO_LLAMADA_AJENA, 'Llamada a un cliente tuyo'),
        (TIPO_SISTEMA, 'Sistema'),
    ]
    ICONOS = {
        TIPO_ASIGNACION: 'person-plus', TIPO_TAREA: 'calendar-check', TIPO_LLAMADA_PERDIDA: 'telephone-x',
        TIPO_VENTA: 'trophy', TIPO_ESTANCADO: 'hourglass-split', TIPO_MENCION: 'at',
        TIPO_WHATSAPP: 'whatsapp', TIPO_LLAMADA_AJENA: 'telephone-inbound', TIPO_SISTEMA: 'info-circle',
    }

    destinatario = models.ForeignKey(User, on_delete=models.CASCADE, related_name='notificaciones')
    tipo = models.CharField(max_length=30, choices=TIPO_CHOICES, default=TIPO_SISTEMA)
    titulo = models.CharField(max_length=200)
    cuerpo = models.TextField(blank=True)
    url = models.CharField(max_length=500, blank=True)
    leida = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['destinatario', 'leida', '-created_at'])]
        verbose_name = 'Notificación'
        verbose_name_plural = 'Notificaciones'

    def __str__(self):
        return f'{self.destinatario}: {self.titulo}'

    @property
    def icono(self):
        return self.ICONOS.get(self.tipo, 'bell')


class SesionConexion(models.Model):
    """Tramo en que el usuario tuvo el CRM abierto (para "tiempo conectado"). Se arma con el pulso de la pantalla."""
    usuario = models.ForeignKey(User, on_delete=models.CASCADE, related_name='sesiones')
    inicio = models.DateTimeField(db_index=True)
    ultimo = models.DateTimeField(help_text='Último pulso recibido (si no hay "fin", la sesión terminó acá).')
    fin = models.DateTimeField(null=True, blank=True, help_text='Salida explícita (botón Salir).')

    class Meta:
        ordering = ['-inicio']
        indexes = [models.Index(fields=['usuario', 'inicio'])]

    @property
    def termina(self):
        return self.fin or self.ultimo


class RegistroAuditoria(models.Model):
    """
    Bitácora append-only de acciones sensibles (C2). Vive en cada schema: en el de cada cliente quedan las
    acciones de ese cliente; en el público, las del panel de la plataforma. No se edita ni se borra desde la app
    (lo imponen save/delete). Para inmutabilidad a nivel base, se puede revocar UPDATE/DELETE al rol de la app.
    """
    LOGIN = 'login'
    LOGIN_FALLIDO = 'login_fallido'
    LOGOUT = 'logout'
    USUARIO_ALTA = 'usuario_alta'
    USUARIO_BAJA = 'usuario_baja'
    USUARIO_CAMBIO = 'usuario_cambio'
    CREDENCIAL = 'credencial'
    EXPORT = 'export'
    BORRADO_MASIVO = 'borrado_masivo'
    CLIENTE_SUSPENSION = 'cliente_suspension'
    CLIENTE_REACTIVACION = 'cliente_reactivacion'
    PAGO = 'pago'
    ACCION_CHOICES = [
        (LOGIN, 'Ingreso'), (LOGIN_FALLIDO, 'Ingreso fallido'), (LOGOUT, 'Salida'),
        (USUARIO_ALTA, 'Alta de usuario'), (USUARIO_BAJA, 'Baja de usuario'), (USUARIO_CAMBIO, 'Cambio de usuario'),
        (CREDENCIAL, 'Cambio de credenciales'), (EXPORT, 'Exportación'), (BORRADO_MASIVO, 'Borrado / acción masiva'),
        (CLIENTE_SUSPENSION, 'Suspensión de cliente'), (CLIENTE_REACTIVACION, 'Reactivación de cliente'),
        (PAGO, 'Pago'),
    ]

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    accion = models.CharField(max_length=25, choices=ACCION_CHOICES, db_index=True)
    usuario = models.ForeignKey('users.User', null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    usuario_desc = models.CharField(max_length=150, blank=True, help_text='Quién lo hizo (copia, por si se borra el usuario).')
    objeto = models.CharField(max_length=200, blank=True, help_text='Sobre qué (usuario, línea, export…).')
    detalle = models.TextField(blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['accion', '-created_at'])]
        verbose_name = 'Registro de auditoría'
        verbose_name_plural = 'Registros de auditoría'

    def __str__(self):
        return f'{self.created_at:%Y-%m-%d %H:%M} {self.accion} {self.usuario_desc}'

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValueError('El registro de auditoría es inmutable: no se puede modificar.')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError('El registro de auditoría es inmutable: no se puede borrar.')
