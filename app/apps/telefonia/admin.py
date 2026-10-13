from django.contrib import admin

from .models import CampaniaContacto, CampaniaDiscado, ConfigAnura, InternoAnura, Llamada, RutaAnura

admin.site.register(ConfigAnura)
admin.site.register(InternoAnura)
admin.site.register(RutaAnura)
admin.site.register(CampaniaDiscado)
admin.site.register(CampaniaContacto)


@admin.register(Llamada)
class LlamadaAdmin(admin.ModelAdmin):
    list_display = ('call_id', 'direccion', 'numero', 'estado', 'agente', 'duracion_seg', 'inicio_at')
    list_filter = ('direccion', 'estado')
    raw_id_fields = ('contacto', 'oportunidad', 'campania_contacto')
