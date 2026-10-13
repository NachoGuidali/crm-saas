from django.contrib import admin

from .models import Actividad, Contacto, Embudo, Etapa, Etiqueta, ImportacionLote, Oportunidad, Tarea, Tipificacion


class EtapaInline(admin.TabularInline):
    model = Etapa
    extra = 0


@admin.register(Embudo)
class EmbudoAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'activo', 'modo_asignacion')
    inlines = [EtapaInline]
    filter_horizontal = ('agentes', 'supervisores')


@admin.register(Contacto)
class ContactoAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'telefono', 'email', 'no_contactar', 'created_at')
    search_fields = ('nombre', 'telefono', 'email', 'dni')


@admin.register(Oportunidad)
class OportunidadAdmin(admin.ModelAdmin):
    list_display = ('contacto', 'embudo', 'etapa', 'estado', 'agente', 'origen', 'created_at')
    list_filter = ('embudo', 'estado', 'origen')
    search_fields = ('contacto__nombre', 'contacto__telefono')
    raw_id_fields = ('contacto',)


admin.site.register(Tipificacion)
admin.site.register(Etiqueta)
admin.site.register(Tarea)
admin.site.register(Actividad)
admin.site.register(ImportacionLote)
