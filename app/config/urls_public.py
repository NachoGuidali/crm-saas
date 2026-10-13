"""
URLs del dominio principal (schema público): la pantalla para ingresar y el panel de la plataforma.
El CRM de cada cliente se sirve en su subdominio con config/urls.py.
"""
from django.urls import include, path
from django.views.generic import RedirectView

from apps.clientes import views as clientes_views

urlpatterns = [
    path('', RedirectView.as_view(pattern_name='ingresar', permanent=False)),
    path('ingresar/', clientes_views.IngresarView.as_view(), name='ingresar'),
    path('panel/', include('apps.clientes.urls')),
    path('mercadopago/webhook/', clientes_views.MercadoPagoWebhookView.as_view(), name='mercadopago_webhook'),
]

handler403 = 'core.views.error_403'
handler404 = 'core.views.error_404'
handler500 = 'core.views.error_500'
