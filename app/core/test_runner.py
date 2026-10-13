"""
Runner de tests multi-cliente: los tests del CRM corren dentro de un cliente de prueba (schema "test") con el
dominio "testserver" (el host del cliente de tests de Django), así se ejercita el mismo camino que en producción:
middleware de django-tenants, tablas en el schema del cliente, caché con prefijo, etc.

El cliente se crea una sola vez, antes de clonar la base para los workers en paralelo (cada clon ya lo trae).
"""
from django.db import connections
from django.test.runner import DiscoverRunner, ParallelTestSuite, _init_worker
from django.test.utils import setup_databases

SCHEMA_TESTS = 'test'
# Segundo cliente, para los tests de aislamiento entre clientes (apps/clientes/tests.py).
SCHEMA_OTRO = 'test_otro'


def _activar_cliente_de_tests():
    from django_tenants.utils import get_tenant_model
    connections['default'].set_tenant(get_tenant_model().objects.get(schema_name=SCHEMA_TESTS))


def _init_worker_con_cliente(*args, **kwargs):
    _init_worker(*args, **kwargs)
    _activar_cliente_de_tests()


class _ParallelTestSuiteConCliente(ParallelTestSuite):
    init_worker = _init_worker_con_cliente


class RunnerMultiCliente(DiscoverRunner):
    parallel_test_suite = _ParallelTestSuiteConCliente

    def setup_databases(self, **kwargs):
        # Base de tests sin clonar → cliente de prueba → recién ahí los clones para los workers.
        old_config = setup_databases(self.verbosity, self.interactive, time_keeper=self.time_keeper,
                                     keepdb=self.keepdb, debug_sql=self.debug_sql, parallel=0, **kwargs)
        self._crear_cliente_de_tests()
        if self.parallel > 1:
            for indice in range(self.parallel):
                connections['default'].creation.clone_test_db(suffix=str(indice + 1), verbosity=self.verbosity,
                                                              keepdb=self.keepdb)
        _activar_cliente_de_tests()
        return old_config

    def _crear_cliente_de_tests(self):
        from django_tenants.utils import get_public_schema_name, get_tenant_domain_model, get_tenant_model
        Cliente, Dominio = get_tenant_model(), get_tenant_domain_model()
        connections['default'].set_schema_to_public()
        publico, _ = Cliente.objects.get_or_create(schema_name=get_public_schema_name(),
                                                   defaults={'nombre': 'Plataforma', 'estado': 'activo'})
        Dominio.objects.get_or_create(domain='plataforma.testserver', defaults={'tenant': publico, 'is_primary': True})
        for schema, nombre, dominio in ((SCHEMA_TESTS, 'Cliente de tests', 'testserver'),
                                        (SCHEMA_OTRO, 'Otro cliente', 'otro.testserver')):
            cliente = Cliente.objects.filter(schema_name=schema).first()
            if cliente is None:  # con --keepdb ya existe
                cliente = Cliente(schema_name=schema, nombre=nombre, estado='activo')
                cliente.save(verbosity=0)  # crea el schema y corre las migraciones del CRM
            Dominio.objects.get_or_create(domain=dominio, defaults={'tenant': cliente, 'is_primary': True})
