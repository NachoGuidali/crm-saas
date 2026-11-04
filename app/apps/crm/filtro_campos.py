"""
Filtro avanzado por campo: "Edad es mayor que 30", "Localidad contiene Quilmes", "Es socio = Sí"…

Sirve para los campos originales del contacto y para todos los campos personalizados. Cada condición viaja en
el parámetro `cf` (repetible) como `campo|operador|valor`, así funciona igual en tablero, lista, exportación y
acciones masivas (todos pasan por filtrar_oportunidades). Varias condiciones se combinan con Y.

  campo:  `c:<campo del contacto>` (ej. c:localidad, c:edad) o `cp:<slug del personalizado>`.
  operador: los mismos de las automatizaciones (CondicionCampo.OPERADORES).
"""
import re
from datetime import date

from django.db.models import Case, DateField, FloatField, Q, When
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast, Substr
from django.utils import timezone

PARAM = 'cf'

OP_IGUAL, OP_DISTINTO = '=', '!='
OP_MAYOR, OP_MAYOR_IGUAL, OP_MENOR, OP_MENOR_IGUAL = '>', '>=', '<', '<='
OP_CONTIENE, OP_LLENO, OP_VACIO = 'contiene', 'lleno', 'vacio'
COMPARACION = (OP_MAYOR, OP_MAYOR_IGUAL, OP_MENOR, OP_MENOR_IGUAL)
SIN_VALOR = (OP_LLENO, OP_VACIO)

# Campos propios del contacto: (clave, etiqueta, tipo). "edad" es virtual: sale de la fecha de nacimiento.
CAMPOS_BASE = [
    ('nombre', 'Nombre y apellido', 'texto'),
    ('telefono', 'Teléfono', 'telefono'),
    ('email', 'Email', 'email'),
    ('dni', 'DNI', 'texto'),
    ('edad', 'Edad (por fecha de nacimiento)', 'numero'),
    ('fecha_nacimiento', 'Fecha de nacimiento', 'fecha'),
    ('localidad', 'Localidad', 'texto'),
    ('provincia', 'Provincia', 'texto'),
    ('fecha_atencion', 'Fecha de atención', 'fecha'),
    ('especialidad_atencion', 'Motivo / especialidad', 'texto'),
]
_BASE = {k: (label, tipo) for k, label, tipo in CAMPOS_BASE}

_RE_NUM = r'^\s*-?[0-9]+([.,][0-9]+)?\s*$'
_SI = ['true', '1', 'si', 'sí', 'Si', 'Sí', 'SI', 'yes']
_NO = ['false', '0', 'no', 'No', 'NO']
_RE_FECHA = r'^[0-9]{4}-[0-9]{2}-[0-9]{2}'


def campos_disponibles(embudo=None):
    """[(clave, etiqueta, tipo, opciones)] para armar el selector de campo en pantalla."""
    from .models import CampoPersonalizado as C
    campos = [(f'c:{k}', label, tipo, []) for k, label, tipo in CAMPOS_BASE]
    campos += [(f'cp:{c.slug}', c.nombre, c.tipo, c.opciones or [])
               for c in C.activos(embudo) if c.tipo != C.TIPO_ARCHIVO]
    return campos


def _definicion(clave, personalizados):
    """(etiqueta, tipo) del campo, o None si no existe."""
    if clave.startswith('c:'):
        return _BASE.get(clave[2:])
    if clave.startswith('cp:'):
        c = personalizados.get(clave[3:])
        return (c.nombre, c.tipo) if c else None
    return None


def parsear(params):
    """Condiciones válidas del request: [(clave, operador, valor, texto_crudo)]."""
    from .services import valores_filtro
    conds = []
    for crudo in valores_filtro(params, PARAM):
        partes = crudo.split('|', 2)
        if len(partes) < 2:
            continue
        clave, op = partes[0], partes[1]
        valor = partes[2].strip() if len(partes) > 2 else ''
        if op not in SIN_VALOR and valor == '':
            continue
        conds.append((clave, op, valor, crudo))
    return conds


def _personalizados(conds):
    from .models import CampoPersonalizado
    slugs = [c[0][3:] for c in conds if c[0].startswith('cp:')]
    return {c.slug: c for c in CampoPersonalizado.objects.filter(slug__in=slugs)} if slugs else {}


def _numero(valor):
    try:
        return float(str(valor).replace(',', '.'))
    except (TypeError, ValueError):
        return None


def _fecha(valor):
    try:
        return date.fromisoformat(str(valor)[:10])
    except ValueError:
        return None


def _hace_anios(hoy, anios):
    try:
        return hoy.replace(year=hoy.year - anios)
    except ValueError:  # 29/2 en año no bisiesto
        return hoy.replace(year=hoy.year - anios, day=28)


def _q_comparar(campo, op, valor):
    """Q para =, !=, >, >=, <, <= sobre un campo ya comparable (número o fecha). Los vacíos solo entran en !=."""
    if op == OP_IGUAL:
        return Q(**{campo: valor})
    if op == OP_DISTINTO:
        return Q(**{f'{campo}__isnull': True}) | ~Q(**{campo: valor}) & Q(**{f'{campo}__isnull': False})
    sufijo = {OP_MAYOR: 'gt', OP_MAYOR_IGUAL: 'gte', OP_MENOR: 'lt', OP_MENOR_IGUAL: 'lte'}.get(op)
    return Q(**{f'{campo}__{sufijo}': valor}) if sufijo else None


def _q_texto(campo, op, valor, nulo):
    """Q para campos de texto. `nulo`: si el campo puede venir NULL (JSON) además de ''."""
    es_nulo = Q(**{f'{campo}__isnull': True})
    vacio = (Q(**{campo: ''}) | es_nulo) if nulo else Q(**{campo: ''})
    if op == OP_VACIO:
        return vacio
    if op == OP_LLENO:
        return (~Q(**{campo: ''}) & ~es_nulo) if nulo else ~Q(**{campo: ''})
    if op == OP_CONTIENE:
        return Q(**{f'{campo}__icontains': valor})
    if op == OP_IGUAL:
        return Q(**{f'{campo}__iexact': valor})
    if op == OP_DISTINTO:
        distinto = ~Q(**{f'{campo}__iexact': valor})
        return (es_nulo | distinto) if nulo else distinto
    return None


def _q_edad(op, valor):
    """Edad sobre contacto__fecha_nacimiento: "edad > 30" = nació hace 31 años o más."""
    n = _numero(valor)
    if op in SIN_VALOR:
        return Q(contacto__fecha_nacimiento__isnull=(op == OP_VACIO))
    if n is None:
        return None
    n = int(n)
    hoy = timezone.localdate()
    f = 'contacto__fecha_nacimiento'
    # edad >= n  ⇔  nacimiento <= hoy - n años
    if op == OP_MAYOR_IGUAL:
        return Q(**{f'{f}__lte': _hace_anios(hoy, n)})
    if op == OP_MAYOR:
        return Q(**{f'{f}__lte': _hace_anios(hoy, n + 1)})
    if op == OP_MENOR:
        return Q(**{f'{f}__gt': _hace_anios(hoy, n)})
    if op == OP_MENOR_IGUAL:
        return Q(**{f'{f}__gt': _hace_anios(hoy, n + 1)})
    igual = Q(**{f'{f}__gt': _hace_anios(hoy, n + 1), f'{f}__lte': _hace_anios(hoy, n)})
    if op == OP_IGUAL:
        return igual
    if op == OP_DISTINTO:
        return Q(**{f'{f}__isnull': True}) | (~igual & Q(**{f'{f}__isnull': False}))
    return None


def aplicar(qs, params):
    """Aplica al queryset de Oportunidad las condiciones `cf` del request. Las inválidas se ignoran."""
    conds = parsear(params)
    if not conds:
        return qs
    personalizados = _personalizados(conds)
    for i, (clave, op, valor, _) in enumerate(conds):
        defin = _definicion(clave, personalizados)
        if not defin:
            continue
        _, tipo = defin
        q = None
        if clave == 'c:edad':
            q = _q_edad(op, valor)
        elif clave.startswith('c:'):
            campo = f'contacto__{clave[2:]}'
            if tipo == 'fecha':
                if op in SIN_VALOR:
                    q = Q(**{f'{campo}__isnull': op == OP_VACIO})
                elif _fecha(valor):
                    q = _q_comparar(campo, op, _fecha(valor))
            else:
                q = _q_texto(campo, op, valor, nulo=False)
        else:
            slug = clave[3:]
            texto = f'_cf{i}t'
            qs = qs.alias(**{texto: KeyTextTransform(slug, 'contacto__datos_extra')})
            if op in SIN_VALOR:
                q = _q_texto(texto, op, valor, nulo=True)
            elif tipo == 'sino':
                si = str(valor).strip().lower() in ('true', '1', 'si', 'sí', 'yes')
                igual = Q(**{f'{texto}__in': _SI if si else _NO})
                q = igual if op != OP_DISTINTO else (Q(**{f'{texto}__isnull': True}) | ~igual)
            elif tipo == 'numero' and op != OP_CONTIENE:
                n = _numero(valor)
                if n is not None:
                    num = f'_cf{i}n'
                    qs = qs.alias(**{num: Case(
                        When(**{f'{texto}__regex': _RE_NUM},
                             then=Cast(_reemplazar_coma(texto), FloatField())),
                        default=None, output_field=FloatField())})
                    q = _q_comparar(num, op, n)
            elif tipo == 'fecha' and op != OP_CONTIENE:
                f = _fecha(valor)
                if f:
                    fec = f'_cf{i}f'
                    qs = qs.alias(**{fec: Case(
                        When(**{f'{texto}__regex': _RE_FECHA}, then=Cast(Substr(texto, 1, 10), DateField())),
                        default=None, output_field=DateField())})
                    q = _q_comparar(fec, op, f)
            elif op not in COMPARACION:
                q = _q_texto(texto, op, valor, nulo=True)
        if q is not None:
            qs = qs.filter(q)
    return qs


def _reemplazar_coma(campo):
    from django.db.models import Value
    from django.db.models.functions import Replace, Trim
    return Trim(Replace(campo, Value(','), Value('.')))


def etiqueta_operador(op, tipo):
    from apps.automatizaciones.models import CondicionCampo
    if tipo == 'fecha':
        relabel = CondicionCampo.metadata_js()['relabel_fecha']
        if op in relabel:
            return relabel[op]
    return dict(CondicionCampo.OPERADORES).get(op, op)


def chips(params):
    """[(texto, valor crudo)] de las condiciones activas, para mostrarlas y poder quitarlas de a una."""
    conds = parsear(params)
    personalizados = _personalizados(conds)
    salida = []
    for clave, op, valor, crudo in conds:
        defin = _definicion(clave, personalizados)
        if not defin:
            continue
        label, tipo = defin
        if tipo == 'sino':
            valor = 'Sí' if valor.lower() in ('true', '1', 'si', 'sí') else 'No'
        elif tipo == 'fecha' and _fecha(valor):
            valor = _fecha(valor).strftime('%d/%m/%Y')
        texto = f'{label} {etiqueta_operador(op, tipo)}' + (f' {valor}' if op not in SIN_VALOR else '')
        salida.append((texto, crudo))
    return salida


def meta_js(embudo=None):
    """Lo que necesita el armador de condiciones en pantalla (campos, operadores por tipo, etiquetas)."""
    from apps.automatizaciones.models import CondicionCampo
    meta = CondicionCampo.metadata_js()
    meta['campos'] = [{'k': k, 'l': label, 't': tipo, 'o': opciones}
                      for k, label, tipo, opciones in campos_disponibles(embudo)]
    return meta
