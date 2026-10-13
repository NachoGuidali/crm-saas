"""
Pautas: vínculo lead ↔ campaña y métricas de costo.

- Atribución al **primer ingreso**: la oportunidad queda con la pauta por la que entró. Si la persona vuelve a
  entrar por otra pauta mientras sigue en curso, no se duplica la oportunidad pero se cuenta como "repetido" de esa pauta.
- Las métricas son de **cohorte**: de los leads que ingresaron en el período, cuántos se contactaron, compraron, etc.
  (aunque la venta se haya cerrado después). La inversión se toma por la fecha de cada carga.
"""
from decimal import Decimal

from django.db.models import Count, Q, Sum

from .models import InversionPauta, Pauta, normalizar_clave, normalizar_mensaje


def resolver_pauta(texto):
    """Pauta que corresponde a un texto de origen, o None."""
    clave = normalizar_clave(texto)
    if not clave:
        return None
    pk = Pauta.mapa_claves().get(clave)
    return Pauta.objects.filter(pk=pk).first() if pk else None


def resolver_pauta_whatsapp(extra, texto):
    """
    Pauta de un chat de WhatsApp nuevo. Orden: ID del anuncio de Meta → título del anuncio (nombre o "También llega
    como") → palabras clave en el primer mensaje. Devuelve (pauta | None, texto de origen para guardar).
    """
    extra = extra or {}
    referral = extra.get('referral') or {}
    anuncio_id = str(referral.get('source_id') or referral.get('sourceId') or '').strip()
    titulo = extra.get('pauta', '')
    mapa = Pauta.mapa_whatsapp()
    if anuncio_id and anuncio_id in mapa['anuncios']:
        pauta = Pauta.objects.filter(pk=mapa['anuncios'][anuncio_id]).first()
        if pauta:
            return pauta, titulo or f'Anuncio {anuncio_id}'
    if titulo:
        pauta = resolver_pauta(titulo)
        if pauta:
            return pauta, titulo
    mensaje = f' {normalizar_mensaje(texto)} '
    if mensaje:
        for palabra, pk in mapa['palabras']:
            if f' {palabra} ' in mensaje or (not palabra[-1].isalnum() and palabra in mensaje):
                pauta = Pauta.objects.filter(pk=pk).first()
                if pauta:
                    return pauta, pauta.nombre
    return None, titulo or (f'Anuncio {anuncio_id}' if anuncio_id else '')


def vincular_existentes(pauta):
    """Asocia a la pauta las oportunidades sin pauta cuyo origen coincide (útil al crearla después)."""
    from apps.crm.models import Oportunidad
    claves = pauta.claves_normalizadas
    textos = [t for t in Oportunidad.objects.filter(pauta__isnull=True).exclude(origen_pauta='')
              .values_list('origen_pauta', flat=True).distinct() if normalizar_clave(t) in claves]
    if not textos:
        return 0
    return Oportunidad.objects.filter(pauta__isnull=True, origen_pauta__in=textos).update(pauta=pauta)


def origenes_sin_pauta(limite=30):
    """Textos de origen que llegaron y no coinciden con ninguna pauta, con su cantidad."""
    from apps.crm.models import Oportunidad
    return list(Oportunidad.objects.filter(pauta__isnull=True).exclude(origen_pauta='')
                .values('origen_pauta').annotate(n=Count('pk')).order_by('-n')[:limite])


def _div(a, b):
    return (Decimal(a) / Decimal(b)).quantize(Decimal('0.01')) if b else None


def _pct(a, b):
    return round(a * 100 / b, 1) if b else 0


def metricas(ini, fin, embudo=None, pautas=None):
    """Filas por pauta (+ 'Sin pauta') con inversión, leads, contactados, ventas y costos."""
    from apps.crm.models import Actividad, Oportunidad
    ops = Oportunidad.objects.filter(created_at__range=(ini, fin))
    if embudo is not None:
        ops = ops.filter(embudo=embudo)
    agregados = {r['pauta']: r for r in ops.values('pauta').annotate(
        leads=Count('pk'),
        efectivos=Count('pk', filter=Q(contacto_efectivo_at__isnull=False)),
        ventas=Count('pk', filter=Q(estado=Oportunidad.ESTADO_GANADA)),
        perdidas=Count('pk', filter=Q(estado=Oportunidad.ESTADO_PERDIDA)),
        en_curso=Count('pk', filter=Q(estado__in=Oportunidad.ESTADOS_ACTIVOS)),
        valor=Sum('valor', filter=Q(estado=Oportunidad.ESTADO_GANADA)),
    )}
    inversion = dict(InversionPauta.objects.filter(fecha__range=(ini.date(), fin.date()))
                     .values_list('pauta').annotate(t=Sum('monto')).values_list('pauta', 't'))
    from django.db.models.fields.json import KeyTextTransform
    repetidos = {int(k): n for k, n in (
        Actividad.objects.filter(tipo=Actividad.TIPO_REINGRESO, created_at__range=(ini, fin), datos__has_key='pauta_id')
        .annotate(p=KeyTextTransform('pauta_id', 'datos')).values_list('p').annotate(n=Count('pk')).values_list('p', 'n'))
        if k}

    qs_pautas = Pauta.objects.all() if pautas is None else pautas
    filas = []
    for p in qs_pautas:
        a = agregados.get(p.pk, {})
        if not a and not inversion.get(p.pk) and not p.activa:
            continue
        filas.append(_fila(p, a, inversion.get(p.pk) or Decimal('0'), repetidos.get(p.pk, 0)))
    if pautas is None and agregados.get(None):
        filas.append(_fila(None, agregados[None], Decimal('0'), 0))
    return filas


def _fila(pauta, a, inversion, repetidos):
    leads, ventas = a.get('leads', 0), a.get('ventas', 0)
    valor = a.get('valor') or Decimal('0')
    return {
        'pauta': pauta, 'inversion': inversion, 'leads': leads, 'repetidos': repetidos,
        'efectivos': a.get('efectivos', 0), 'ventas': ventas, 'perdidas': a.get('perdidas', 0),
        'en_curso': a.get('en_curso', 0), 'valor': valor,
        'contactabilidad': _pct(a.get('efectivos', 0), leads), 'conversion': _pct(ventas, leads),
        'cpl': _div(inversion, leads) if inversion else None,
        'cpa': _div(inversion, ventas) if inversion else None,
        # Cuotas mensuales vendidas vs inversión: cuántos meses de cuota recuperan lo invertido
        'meses_recupero': _div(inversion, valor) if inversion and valor else None,
    }


def totales(filas):
    t = {k: sum((f[k] for f in filas), Decimal('0') if k in ('inversion', 'valor') else 0)
         for k in ('inversion', 'leads', 'repetidos', 'efectivos', 'ventas', 'perdidas', 'en_curso', 'valor')}
    t.update(contactabilidad=_pct(t['efectivos'], t['leads']), conversion=_pct(t['ventas'], t['leads']))
    con_inversion = [f for f in filas if f['inversion']]
    leads_pagos = sum(f['leads'] for f in con_inversion)
    ventas_pagas = sum(f['ventas'] for f in con_inversion)
    t['cpl'] = _div(t['inversion'], leads_pagos) if t['inversion'] else None
    t['cpa'] = _div(t['inversion'], ventas_pagas) if t['inversion'] else None
    return t
