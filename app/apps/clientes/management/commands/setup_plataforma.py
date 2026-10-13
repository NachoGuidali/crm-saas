"""
Deja lista la plataforma (idempotente, corre en cada arranque): el "cliente" público con el dominio principal
y el superusuario del panel. Las migraciones las corre antes `migrate_schemas`.
"""
import os
import secrets

from django.conf import settings
from django.core.management.base import BaseCommand
from django_tenants.utils import get_public_schema_name, schema_context


class Command(BaseCommand):
    help = 'Crea el schema público con el dominio principal y el superusuario del panel de la plataforma.'

    def handle(self, *args, **opts):
        from apps.clientes.models import Cliente, Dominio
        from apps.users.models import User

        publico, creado = Cliente.objects.get_or_create(
            schema_name=get_public_schema_name(),
            defaults={'nombre': settings.CRM_NOMBRE, 'estado': Cliente.ESTADO_ACTIVO},
        )
        for dominio in (settings.SAAS_DOMINIO, f'www.{settings.SAAS_DOMINIO}'):
            Dominio.objects.get_or_create(domain=dominio, defaults={
                'tenant': publico, 'is_primary': dominio == settings.SAAS_DOMINIO})
        self.stdout.write(self.style.SUCCESS(f'Plataforma en {settings.SAAS_DOMINIO} '
                                             f'{"creada" if creado else "verificada"}.'))

        with schema_context(get_public_schema_name()):
            if User.objects.filter(is_superuser=True).exists():
                return
            username = os.environ.get('ADMIN_USERNAME', 'admin')
            password = os.environ.get('ADMIN_PASSWORD') or secrets.token_urlsafe(10)
            User.objects.create_superuser(username=username, email=os.environ.get('ADMIN_EMAIL', ''),
                                          password=password, rol=User.ROL_ADMIN, first_name='Plataforma')
            self.stdout.write(self.style.WARNING(
                f'Superusuario del panel creado → usuario: {username}  contraseña: {password}'))
