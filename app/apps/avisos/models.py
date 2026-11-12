"""Avisos internos: "cuando pasa X, avisarle a Y por Z" (campanita, email, WhatsApp)."""
from django.conf import settings
from django.db import models

EV_HUMANO = 'humano'
EV_DERIVADO_IA = 'derivado_ia'
EV_LEAD_NUEVO = 'lead_nuevo'
EV_SIN_ATENDER = 'sin_atender'
EV_VENTA = 'venta'
EV_FIRMA = 'firma'
EV_EVENTO_EXTERNO = 'evento_externo'
EV_LINEA_CAIDA = 'linea_caida'
EVENTOS = [
    (EV_HUMANO, 'El bot o el agente IA pasa un chat a una persona'),
    (EV_DERIVADO_IA, 'El agente IA deriva un lead calificado al cerrador'),
    (EV_LEAD_NUEVO, 'Entra un lead nuevo (formulario, WhatsApp, API; no importaciones)'),
    (EV_SIN_ATENDER, 'Un chat queda sin atender más de N minutos'),
    (EV_VENTA, 'Se cierra una venta'),
    (EV_FIRMA, 'Un lead firma un formulario'),
    (EV_EVENTO_EXTERNO, 'Llega un evento de un sistema externo'),
    (EV_LINEA_CAIDA, 'Se desconecta una línea de WhatsApp'),
]
# Texto por defecto de cada evento. Variables: {nombre} {telefono} {motivo} {etapa} {embudo} {vendedor} {linea} {link}
TEXTOS = {
    EV_HUMANO: '🙋 {nombre} ({telefono}) necesita que lo atienda una persona. Motivo: {motivo}\n{link}',
    EV_DERIVADO_IA: '✅ El agente IA derivó a {nombre} ({telefono}). {motivo}\n{link}',
    EV_LEAD_NUEVO: '🆕 Lead nuevo: {nombre} ({telefono}) en {embudo} · {motivo}\n{link}',
    EV_SIN_ATENDER: '⏰ El chat con {nombre} ({telefono}) lleva {motivo} sin atender.\n{link}',
    EV_VENTA: '🎉 ¡Venta! {nombre} · {embudo} · {vendedor}\n{link}',
    EV_FIRMA: '✍️ {nombre} firmó «{motivo}».\n{link}',
    EV_EVENTO_EXTERNO: '📥 Evento «{motivo}» de {nombre}.\n{link}',
    EV_LINEA_CAIDA: '⚠️ Se desconectó la línea de WhatsApp «{linea}». {motivo}',
}


class ReglaAviso(models.Model):
    nombre = models.CharField(max_length=100)
    activa = models.BooleanField(default=True)
    evento = models.CharField(max_length=20, choices=EVENTOS)
    embudo = models.ForeignKey('crm.Embudo', null=True, blank=True, on_delete=models.CASCADE, related_name='+',
                               help_text='Vacío = todos los embudos.')
    minutos = models.PositiveSmallIntegerField(default=15, help_text='Para "chat sin atender": cuántos minutos.')
    evento_nombre = models.CharField(max_length=60, blank=True, help_text='Para "evento externo": vacío = cualquiera.')

    al_vendedor = models.BooleanField(default=True, verbose_name='Al vendedor asignado')
    a_supervisores = models.BooleanField(default=False, verbose_name='A los supervisores del embudo')
    usuarios = models.ManyToManyField(settings.AUTH_USER_MODEL, blank=True, related_name='+', verbose_name='A estos usuarios')
    emails_extra = models.CharField(max_length=500, blank=True, verbose_name='Emails externos',
                                    help_text='Separados por coma (gente que no usa el CRM).')
    whatsapps_extra = models.CharField(max_length=500, blank=True, verbose_name='WhatsApp externos',
                                       help_text='Números separados por coma, ej. 5491155551234.')

    por_campanita = models.BooleanField(default=True, verbose_name='Campanita del CRM')
    por_email = models.BooleanField(default=False, verbose_name='Email')
    por_whatsapp = models.BooleanField(default=False, verbose_name='WhatsApp')
    linea = models.ForeignKey('whatsapp.LineaWhatsApp', null=True, blank=True, on_delete=models.SET_NULL,
                              related_name='+', verbose_name='Línea para los avisos por WhatsApp',
                              help_text='Ideal: una línea dedicada (no la comercial). Por QR: las oficiales solo '
                                        'pueden escribir si la persona les habló en las últimas 24 h.')
    texto = models.TextField(blank=True, help_text='Vacío = el texto por defecto del evento.')
    desde = models.TimeField(null=True, blank=True, verbose_name='Solo desde',
                             help_text='Horario para email y WhatsApp (la campanita avisa siempre).')
    hasta = models.TimeField(null=True, blank=True, verbose_name='Hasta')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['evento', 'nombre']
        verbose_name = 'Regla de aviso'

    def __str__(self):
        return self.nombre

    @property
    def texto_efectivo(self):
        return self.texto.strip() or TEXTOS.get(self.evento, '{nombre}')


class EnvioAviso(models.Model):
    """Registro de cada aviso por email / WhatsApp (la campanita ya queda en las notificaciones)."""
    regla = models.ForeignKey(ReglaAviso, null=True, blank=True, on_delete=models.SET_NULL, related_name='envios')
    evento = models.CharField(max_length=20)
    canal = models.CharField(max_length=10)
    destino = models.CharField(max_length=200)
    texto = models.TextField()
    ok = models.BooleanField(default=False)
    error = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
