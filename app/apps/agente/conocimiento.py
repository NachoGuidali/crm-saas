"""Lo que sabe el agente: texto de documentos (PDF/Word/TXT) y la tabla de precios (Excel, CSV o Google Sheets)."""
import csv
import io
import re
import unicodedata

import requests
from django.utils import timezone

MAX_FILAS = 5000
MAX_RESULTADOS = 40


class ErrorConocimiento(Exception):
    pass


# ── Documentos ─────────────────────────────────────────────────────────────

def extraer_texto(nombre, contenido: bytes) -> str:
    ext = nombre.lower().rsplit('.', 1)[-1] if '.' in nombre else ''
    try:
        if ext == 'pdf':
            from pypdf import PdfReader
            lector = PdfReader(io.BytesIO(contenido))
            return '\n\n'.join((p.extract_text() or '').strip() for p in lector.pages).strip()
        if ext == 'docx':
            import docx
            d = docx.Document(io.BytesIO(contenido))
            partes = [p.text for p in d.paragraphs]
            for t in d.tables:
                partes += [' | '.join(c.text.strip() for c in fila.cells) for fila in t.rows]
            return '\n'.join(partes).strip()
        if ext in ('txt', 'md', 'csv'):
            return contenido.decode('utf-8', errors='replace').strip()
    except Exception as e:
        raise ErrorConocimiento(f'No se pudo leer el archivo: {e}')
    raise ErrorConocimiento('Formato no soportado: subí PDF, Word (.docx) o texto (.txt).')


# ── Tabla de precios ───────────────────────────────────────────────────────

def _limpiar(filas):
    filas = [[str(c if c is not None else '').strip() for c in f] for f in filas]
    filas = [f for f in filas if any(f)]
    if len(filas) < 2:
        raise ErrorConocimiento('La planilla tiene que tener una fila de títulos y al menos una fila de datos.')
    columnas = [c or f'Columna {i + 1}' for i, c in enumerate(filas[0])]
    datos = [(f + [''] * len(columnas))[:len(columnas)] for f in filas[1:MAX_FILAS + 1]]
    return columnas, datos


def leer_planilla(nombre, contenido: bytes):
    """(columnas, filas) de un .xlsx o .csv."""
    ext = nombre.lower().rsplit('.', 1)[-1] if '.' in nombre else ''
    if ext == 'xlsx':
        from openpyxl import load_workbook
        try:
            hoja = load_workbook(io.BytesIO(contenido), read_only=True, data_only=True).worksheets[0]
        except Exception as e:
            raise ErrorConocimiento(f'No se pudo abrir el Excel: {e}')
        return _limpiar([list(f) for f in hoja.iter_rows(values_only=True)])
    if ext == 'csv':
        texto = contenido.decode('utf-8-sig', errors='replace')
        try:
            dialecto = csv.Sniffer().sniff(texto[:4000], delimiters=',;\t')
        except csv.Error:
            dialecto = csv.excel
        return _limpiar(list(csv.reader(io.StringIO(texto), dialecto)))
    raise ErrorConocimiento('Subí la planilla en Excel (.xlsx) o CSV.')


def url_csv_google(url):
    """Link de Google Sheets (el de compartir) → URL de exportación CSV de la primera hoja (o la del gid)."""
    m = re.search(r'/spreadsheets/d/([a-zA-Z0-9_-]+)', url or '')
    if not m:
        raise ErrorConocimiento('No parece un link de Google Sheets.')
    gid = re.search(r'[#&?]gid=(\d+)', url)
    return f'https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=csv' + (f'&gid={gid.group(1)}' if gid else '')


def sincronizar_google(config):
    try:
        resp = requests.get(url_csv_google(config.precios_url), timeout=20)
    except requests.RequestException as e:
        raise ErrorConocimiento(f'No se pudo bajar la planilla: {e}')
    if resp.status_code != 200 or 'text/html' in resp.headers.get('Content-Type', ''):
        raise ErrorConocimiento('Google no devolvió la planilla: compartila como "Cualquier persona con el link".')
    guardar_precios(config, *leer_planilla('precios.csv', resp.content))


def guardar_precios(config, columnas, filas):
    config.precios_columnas, config.precios_filas = columnas, filas
    config.precios_actualizado_at, config.precios_error = timezone.now(), ''
    config.save(update_fields=['precios_columnas', 'precios_filas', 'precios_actualizado_at', 'precios_error'])


def _norm(s):
    s = unicodedata.normalize('NFKD', str(s)).encode('ascii', 'ignore').decode().lower()
    return re.sub(r'\s+', ' ', s).strip()


def buscar_precios(config, terminos):
    """Filas donde aparecen TODOS los términos (sin importar mayúsculas ni acentos)."""
    terminos = [_norm(t) for t in terminos or [] if str(t).strip()]
    filas = config.precios_filas or []
    encontradas = []
    for f in filas:
        texto = ' | '.join(_norm(c) for c in f)
        if all(t in texto for t in terminos):
            encontradas.append(f)
    return encontradas
