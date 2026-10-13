from django.contrib import admin

from .models import Conversacion, LineaWhatsApp, LogAPIWhatsApp, Mensaje, Plantilla, RespuestaRapida

admin.site.register(LineaWhatsApp)
admin.site.register(Plantilla)
admin.site.register(RespuestaRapida)


@admin.register(Conversacion)
class ConversacionAdmin(admin.ModelAdmin):
    list_display = ('telefono', 'linea', 'contacto', 'agente', 'estado', 'ultimo_mensaje_at')
    raw_id_fields = ('contacto',)


@admin.register(Mensaje)
class MensajeAdmin(admin.ModelAdmin):
    list_display = ('conversacion', 'direccion', 'tipo', 'status', 'timestamp')
    raw_id_fields = ('conversacion',)


@admin.register(LogAPIWhatsApp)
class LogAdmin(admin.ModelAdmin):
    list_display = ('linea', 'metodo', 'endpoint', 'status', 'exitoso', 'created_at')
