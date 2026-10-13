from django.conf import settings
from django.contrib import admin
from django.urls import include, path, re_path

from core.views import media_protegida

from apps.integraciones.urls import api_urlpatterns as api_integraciones
from django.views.decorators.csrf import csrf_exempt

from apps.automatizaciones import views as auto_views
from apps.automatizaciones import views_correo as auto_correo
from apps.integraciones import landing
from apps.reportes import views as reportes_views
from apps.telefonia.urls import api_urlpatterns as api_telefonia

urlpatterns = [
    path('', reportes_views.inicio, name='inicio'),
    path('pulso/', reportes_views.PulsoView.as_view(), name='pulso'),
    path('demo/formulario/', landing.LandingView.as_view(), name='demo_formulario'),
    path('demo/gracias/', landing.gracias, name='demo_gracias'),
    path('pulso/salir/', reportes_views.SalirPestanaView.as_view(), name='pulso_salir'),
    path('e/o/<str:token>.gif', auto_views.email_abierto, name='email_abierto'),
    path('e/c/<str:token>/', auto_views.email_clic, name='email_clic'),
    path('e/baja/<str:token>/', csrf_exempt(auto_correo.baja_email), name='email_baja'),
    path('admin/', admin.site.urls),
    path('usuarios/', include('apps.users.urls')),
    path('reportes/', include('apps.reportes.urls')),
    path('whatsapp/', include('apps.whatsapp.urls')),
    path('telefonia/', include('apps.telefonia.urls')),
    path('automatizaciones/', include('apps.automatizaciones.urls')),
    path('integraciones/', include('apps.integraciones.urls')),
    path('pautas/', include('apps.pautas.urls')),
    path('', include('apps.crm.urls')),
] + api_telefonia + api_integraciones

urlpatterns.insert(0, re_path(r'^media/(?P<ruta>.+)$', media_protegida, name='media'))

handler403 = 'core.views.error_403'
handler404 = 'core.views.error_404'
handler500 = 'core.views.error_500'
