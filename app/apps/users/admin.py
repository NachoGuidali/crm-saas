from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import NotificacionInterna, RolPersonalizado, User


@admin.register(User)
class UsuarioAdmin(UserAdmin):
    list_display = ('username', 'get_full_name', 'email', 'rol', 'rol_custom', 'disponible', 'is_active')
    list_filter = ('rol', 'is_active', 'disponible')
    fieldsets = UserAdmin.fieldsets + (
        ('CRM', {'fields': ('rol', 'rol_custom', 'telefono', 'avatar', 'disponible', 'debe_cambiar_password')}),
    )


admin.site.register(RolPersonalizado)
admin.site.register(NotificacionInterna)
