from django.contrib.postgres.operations import TrigramExtension
from django.db import migrations


class Migration(migrations.Migration):
    """
    pg_trgm (búsqueda por nombre/email) va en el schema público, así la ven todos los clientes.
    Si la creara la migración del CRM, quedaría dentro del schema del primer cliente y el resto no la vería.
    """

    dependencies = [('clientes', '0001_initial')]

    operations = [TrigramExtension()]
