"""
Marco fijo para el teléfono del CRM.

Si el agente usa el teléfono dentro del CRM, cada vez que abre una pantalla del CRM en la pestaña (no dentro de un
iframe) se devuelve el marco: arriba el teléfono, abajo un iframe con la pantalla pedida. Al navegar solo cambia el
iframe, así el teléfono (registro SIP y llamada en curso) no se recarga. Usa el encabezado Sec-Fetch-Dest que mandan
Chrome, Edge y Firefox; sin ese encabezado el CRM funciona como siempre.
"""
from django.shortcuts import render

from core.tenancy import cliente_actual

EXCLUIDOS = ('/api/', '/static/', '/media/', '/telefono/', '/usuarios/login', '/usuarios/logout',
             '/usuarios/recuperar', '/e/', '/demo/', '/pulso', '/integraciones/', '/whatsapp/webhook', '/admin/')


class MarcoTelefonoMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if self._corresponde(request):
            from apps.telefonia.services import usa_softphone
            if usa_softphone(request.user):
                return render(request, 'telefonia/marco.html', {'destino': request.get_full_path()})
        return self.get_response(request)

    @staticmethod
    def _corresponde(request):
        if cliente_actual() is None:  # dominio principal: no hay CRM
            return False
        if request.method != 'GET' or request.headers.get('Sec-Fetch-Dest') != 'document':
            return False
        if request.headers.get('Sec-Fetch-Mode') != 'navigate' or 'sin_marco' in request.GET:
            return False
        if any(request.path.startswith(p) for p in EXCLUIDOS):
            return False
        if 'csv' in request.GET or 'json' in request.GET or 'exportar' in request.path:
            return False
        return getattr(request, 'user', None) is not None and request.user.is_authenticated
