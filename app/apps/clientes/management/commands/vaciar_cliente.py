"""
Deja un cliente "en 0" (ej. después de una demo): borra leads, contactos, chats, llamadas, firmas, etc.
y desconecta/borra sus líneas de WhatsApp. Conserva la configuración: usuarios, embudos y etapas, campos,
plantillas, automatizaciones, formularios de firma, integraciones y la auditoría.

    python manage.py vaciar_cliente bernal              → muestra qué se borraría (no toca nada)
    python manage.py vaciar_cliente bernal --confirmar  → lo borra
"""
import os
import shutil

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django_tenants.utils import get_public_schema_name, tenant_context

from apps.clientes.models import Cliente, Dominio

# Orden de borrado: primero lo que depende de lo de abajo. (app_label, modelo)
DATOS = [
    ('firmas', 'SolicitudFirma'),
    ('cuentas', 'RecordatorioPersonal'), ('cuentas', 'AvisoRecurrente'), ('cuentas', 'TareaPostventa'),
    ('cuentas', 'Proyecto'), ('cuentas', 'Cuenta'),
    ('automatizaciones', 'EjecucionAccion'), ('automatizaciones', 'EmailEnviado'),
    ('automatizaciones', 'DifusionDestinatario'), ('automatizaciones', 'Difusion'),
    ('telefonia', 'CampaniaContacto'), ('telefonia', 'AgenteDiscador'), ('telefonia', 'CampaniaDiscado'),
    ('telefonia', 'EventoAnura'), ('telefonia', 'Llamada'), ('telefonia', 'PausaTelefono'),
    ('whatsapp', 'Mensaje'), ('whatsapp', 'Conversacion'), ('whatsapp', 'LogAPIWhatsApp'),
    ('crm', 'Tarea'), ('crm', 'Actividad'), ('crm', 'HistorialEtapa'), ('crm', 'Oportunidad'),
    ('crm', 'Contacto'), ('crm', 'ImportacionLote'),
    ('pautas', 'InversionPauta'), ('integraciones', 'LogIntegracion'),
    ('users', 'NotificacionInterna'), ('users', 'SesionConexion'),
]
# Carpetas de archivos del cliente que son datos (no configuración como logos o imágenes de email).
CARPETAS = ['whatsapp', 'documentos', 'importaciones', 'grabaciones']


class Command(BaseCommand):
    help = 'Borra los datos de un cliente (leads, chats, WhatsApp conectado…) conservando la configuración.'

    def add_arguments(self, parser):
        parser.add_argument('cliente', help='Subdominio, dominio o schema del cliente.')
        parser.add_argument('--confirmar', action='store_true', help='Borrar de verdad (sin esto solo muestra).')
        parser.add_argument('--conservar-lineas', action='store_true',
                            help='Desconecta WhatsApp pero deja las líneas configuradas.')

    def handle(self, cliente, confirmar=False, conservar_lineas=False, **opts):
        from django.apps import apps
        c = self._cliente(cliente)
        self.stdout.write(self.style.WARNING(f'Cliente: {c.nombre}  (schema {c.schema_name})'))
        with tenant_context(c):
            from apps.whatsapp.models import LineaWhatsApp
            modelos = [apps.get_model(a, m) for a, m in DATOS]
            total = 0
            for modelo in modelos:
                n = modelo.objects.count()
                total += n
                if n:
                    self.stdout.write(f'  {modelo._meta.verbose_name_plural}: {n}')
            lineas = list(LineaWhatsApp.objects.all())
            for linea in lineas:
                accion = 'desconectar' if conservar_lineas else 'desconectar y borrar'
                self.stdout.write(f'  Línea de WhatsApp "{linea}" ({linea.get_proveedor_display()}): {accion}')
            from core.storage import media_root
            raiz = media_root()
            carpetas = [os.path.join(raiz, d) for d in CARPETAS if os.path.isdir(os.path.join(raiz, d))]
            firmas = os.path.join(raiz, 'firmas')
            if os.path.isdir(firmas):
                carpetas += [os.path.join(firmas, d) for d in os.listdir(firmas) if d.isdigit()]
            if carpetas:
                self.stdout.write(f'  Carpetas de archivos: {len(carpetas)} ({", ".join(sorted({os.path.relpath(p, raiz).split(os.sep)[0] for p in carpetas}))})')
            if not total and not lineas and not carpetas:
                self.stdout.write(self.style.SUCCESS('No hay nada para borrar: el cliente ya está vacío.'))
                return
            if not confirmar:
                self.stdout.write(self.style.NOTICE(
                    '\nNo se borró nada. Para hacerlo: agregá --confirmar (se conservan usuarios, embudos, '
                    'campos, plantillas, automatizaciones y formularios).'))
                return

            # 1) WhatsApp afuera primero: que no entren mensajes nuevos mientras se borra.
            from apps.whatsapp.proveedores import get_proveedor
            for linea in lineas:
                prov = get_proveedor(linea)
                if hasattr(prov, 'desconectar'):
                    prov.desconectar()
                if not conservar_lineas and linea.proveedor == LineaWhatsApp.PROV_EVOLUTION:
                    try:
                        prov._http('DELETE', prov._url(f'/instance/delete/{prov.instancia}'),
                                   headers=prov._headers(), timeout=15)
                    except Exception as e:  # Evolution caído: igual se borra la línea del CRM
                        self.stdout.write(self.style.WARNING(f'  No se pudo borrar la instancia en Evolution: {e}'))
            # 2) Datos
            with transaction.atomic():
                for modelo in modelos:
                    modelo.objects.all().delete()
                if conservar_lineas:
                    LineaWhatsApp.objects.update(estado=LineaWhatsApp.ESTADO_DESCONECTADA)
                else:
                    LineaWhatsApp.objects.all().delete()
            # 3) Archivos y contadores en caché
            for p in carpetas:
                shutil.rmtree(p, ignore_errors=True)
            from apps.users.models import User
            from apps.whatsapp import services as wa
            wa.invalidar_no_leidos(list(User.objects.values_list('pk', flat=True)))
            wa.marcar_cambio_inbox()
        self.stdout.write(self.style.SUCCESS(f'Listo: {c.nombre} quedó en 0 ({total} registros borrados).'))

    def _cliente(self, valor):
        valor = valor.strip().lower()
        c = (Cliente.objects.filter(schema_name=valor).first()
             or Cliente.objects.filter(pk__in=Dominio.objects.filter(domain=valor).values('tenant')).first()
             or Cliente.objects.filter(pk__in=Dominio.objects.filter(domain__startswith=f'{valor}.')
                                       .values('tenant')).first())
        if c is None or c.schema_name == get_public_schema_name():
            raise CommandError(f'No encontré el cliente "{valor}". Usá el subdominio (ej. bernal) o el schema.')
        return c
