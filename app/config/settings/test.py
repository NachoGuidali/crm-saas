from .base import *  # noqa

DEBUG = False
ALLOWED_HOSTS = ['*']
CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
                       'KEY_FUNCTION': 'django_tenants.cache.make_key',
                       'REVERSE_KEY_FUNCTION': 'django_tenants.cache.reverse_key'}}
# Los tests corren dentro de un cliente de prueba (dominio "testserver"): ver core/test_runner.py
TEST_RUNNER = 'core.test_runner.RunnerMultiCliente'
SESSION_ENGINE = 'django.contrib.sessions.backends.db'
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
STORAGES = {
    'default': {'BACKEND': 'core.storage.ArchivosPorCliente'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
}
