from django.db import migrations
from django.db.models import F

RESULTADOS = [
    # nombre, contactado, pide_fecha
    ('Interesado, sigue la negociación', True, False),
    ('Pidió que lo llamen más tarde', True, True),
    ('Pidió info por WhatsApp / email', True, False),
    ('No le interesa', True, False),
    ('Atendió otra persona', False, True),
    ('Cortó / no quiso hablar', False, False),
    ('Número equivocado', False, False),
]


def crear(apps, schema_editor):
    Resultado = apps.get_model('crm', 'ResultadoGestion')
    if not Resultado.objects.exists():
        for i, (nombre, contactado, pide_fecha) in enumerate(RESULTADOS, start=1):
            Resultado.objects.create(nombre=nombre, contactado=contactado, pide_fecha=pide_fecha, orden=i)
    # Las llamadas anteriores a esta función no quedan como "sin calificar"
    Llamada = apps.get_model('telefonia', 'Llamada')
    Llamada.objects.filter(calificada_at__isnull=True).update(calificada_at=F('inicio_at'))


class Migration(migrations.Migration):
    dependencies = [
        ('telefonia', '0007_llamada_calificada_at_llamada_calificada_por_and_more'),
        ('crm', '0016_embudo_etiqueta_venta_embudo_exigir_resultado_and_more'),
    ]
    operations = [migrations.RunPython(crear, migrations.RunPython.noop)]
