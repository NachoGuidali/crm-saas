from django.http import HttpResponse
from django.shortcuts import render


def robots_privado(request):
    """robots.txt de los subdominios de clientes: el CRM es privado, no se indexa."""
    return HttpResponse('User-agent: *\nDisallow: /\n', content_type='text/plain')


def error_403(request, exception=None):
    return render(request, 'errores/403.html', {'mensaje': str(exception or '')}, status=403)


def error_404(request, exception=None):
    return render(request, 'errores/404.html', status=404)


def error_500(request):
    return render(request, 'errores/500.html', status=500)


def media_protegida(request, ruta):
    """
    Sirve /media/ exigiendo sesión (grabaciones, adjuntos de clientes, importaciones).
    Excepción: los adjuntos salientes de WhatsApp se sirven con una URL firmada y con vencimiento (A4),
    para que Meta / Twilio los descarguen al enviar sin dejarlos públicos de forma permanente.
    """
    import mimetypes
    import os
    from django.conf import settings
    from django.http import FileResponse, Http404
    from django.shortcuts import redirect

    # Los adjuntos salientes los baja el proveedor (Meta/Twilio) con una URL firmada y con vencimiento;
    # el agente los ve con su sesión. Ya no son públicos permanentes.
    from core.media_firmada import ruta_firmada_valida
    saliente = ruta.startswith('whatsapp/salientes/')
    # Imágenes de emails/difusiones (flyers): públicas, las tiene que abrir el cliente de correo sin login.
    publico = ruta.startswith('email/')
    firmada = saliente and ruta_firmada_valida(request.GET.get('sig', ''), ruta)
    if not firmada and not publico and not request.user.is_authenticated:
        if saliente:
            raise Http404
        return redirect(f'{settings.LOGIN_URL}?next={request.path}')
    from core.storage import media_root
    base = os.path.realpath(media_root())
    completa = os.path.realpath(os.path.join(base, ruta))
    if not completa.startswith(base + os.sep) or not os.path.isfile(completa):
        raise Http404
    tipo, _ = mimetypes.guess_type(completa)
    resp = FileResponse(open(completa, 'rb'), content_type=tipo or 'application/octet-stream')
    resp['Cache-Control'] = 'public, max-age=86400' if publico else 'private, max-age=3600'
    return resp
