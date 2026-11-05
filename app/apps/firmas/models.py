"""
Firmas electrónicas: formularios que el lead completa y firma (con el dedo o el mouse) desde un link.

- FormularioFirma: el modelo configurable (título, logo, colores, texto del documento y campos).
- SolicitudFirma: cada envío a un lead. Tiene su link único, guarda una copia de lo que se le mostró
  (así editar el formulario después no cambia lo ya firmado) y, al firmar, las respuestas, las firmas,
  el PDF y la evidencia (fecha, IP, dispositivo y huella SHA-256).

Es una función extra: se habilita por plan o por cliente con la clave 'firmas' (apps.clientes.FUNCIONES_EXTRA).
"""
import secrets
from datetime import timedelta

from django.db import models
from django.utils import timezone

FUNCION = 'firmas'


class FormularioFirma(models.Model):
    # Tipos de campo del formulario
    T_TEXTO, T_TEXTO_LARGO, T_NUMERO, T_FECHA = 'texto', 'texto_largo', 'numero', 'fecha'
    T_EMAIL, T_TELEFONO, T_SINO, T_LISTA = 'email', 'telefono', 'sino', 'lista'
    T_ACEPTO, T_FIRMA, T_TITULO = 'acepto', 'firma', 'titulo'
    TIPOS_CAMPO = [
        (T_TEXTO, 'Texto corto'), (T_TEXTO_LARGO, 'Texto largo'), (T_NUMERO, 'Número'), (T_FECHA, 'Fecha'),
        (T_EMAIL, 'Email'), (T_TELEFONO, 'Teléfono'), (T_SINO, 'Sí / No'), (T_LISTA, 'Lista de opciones'),
        (T_ACEPTO, 'Casilla "Acepto"'), (T_FIRMA, 'Firma (dedo o mouse)'), (T_TITULO, 'Subtítulo / texto'),
    ]

    nombre = models.CharField(max_length=120, help_text='Nombre interno, ej. "Declaración jurada de salud".')
    titulo = models.CharField(max_length=200, help_text='Lo que ve el lead arriba de todo.')
    documento = models.TextField(
        blank=True, verbose_name='Texto del documento',
        help_text='Se muestra antes de los campos y queda en el PDF. Variables: {nombre} {dni} {email} '
                  '{telefono} {agente} {embudo} y los campos personalizados ({slug}).')
    logo = models.ImageField(upload_to='firmas/publico/logos/', blank=True)
    color_fondo = models.CharField(max_length=9, default='#F1F5F9', verbose_name='Color de fondo')
    color_principal = models.CharField(max_length=9, default='#2563EB', verbose_name='Color principal (botones)')
    campos = models.JSONField(default=list, blank=True)
    texto_final = models.CharField(max_length=300, default='¡Listo! Recibimos tu formulario firmado. Gracias.',
                                   verbose_name='Mensaje al terminar')
    vence_dias = models.PositiveSmallIntegerField(default=7, verbose_name='El link vence a los (días)',
                                                  help_text='0 = no vence.')
    enviar_copia = models.BooleanField(default=True, verbose_name='Mandarle el PDF firmado al lead por email')
    asunto_email = models.CharField(max_length=200, default='Completá y firmá: {formulario}',
                                    verbose_name='Asunto del email')
    texto_email = models.TextField(
        default='Hola {primer_nombre}, para avanzar necesitamos que completes y firmes este formulario:\n\n{link}\n\n'
                'Te lleva un minuto y lo podés hacer desde el celular.',
        verbose_name='Texto del email', help_text='{link} = el link al formulario. Acepta las mismas variables.')
    texto_whatsapp = models.TextField(
        default='Hola {primer_nombre}, para avanzar completá y firmá este formulario desde el celu: {link}',
        verbose_name='Texto del WhatsApp')
    activo = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['nombre']
        verbose_name = 'Formulario de firma'
        verbose_name_plural = 'Formularios de firma'

    def __str__(self):
        return self.nombre

    @property
    def campos_firma(self):
        return [c for c in self.campos if c.get('tipo') == self.T_FIRMA]


class SolicitudFirma(models.Model):
    ESTADO_PENDIENTE = 'pendiente'
    ESTADO_ABIERTA = 'abierta'
    ESTADO_FIRMADA = 'firmada'
    ESTADO_ANULADA = 'anulada'
    ESTADO_CHOICES = [(ESTADO_PENDIENTE, 'Enviada'), (ESTADO_ABIERTA, 'Abierta'), (ESTADO_FIRMADA, 'Firmada'),
                      (ESTADO_ANULADA, 'Anulada')]
    ORIGEN_CHOICES = [('manual', 'Manual'), ('automatizacion', 'Automatización'), ('api', 'Bot / API')]

    formulario = models.ForeignKey(FormularioFirma, on_delete=models.PROTECT, related_name='solicitudes')
    oportunidad = models.ForeignKey('crm.Oportunidad', on_delete=models.CASCADE, related_name='firmas')
    contacto = models.ForeignKey('crm.Contacto', on_delete=models.CASCADE, related_name='firmas')
    token = models.CharField(max_length=64, unique=True, editable=False)
    estado = models.CharField(max_length=10, choices=ESTADO_CHOICES, default=ESTADO_PENDIENTE, db_index=True)
    origen = models.CharField(max_length=15, choices=ORIGEN_CHOICES, default='manual')
    creada_por = models.ForeignKey('users.User', null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    accion = models.ForeignKey('automatizaciones.AccionEtapa', null=True, blank=True, on_delete=models.SET_NULL,
                               related_name='+')
    # Copia de lo que se le mostró (título, documento con variables reemplazadas, campos, colores, valores iniciales)
    snapshot = models.JSONField(default=dict)
    respuestas = models.JSONField(default=dict, blank=True)
    firmas = models.JSONField(default=dict, blank=True, help_text='{id_campo: ruta del PNG}')
    pdf = models.CharField(max_length=300, blank=True, help_text='Ruta del PDF firmado en el storage del cliente.')
    huella = models.CharField(max_length=64, blank=True, help_text='SHA-256 del contenido firmado.')
    huella_pdf = models.CharField(max_length=64, blank=True, help_text='SHA-256 del archivo PDF.')
    ip = models.GenericIPAddressField(null=True, blank=True)
    dispositivo = models.CharField(max_length=300, blank=True)
    enviada_email = models.BooleanField(default=False)
    enviada_whatsapp = models.BooleanField(default=False)
    vence_at = models.DateTimeField(null=True, blank=True)
    abierta_at = models.DateTimeField(null=True, blank=True)
    firmada_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Solicitud de firma'
        verbose_name_plural = 'Solicitudes de firma'

    def __str__(self):
        return f'{self.formulario} · {self.contacto}'

    def save(self, *args, **kwargs):
        if not self.token:
            self.token = secrets.token_urlsafe(32)
        if self.vence_at is None and self.pk is None and self.formulario.vence_dias:
            self.vence_at = timezone.now() + timedelta(days=self.formulario.vence_dias)
        super().save(*args, **kwargs)

    @property
    def vencida(self):
        return self.estado in (self.ESTADO_PENDIENTE, self.ESTADO_ABIERTA) and bool(
            self.vence_at and self.vence_at < timezone.now())

    @property
    def firmable(self):
        return self.estado in (self.ESTADO_PENDIENTE, self.ESTADO_ABIERTA) and not self.vencida

    @property
    def estado_display(self):
        return 'Vencida' if self.vencida else self.get_estado_display()

    @property
    def url(self):
        from django.urls import reverse
        from core import tenancy
        return tenancy.url_sitio() + reverse('firmas_publico:formulario', args=[self.token])
