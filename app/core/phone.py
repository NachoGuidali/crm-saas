"""
Normalización de teléfonos: una sola función para todos los canales.

Todo número que entra al CRM (importación Excel/CSV, API, WhatsApp de cualquier
proveedor, llamadas de Anura, carga manual) pasa por `normalizar_telefono`, y el
resultado es la clave de deduplicación de contactos. Si dos canales escriben el
mismo número en formatos distintos, tienen que terminar en el mismo string.

Formato de salida para Argentina: +549 + código de área + número (10 dígitos),
que es el formato que usa WhatsApp. Se aplica también a fijos: no es el formato
"de marcado" pero sirve como identificador único y estable.

Ejemplos que terminan todos en +5491123456789:
    +54 9 11 2345-6789 · 5491123456789 · +541123456789 · 011 15 2345-6789
    01123456789 · 1123456789 · 11 15 2345 6789 · 0054 9 11 2345 6789
    whatsapp:+5491123456789 · 5491123456789@s.whatsapp.net
"""
import re

PAIS_DEFAULT = '54'
_LARGO_NACIONAL_AR = 10


def _solo_digitos(valor: str) -> str:
    return re.sub(r'\D', '', valor or '')


def _quitar_15(nacional: str) -> str:
    """'11 15 23456789' (12 dígitos) → '1123456789'. El 15 va después del código de área (2 a 4 dígitos)."""
    if len(nacional) != _LARGO_NACIONAL_AR + 2:
        return nacional
    for largo_area in (2, 3, 4):
        if nacional[largo_area:largo_area + 2] == '15':
            return nacional[:largo_area] + nacional[largo_area + 2:]
    return nacional


def normalizar_telefono(valor: str, pais_default: str = PAIS_DEFAULT) -> str:
    """Devuelve el teléfono en formato E.164 (+...) o '' si no parece un teléfono válido."""
    if not valor:
        return ''
    crudo = str(valor).strip().lower()
    crudo = crudo.replace('whatsapp:', '').split('@')[0]
    tiene_mas = crudo.startswith('+')
    digitos = _solo_digitos(crudo)
    if not digitos:
        return ''

    if digitos.startswith('00'):
        digitos = digitos[2:]
        tiene_mas = True

    if tiene_mas or (digitos.startswith('54') and len(digitos) >= 12):
        if digitos.startswith('54'):
            return _normalizar_ar(digitos[2:])
        # Número internacional de otro país: se deja tal cual
        return '+' + digitos if 8 <= len(digitos) <= 15 else ''

    # Sin código de país → se asume nacional del país default
    if pais_default == '54':
        return _normalizar_ar(digitos)
    return '+' + pais_default + digitos.lstrip('0')


def _normalizar_ar(resto: str) -> str:
    """`resto` = lo que viene después del 54 (o un número nacional)."""
    resto = resto.lstrip('0')
    # Con el 9 de celular adelante (formato internacional de WhatsApp)
    if resto.startswith('9') and len(resto) in (_LARGO_NACIONAL_AR + 1, _LARGO_NACIONAL_AR + 3):
        resto = resto[1:]
    resto = _quitar_15(resto)
    if len(resto) != _LARGO_NACIONAL_AR:
        # Número corto/raro: se guarda con prefijo para no perderlo, pero no se "inventa" nada.
        return '+54' + resto if 6 <= len(resto) <= 13 else ''
    return '+549' + resto


def variantes_telefono(telefono: str) -> list:
    """Variantes para buscar registros viejos guardados sin normalizar (+549X / +54X)."""
    tel = normalizar_telefono(telefono)
    if not tel:
        return []
    variantes = [tel]
    if tel.startswith('+549'):
        variantes.append('+54' + tel[4:])
    return variantes


def telefono_para_proveedor(telefono: str) -> str:
    """'+5491123456789' → '5491123456789' (Evolution / Meta)."""
    return (telefono or '').lstrip('+')


def formatear_telefono(telefono: str) -> str:
    """Formato legible: +54 9 11 2345-6789."""
    if not telefono:
        return ''
    if telefono.startswith('+549') and len(telefono) == 14:
        n = telefono[4:]
        area_len = 2 if n.startswith('11') else (3 if n[:3] in _AREAS_3 else 4)
        area, abonado = n[:area_len], n[area_len:]
        return f'+54 9 {area} {abonado[:-4]}-{abonado[-4:]}'
    return telefono


# Códigos de área de 3 dígitos más comunes (el resto se muestra con 4)
_AREAS_3 = {
    '221', '223', '230', '236', '237', '249', '260', '261', '263', '264', '266', '280', '291',
    '294', '297', '298', '299', '336', '341', '342', '343', '345', '348', '351', '353', '358',
    '362', '364', '370', '376', '379', '380', '381', '383', '385', '387', '388',
}
