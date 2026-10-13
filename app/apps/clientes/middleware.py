from django.db import connection
from django.shortcuts import render


class ClienteSuspendidoMiddleware:
    """Corta el acceso al CRM de un cliente suspendido o dado de baja (pantalla, API y webhooks)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        tenant = getattr(connection, 'tenant', None)
        if tenant is not None and not getattr(tenant, 'tiene_acceso', True):
            return render(request, 'clientes/suspendido.html', {'cliente': tenant}, status=403)
        return self.get_response(request)
