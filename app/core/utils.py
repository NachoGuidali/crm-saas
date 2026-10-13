import json

from django.core.paginator import Paginator
from django.http import JsonResponse


def paginar(request, queryset, por_pagina=50):
    paginator = Paginator(queryset, por_pagina)
    return paginator.get_page(request.GET.get('page'))


def json_body(request):
    """Lee JSON o form-data indistintamente (los endpoints AJAX aceptan ambos)."""
    if request.content_type and 'json' in request.content_type:
        try:
            return json.loads(request.body or b'{}')
        except (ValueError, TypeError):
            return {}
    return request.POST.dict()


def ok(**data):
    return JsonResponse({'ok': True, **data})


def error(mensaje, status=400, **data):
    return JsonResponse({'ok': False, 'error': mensaje, **data}, status=status)


def query_sin_page(request):
    """Querystring actual sin el parámetro page (para links de paginación)."""
    params = request.GET.copy()
    params.pop('page', None)
    return params.urlencode()
