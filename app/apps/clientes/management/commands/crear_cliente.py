from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from core.tenancy import dominio_para


class Command(BaseCommand):
    help = 'Da de alta un cliente del SaaS (lo mismo que "Nuevo cliente" en el panel).'

    def add_arguments(self, parser):
        parser.add_argument('subdominio')
        parser.add_argument('--nombre', required=True, help='Nombre de la empresa.')
        parser.add_argument('--email', required=True, help='Email del administrador del CRM.')
        parser.add_argument('--admin-nombre', default='')
        parser.add_argument('--usuario', default='admin')
        parser.add_argument('--password', default=None, help='Vacío = temporal (se pide cambiarla al ingresar).')
        parser.add_argument('--plan', default=None, help='Nombre del plan.')

    def handle(self, *args, **o):
        from apps.clientes.models import Plan
        from apps.clientes.services import crear_cliente
        plan = None
        if o['plan']:
            plan = Plan.objects.filter(nombre__iexact=o['plan']).first()
            if plan is None:
                raise CommandError(f'No existe el plan "{o["plan"]}".')
        try:
            cliente, password = crear_cliente(
                nombre=o['nombre'], subdominio=o['subdominio'], admin_email=o['email'],
                admin_nombre=o['admin_nombre'], admin_usuario=o['usuario'], admin_password=o['password'], plan=plan,
            )
        except ValidationError as e:
            raise CommandError(' '.join(e.messages))
        self.stdout.write(self.style.SUCCESS(
            f'Cliente "{cliente}" creado en {dominio_para(o["subdominio"])} → usuario: {o["usuario"]}  '
            f'contraseña: {password}'))
