from django.contrib import admin

from .models import InversionPauta, Pauta


class InversionInline(admin.TabularInline):
    model = InversionPauta
    extra = 0


@admin.register(Pauta)
class PautaAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'plataforma', 'activa', 'inicio', 'fin')
    inlines = [InversionInline]
