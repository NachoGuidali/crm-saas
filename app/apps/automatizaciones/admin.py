from django.contrib import admin

from .models import AccionEtapa, EjecucionAccion

admin.site.register(AccionEtapa)


@admin.register(EjecucionAccion)
class EjecucionAdmin(admin.ModelAdmin):
    list_display = ('accion', 'oportunidad', 'estado', 'programada_para', 'ejecutada_at')
    list_filter = ('estado',)
    raw_id_fields = ('oportunidad', 'historial')
