from functools import wraps

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse


class PermisoRequeridoMixin(LoginRequiredMixin):
    """Vista que exige uno (o cualquiera de varios) permisos granulares del usuario."""
    permiso = None           # 'embudos' o ('reportes', 'supervision') → alcanza con uno

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        permisos = (self.permiso,) if isinstance(self.permiso, str) else (self.permiso or ())
        if permisos and not any(request.user.tiene_permiso(p) for p in permisos):
            raise PermissionDenied('No tenés permiso para acceder a esta sección.')
        return super().dispatch(request, *args, **kwargs)


class AdminRequeridoMixin(LoginRequiredMixin):
    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        if not request.user.is_admin:
            raise PermissionDenied('Solo administradores.')
        return super().dispatch(request, *args, **kwargs)


def permiso_json(permiso):
    """Decorador para endpoints AJAX: 403 en JSON en vez de página de error."""
    def deco(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return JsonResponse({'ok': False, 'error': 'no_autenticado'}, status=401)
            if permiso and not request.user.tiene_permiso(permiso):
                return JsonResponse({'ok': False, 'error': 'sin_permiso'}, status=403)
            return view(request, *args, **kwargs)
        return wrapper
    return deco
