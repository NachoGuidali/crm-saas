from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.operations import TrigramExtension
from django.db import migrations


class Migration(migrations.Migration):
    """Índices trigram: búsqueda por nombre/email con ILIKE rápida aunque haya cientos de miles de contactos."""

    dependencies = [('crm', '0003_initial')]

    operations = [
        TrigramExtension(),
        migrations.AddIndex('contacto', GinIndex(fields=['nombre'], name='contacto_nombre_trgm',
                                                 opclasses=['gin_trgm_ops'])),
        migrations.AddIndex('contacto', GinIndex(fields=['email'], name='contacto_email_trgm',
                                                 opclasses=['gin_trgm_ops'])),
    ]
