from django import template
from django.utils import timezone

from core.phone import formatear_telefono

register = template.Library()


@register.filter
def telefono(valor):
    return formatear_telefono(valor)


@register.filter
def tiene_perm(user, slug):
    return bool(user and user.is_authenticated and user.tiene_permiso(slug))


@register.filter
def dias_desde(fecha):
    if not fecha:
        return ''
    return (timezone.now() - fecha).days


@register.filter
def duracion(segundos):
    try:
        s = int(segundos or 0)
    except (TypeError, ValueError):
        return ''
    h, resto = divmod(s, 3600)
    m, s = divmod(resto, 60)
    return f'{h}:{m:02d}:{s:02d}' if h else f'{m}:{s:02d}'


@register.filter
def get_item(dic, key):
    try:
        return dic.get(key)
    except AttributeError:
        return None


@register.simple_tag(takes_context=True)
def qs_con(context, **kwargs):
    """Querystring actual reemplazando/agregando parámetros: {% qs_con page=2 %}"""
    params = context['request'].GET.copy()
    for k, v in kwargs.items():
        if v in (None, ''):
            params.pop(k, None)
        else:
            params[k] = v
    return params.urlencode()


@register.filter
def duracion_td(td):
    """timedelta → '5 h 20 min'"""
    if not td:
        return ''
    minutos = int(td.total_seconds() // 60)
    h, m = divmod(minutos, 60)
    return f'{h} h {m} min' if h else f'{m} min'


@register.filter
def miles(valor):
    """10000 → '10.000' (solo para mostrar; nunca en IDs ni en valores de formularios)."""
    try:
        return f'{int(valor):,}'.replace(',', '.')
    except (TypeError, ValueError):
        return valor


@register.filter
def pesos(valor, decimales=None):
    """1234.5 → '$ 1.234,50' (formato argentino). Sin decimales si el monto es grande."""
    if valor in (None, ''):
        return '—'
    try:
        from decimal import Decimal
        v = Decimal(str(valor))
    except Exception:
        return valor
    dec = int(decimales) if decimales not in (None, '') else (0 if abs(v) >= 1000 else 2)
    texto = f'{v:,.{dec}f}'.replace(',', 'X').replace('.', ',').replace('X', '.')
    return f'$ {texto}'


@register.filter
def split_pares(texto):
    """'a:A,b:B' → [('a', 'A'), ('b', 'B')] (para armar botones en plantillas)."""
    return [tuple(p.split(':', 1)) for p in str(texto).split(',') if ':' in p]


@register.filter
def split_pares_simple(texto):
    return [p.strip() for p in str(texto).split(',') if p.strip()]
