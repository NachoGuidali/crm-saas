"""PDF del formulario firmado: documento, respuestas, firmas y un bloque de evidencia (fecha, IP, dispositivo, huella)."""
from io import BytesIO

from django.core.files.storage import default_storage
from django.utils import timezone
from fpdf import FPDF

from .models import FormularioFirma as F


def _txt(valor):
    """Las fuentes base del PDF son Latin-1: tildes y ñ entran; emojis y otros símbolos se sacan."""
    s = str(valor if valor is not None else '')
    s = s.replace('–', '-').replace('—', '-').replace('“', '"').replace('”', '"') \
         .replace('‘', "'").replace('’', "'").replace('…', '...').replace('•', '-')
    return s.encode('latin-1', 'ignore').decode('latin-1')


def _hex_a_rgb(color, defecto=(37, 99, 235)):
    c = (color or '').lstrip('#')
    try:
        return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4)) if len(c) >= 6 else defecto
    except ValueError:
        return defecto


def _leer(ruta):
    try:
        with default_storage.open(ruta, 'rb') as f:
            return BytesIO(f.read())
    except (OSError, ValueError):
        return None


def valor_display(campo, valor):
    tipo = campo.get('tipo')
    if tipo in (F.T_SINO, F.T_ACEPTO):
        return 'Sí' if valor in (True, 'true', '1', 'on', 'si', 'sí') else 'No'
    if tipo == F.T_FECHA and valor:
        try:
            from datetime import date
            return date.fromisoformat(str(valor)[:10]).strftime('%d/%m/%Y')
        except ValueError:
            pass
    return str(valor or '-')


def generar(sol):
    """Devuelve los bytes del PDF de una solicitud firmada."""
    snap = sol.snapshot or {}
    principal = _hex_a_rgb(snap.get('color_principal'))
    pdf = FPDF(format='A4')
    pdf.set_auto_page_break(True, margin=18)
    pdf.set_title(_txt(snap.get('titulo', '')))
    pdf.add_page()

    logo = _leer(snap['logo']) if snap.get('logo') else None
    if logo is not None:
        try:
            pdf.image(logo, x=pdf.l_margin, y=10, h=16)
            pdf.set_y(30)
        except Exception:  # logo dañado o formato no soportado: el PDF sale igual
            pass
    pdf.set_text_color(*principal)
    pdf.set_font('helvetica', 'B', 16)
    pdf.multi_cell(0, 8, _txt(snap.get('titulo', '')), new_x='LMARGIN', new_y='NEXT')
    pdf.set_text_color(30, 30, 30)
    pdf.ln(2)

    if snap.get('documento'):
        pdf.set_font('helvetica', '', 10.5)
        pdf.multi_cell(0, 5.5, _txt(snap['documento']), new_x='LMARGIN', new_y='NEXT')
        pdf.ln(3)

    ancho = pdf.w - pdf.l_margin - pdf.r_margin
    for campo in snap.get('campos', []):
        tipo = campo.get('tipo')
        if tipo == F.T_TITULO:
            pdf.ln(2)
            pdf.set_font('helvetica', 'B', 12)
            pdf.multi_cell(0, 6, _txt(campo.get('etiqueta')), new_x='LMARGIN', new_y='NEXT')
            if campo.get('ayuda'):
                pdf.set_font('helvetica', '', 10)
                pdf.multi_cell(0, 5, _txt(campo['ayuda']), new_x='LMARGIN', new_y='NEXT')
            continue
        if tipo == F.T_FIRMA:
            ruta = (sol.firmas or {}).get(campo['id'])
            img = _leer(ruta) if ruta else None
            if pdf.get_y() > pdf.h - 60:
                pdf.add_page()
            pdf.ln(2)
            pdf.set_font('helvetica', 'B', 10)
            pdf.cell(0, 5, _txt(campo.get('etiqueta') or 'Firma'), new_x='LMARGIN', new_y='NEXT')
            y = pdf.get_y()
            if img is not None:
                pdf.image(img, x=pdf.l_margin, y=y, h=28)
            pdf.set_y(y + 30)
            pdf.set_draw_color(160, 160, 160)
            pdf.line(pdf.l_margin, pdf.get_y(), pdf.l_margin + 80, pdf.get_y())
            pdf.ln(4)
            continue
        valor = valor_display(campo, (sol.respuestas or {}).get(campo['id']))
        pdf.set_font('helvetica', 'B', 9.5)
        pdf.set_text_color(90, 90, 90)
        pdf.multi_cell(ancho, 5, _txt(campo.get('etiqueta')), new_x='LMARGIN', new_y='NEXT')
        pdf.set_text_color(20, 20, 20)
        pdf.set_font('helvetica', '', 11)
        pdf.multi_cell(ancho, 5.5, _txt(valor), new_x='LMARGIN', new_y='NEXT')
        pdf.ln(1.5)

    # Evidencia de la firma electrónica
    if pdf.get_y() > pdf.h - 55:
        pdf.add_page()
    pdf.ln(4)
    pdf.set_draw_color(*principal)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(2)
    pdf.set_font('helvetica', 'B', 9)
    pdf.set_text_color(60, 60, 60)
    pdf.cell(0, 5, 'Constancia de firma electrónica', new_x='LMARGIN', new_y='NEXT')
    pdf.set_font('helvetica', '', 8)
    firmada = timezone.localtime(sol.firmada_at) if sol.firmada_at else None
    lineas = [
        f'Firmante: {sol.contacto.nombre}' + (f' - DNI {sol.contacto.dni}' if sol.contacto.dni else '')
        + (f' - Tel. {sol.contacto.telefono}' if sol.contacto.telefono else ''),
        f'Fecha y hora: {firmada:%d/%m/%Y %H:%M:%S} (hora de Argentina)' if firmada else '',
        f'IP: {sol.ip or "-"}',
        f'Dispositivo: {sol.dispositivo[:150] or "-"}',
        f'ID de la solicitud: {sol.pk} - código {sol.token[:10]}…'.replace('…', '...'),
        f'Huella SHA-256 del contenido firmado: {sol.huella}',
    ]
    for linea in lineas:
        if linea:
            pdf.multi_cell(0, 4, _txt(linea), new_x='LMARGIN', new_y='NEXT')
    return bytes(pdf.output())
