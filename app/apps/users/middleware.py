from django.shortcuts import redirect
from django.urls import reverse

from core.tenancy import cliente_actual


class ForzarCambioPasswordMiddleware:
    """Si el usuario tiene una contraseña temporal, lo obliga a cambiarla antes de seguir."""

    PERMITIDAS = ('users:password_change', 'users:logout')

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, 'user', None)
        if user is not None and user.is_authenticated and getattr(user, 'debe_cambiar_password', False) \
                and cliente_actual() is not None:
            permitidas = {reverse(n) for n in self.PERMITIDAS}
            if request.path not in permitidas and not request.path.startswith(('/static/', '/media/')):
                return redirect('users:password_change')
        return self.get_response(request)
