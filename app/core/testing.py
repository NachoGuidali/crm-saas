from django.core.cache import cache
from django.test import TestCase as DjangoTestCase


class TestCase(DjangoTestCase):
    """TestCase que limpia la caché: evita que objetos cacheados (config, contadores) pasen de un test a otro."""

    @classmethod
    def setUpClass(cls):
        cache.clear()
        super().setUpClass()

    def setUp(self):
        cache.clear()
        super().setUp()
