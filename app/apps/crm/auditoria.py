"""
Registro de cambios de datos en la actividad de la tarjeta: qué campo, valor anterior → nuevo, y quién.

Uso:  antes = foto_contacto(contacto) … guardar … registrar_cambios(antes, foto_contacto(contacto), usuario, contacto, op)
"""
from datetime import date, datetime
from decimal import Decimal

from django.utils import timezone

from .models import Actividad, CampoPersonalizado, Contacto, Oportunidad

CAMPOS_CONTACTO = ['nombre', 'telefono', 'telefono_alternativo', 'email', 'dni', 'fecha_nacimiento', 'localidad',
                   'provincia', 'fecha_atencion', 'especialidad_atencion', 'no_contactar']
CAMPOS_OPORTUNIDAD = ['valor', 'pauta', 'origen_pauta', 'fuente']


def mostrar(v):
    if v is None or v == '' or v == []:
        return '—'
    if isinstance(v, bool):
        return 'Sí' if v else 'No'
    if isinstance(v, datetime):
        return timezone.localtime(v).strftime('%d/%m/%Y %H:%M')
    if isinstance(v, date):
        return v.strftime('%d/%m/%Y')
    if isinstance(v, Decimal):
        return f'{v:,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.').removesuffix(',00')
    if isinstance(v, (list, tuple, set)):
        return ', '.join(str(x) for x in v) or '—'
    return str(v)


ETIQUETAS = {'telefono_alternativo': 'Teléfono alternativo', 'fecha_nacimiento': 'Fecha de nacimiento',
             'fecha_atencion': 'Fecha de atención', 'especialidad_atencion': 'Especialidad de atención',
             'valor': 'Valor', 'pauta': 'Pauta', 'origen_pauta': 'Origen', 'fuente': 'Fuente'}


def _label(modelo, campo):
    if campo in ETIQUETAS:
        return ETIQUETAS[campo]
    return str(modelo._meta.get_field(campo).verbose_name).capitalize()


def foto_contacto(contacto):
    """{clave: (etiqueta, valor mostrable)} de los datos editables del contacto, incluidos campos personalizados."""
    foto = {c: (_label(Contacto, c), mostrar(getattr(contacto, c))) for c in CAMPOS_CONTACTO}
    if contacto.pk:
        foto['etiquetas'] = ('Etiquetas', mostrar(sorted(e.nombre for e in contacto.etiquetas.all())))
    extra = contacto.datos_extra or {}
    for cp in CampoPersonalizado.objects.all():
        foto[f'cp:{cp.slug}'] = (cp.nombre, mostrar(cp.valor_display(extra.get(cp.slug))))
    return foto


def foto_oportunidad(op):
    return {c: (_label(Oportunidad, c), mostrar(getattr(op, c))) for c in CAMPOS_OPORTUNIDAD}


def diferencias(antes, despues):
    cambios = []
    for clave, (label, nuevo) in despues.items():
        viejo = antes.get(clave, (label, '—'))[1]
        if viejo != nuevo:
            cambios.append({'campo': clave, 'label': label, 'antes': viejo, 'despues': nuevo})
    return cambios


def registrar_cambios(antes, despues, usuario, contacto, oportunidad=None, contexto=''):
    """Crea la actividad "Cambio de datos" si algo cambió. Devuelve la actividad o None."""
    cambios = diferencias(antes, despues)
    if not cambios:
        return None
    if oportunidad is None:
        from .services import oportunidad_activa_de
        oportunidad = oportunidad_activa_de(contacto)
    texto = '\n'.join(f'{c["label"]}: {c["antes"]} → {c["despues"]}' for c in cambios)
    return Actividad.objects.create(
        contacto=contacto, oportunidad=oportunidad, tipo=Actividad.TIPO_CAMBIO, usuario=usuario,
        texto=(f'{contexto}\n' if contexto else '') + texto, datos={'cambios': cambios, 'contexto': contexto},
    )
