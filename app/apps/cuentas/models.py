"""
Módulo de postventa (función extra 'postventa').

Cuando una venta se gana, el contacto puede pasar a ser una **Cuenta** (la empresa / cliente),
que agrupa uno o más contactos, sus **proyectos** (cada uno con su estado en un kanban
parametrizable, su monto y su fecha de entrega) y sus **tareas** de postventa.

Todo este módulo solo se ve/usa si el cliente tiene habilitada la función extra 'postventa'
(ver apps.clientes.models.FUNCIONES_EXTRA) y el usuario tiene el permiso 'postventa'.
"""
from django.db import models
from django.urls import reverse
from django.utils import timezone

from apps.users.models import User

COLORES = [
    ('#6B7280', 'Gris'), ('#3B7EF6', 'Azul'), ('#8B5CF6', 'Violeta'), ('#F59E0B', 'Ámbar'),
    ('#16A34A', 'Verde'), ('#EF4444', 'Rojo'), ('#0EA5E9', 'Celeste'), ('#EC4899', 'Rosa'),
]

# Columnas del kanban de proyectos que se crean la primera vez (después se editan desde el panel).
ESTADOS_PROYECTO_DEFAULT = [
    ('Nuevo', '#6B7280', False),
    ('En curso', '#3B7EF6', False),
    ('En revisión', '#F59E0B', False),
    ('Entregado', '#16A34A', True),
]


class Cuenta(models.Model):
    """Una empresa / cliente de postventa, asociada a uno o más contactos del CRM."""
    nombre = models.CharField('Empresa / cliente', max_length=150)
    contactos = models.ManyToManyField('crm.Contacto', blank=True, related_name='cuentas',
                                        verbose_name='Contactos')
    responsable = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name='cuentas', verbose_name='Responsable de la cuenta')
    email = models.EmailField(blank=True)
    telefono = models.CharField(max_length=40, blank=True, verbose_name='Teléfono')
    activa = models.BooleanField(default=True, help_text='Las cuentas dadas de baja no aparecen por defecto.')
    notas = models.TextField(blank=True)
    # La venta que la originó (si se creó automáticamente al ganar una oportunidad).
    origen_oportunidad = models.ForeignKey('crm.Oportunidad', null=True, blank=True,
                                           on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['nombre']
        verbose_name = 'Cuenta'
        verbose_name_plural = 'Cuentas'

    def __str__(self):
        return self.nombre

    def get_absolute_url(self):
        return reverse('cuentas:detalle', args=[self.pk])


class EstadoProyecto(models.Model):
    """Columna del kanban de proyectos. Parametrizable, como las etapas de un embudo."""
    nombre = models.CharField(max_length=80, unique=True)
    orden = models.PositiveSmallIntegerField(default=0)
    color = models.CharField(max_length=10, choices=COLORES, default='#3B7EF6')
    final = models.BooleanField('Estado final (entregado / cerrado)', default=False,
                                help_text='Los proyectos en este estado cuentan como terminados.')

    class Meta:
        ordering = ['orden', 'pk']
        verbose_name = 'Estado de proyecto'
        verbose_name_plural = 'Estados de proyecto'

    def __str__(self):
        return self.nombre


class Proyecto(models.Model):
    cuenta = models.ForeignKey(Cuenta, on_delete=models.CASCADE, related_name='proyectos')
    nombre = models.CharField(max_length=150)
    descripcion = models.TextField(blank=True)
    estado = models.ForeignKey(EstadoProyecto, null=True, blank=True, on_delete=models.SET_NULL,
                               related_name='proyectos')
    responsable = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name='proyectos', verbose_name='Responsable')
    monto = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True,
                                help_text='Opcional: valor del proyecto.')
    fecha_inicio = models.DateField(null=True, blank=True)
    fecha_entrega = models.DateField('Fecha de entrega', null=True, blank=True)
    orden = models.PositiveIntegerField(default=0)  # orden dentro de su columna del kanban
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['estado__orden', 'orden', '-created_at']
        verbose_name = 'Proyecto'
        verbose_name_plural = 'Proyectos'

    def __str__(self):
        return self.nombre

    def get_absolute_url(self):
        return reverse('cuentas:proyecto_editar', args=[self.pk])

    @property
    def entregado(self):
        return bool(self.estado and self.estado.final)

    @property
    def entrega_vencida(self):
        return bool(self.fecha_entrega and not self.entregado and self.fecha_entrega < timezone.localdate())


class TareaPostventa(models.Model):
    ESTADO_PENDIENTE = 'pendiente'
    ESTADO_COMPLETADA = 'completada'
    ESTADO_CANCELADA = 'cancelada'
    ESTADO_CHOICES = [(ESTADO_PENDIENTE, 'Pendiente'), (ESTADO_COMPLETADA, 'Completada'),
                      (ESTADO_CANCELADA, 'Cancelada')]

    cuenta = models.ForeignKey(Cuenta, on_delete=models.CASCADE, related_name='tareas')
    proyecto = models.ForeignKey(Proyecto, null=True, blank=True, on_delete=models.CASCADE,
                                 related_name='tareas')
    asignado_a = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name='tareas_postventa', verbose_name='Asignada a')
    creada_por = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    titulo = models.CharField(max_length=200)
    descripcion = models.TextField(blank=True)
    vence_at = models.DateTimeField('Vence', null=True, blank=True)
    estado = models.CharField(max_length=12, choices=ESTADO_CHOICES, default=ESTADO_PENDIENTE)
    completada_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['estado', 'vence_at', '-created_at']
        verbose_name = 'Tarea de postventa'
        verbose_name_plural = 'Tareas de postventa'

    def __str__(self):
        return self.titulo

    @property
    def vencida(self):
        return self.estado == self.ESTADO_PENDIENTE and self.vence_at is not None and self.vence_at < timezone.now()


class AvisoRecurrente(models.Model):
    """
    Aviso de WhatsApp que se manda solo cada N meses (mensualidades, recordatorios de suscripción, etc.).
    Se puede usar una plantilla aprobada (con variables) o texto libre con {primer_nombre}, {empresa}…
    En líneas oficiales (Meta/Twilio) con la ventana de 24 h cerrada hay que usar plantilla; por QR (Evolution)
    el texto libre sale igual.
    """
    cuenta = models.ForeignKey(Cuenta, on_delete=models.CASCADE, related_name='avisos')
    contacto = models.ForeignKey('crm.Contacto', null=True, blank=True, on_delete=models.SET_NULL, related_name='+',
                                 verbose_name='Enviar a', help_text='Vacío = el primer contacto de la cuenta.')
    linea = models.ForeignKey('whatsapp.LineaWhatsApp', null=True, blank=True, on_delete=models.SET_NULL,
                              related_name='+', verbose_name='Línea de WhatsApp',
                              help_text='Vacío = se elige una línea activa automáticamente.')
    plantilla = models.ForeignKey('whatsapp.Plantilla', null=True, blank=True, on_delete=models.SET_NULL,
                                  related_name='+', help_text='Opcional. Si la cargás, se usa en vez del texto libre.')
    mensaje = models.TextField(blank=True, verbose_name='Texto libre',
                               help_text='Acepta variables: {primer_nombre}, {nombre}, {empresa}…')
    cada_meses = models.PositiveSmallIntegerField(default=1, verbose_name='Cada cuántos meses',
                                                  help_text='1 = todos los meses, 2 = bimestral, 12 = anual.')
    dia_mes = models.PositiveSmallIntegerField(default=1, verbose_name='Día del mes',
                                               help_text='1 a 28. Si el mes no llega a ese día, se manda el último.')
    proximo_envio = models.DateField(verbose_name='Próximo envío')
    activo = models.BooleanField(default=True)
    ultimo_envio_at = models.DateTimeField(null=True, blank=True)
    ultimo_detalle = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['proximo_envio']
        verbose_name = 'Aviso recurrente'
        verbose_name_plural = 'Avisos recurrentes'

    def __str__(self):
        return f'{self.cuenta} · cada {self.cada_meses} mes(es)'
