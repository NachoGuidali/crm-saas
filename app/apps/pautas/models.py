import re
import unicodedata

from django.conf import settings
from django.core.cache import cache
from django.db import models
from django.urls import reverse


def normalizar_clave(texto):
    """'Pauta Instagram', 'pauta_instagram', 'PAUTA-INSTAGRAM ' → 'pauta instagram'."""
    texto = unicodedata.normalize('NFKD', str(texto or '')).encode('ascii', 'ignore').decode().lower()
    return re.sub(r'[\s_\-\.]+', ' ', texto).strip()


def normalizar_mensaje(texto):
    """Para palabras clave: además de normalizar_clave, ignora la puntuación ('Hola, quería…!' → 'hola queria')."""
    return re.sub(r'\s+', ' ', re.sub(r'[^\w#@]+', ' ', normalizar_clave(texto).replace('_', ' '))).strip()


class Pauta(models.Model):
    """Campaña de publicidad. Los leads se vinculan por su origen (texto que manda el formulario, la API, etc.)."""
    PLATAFORMAS = [
        ('meta', 'Meta (Instagram / Facebook)'), ('google', 'Google Ads'), ('tiktok', 'TikTok'),
        ('email', 'Email marketing'), ('web', 'Sitio web / SEO'), ('offline', 'Offline / eventos'), ('otra', 'Otra'),
    ]

    nombre = models.CharField(max_length=150, unique=True)
    plataforma = models.CharField(max_length=15, choices=PLATAFORMAS, default='meta')
    claves = models.JSONField(
        default=list, blank=True, verbose_name='También llega como',
        help_text='Otros textos de origen que corresponden a esta pauta (utm_campaign, nombre del anuncio…). '
                  'No distingue mayúsculas, acentos, guiones ni guiones bajos.',
    )
    anuncio_ids = models.JSONField(
        default=list, blank=True, verbose_name='IDs de anuncio (Meta)',
        help_text='Para chats de WhatsApp que llegan desde un anuncio "clic para WhatsApp": Meta manda el ID del '
                  'anuncio en el primer mensaje.',
    )
    palabras_clave = models.JSONField(
        default=list, blank=True, verbose_name='Palabras clave en el primer mensaje de WhatsApp',
        help_text='Si el chat no trae datos del anuncio, se busca alguno de estos textos en el primer mensaje '
                  '(un código como #IG-SEP o una frase del mensaje precargado del link).',
    )
    embudo = models.ForeignKey('crm.Embudo', null=True, blank=True, on_delete=models.SET_NULL, related_name='pautas',
                               help_text='Opcional: solo informativo / filtro.')
    inicio = models.DateField(null=True, blank=True)
    fin = models.DateField(null=True, blank=True)
    activa = models.BooleanField(default=True)
    notas = models.TextField(blank=True)
    creada_por = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-activa', 'nombre']

    def __str__(self):
        return self.nombre

    def get_absolute_url(self):
        return reverse('pautas:detalle', args=[self.pk])

    @property
    def claves_normalizadas(self):
        return {normalizar_clave(c) for c in [self.nombre, *(self.claves or [])] if normalizar_clave(c)}

    def save(self, *args, **kwargs):
        limpiar = lambda xs: list(dict.fromkeys(str(c).strip() for c in (xs or []) if c and str(c).strip()))
        self.claves, self.anuncio_ids, self.palabras_clave = (limpiar(self.claves), limpiar(self.anuncio_ids),
                                                              limpiar(self.palabras_clave))
        super().save(*args, **kwargs)
        cache.delete_many(['pautas_mapa_claves', 'pautas_mapa_whatsapp'])

    def delete(self, *args, **kwargs):
        super().delete(*args, **kwargs)
        cache.delete_many(['pautas_mapa_claves', 'pautas_mapa_whatsapp'])

    @property
    def palabras_normalizadas(self):
        return {normalizar_mensaje(c) for c in (self.palabras_clave or []) if normalizar_mensaje(c)}

    @classmethod
    def mapa_whatsapp(cls):
        """{'anuncios': {id: pauta_pk}, 'palabras': [(palabra_normalizada, pauta_pk)] (más largas primero)}."""
        mapa = cache.get('pautas_mapa_whatsapp')
        if mapa is None:
            anuncios, palabras = {}, []
            for p in cls.objects.filter(activa=True):
                for a in p.anuncio_ids or []:
                    anuncios.setdefault(str(a).strip(), p.pk)
                palabras += [(w, p.pk) for w in p.palabras_normalizadas]
            palabras.sort(key=lambda x: -len(x[0]))
            mapa = {'anuncios': anuncios, 'palabras': palabras}
            cache.set('pautas_mapa_whatsapp', mapa, 300)
        return mapa

    @classmethod
    def mapa_claves(cls):
        mapa = cache.get('pautas_mapa_claves')
        if mapa is None:
            mapa = {}
            for p in cls.objects.all():
                for clave in p.claves_normalizadas:
                    mapa.setdefault(clave, p.pk)
            cache.set('pautas_mapa_claves', mapa, 300)
        return mapa


class InversionPauta(models.Model):
    """Plata invertida en una pauta. Se imputa al período por su fecha (ej.: un registro por mes)."""
    pauta = models.ForeignKey(Pauta, on_delete=models.CASCADE, related_name='inversiones')
    fecha = models.DateField(help_text='Día o primer día del período al que corresponde el gasto.')
    monto = models.DecimalField(max_digits=14, decimal_places=2)
    nota = models.CharField(max_length=200, blank=True)
    cargada_por = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-fecha', '-pk']
        indexes = [models.Index(fields=['pauta', 'fecha'])]
