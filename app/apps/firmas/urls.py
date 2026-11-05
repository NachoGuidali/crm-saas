from django.urls import include, path
from django.views.decorators.csrf import csrf_exempt

from . import views

app_name = 'firmas'

urlpatterns = [
    path('', views.FormularioListView.as_view(), name='lista'),
    path('nuevo/', views.FormularioEditarView.as_view(), name='nuevo'),
    path('<int:pk>/', views.FormularioEditarView.as_view(), name='editar'),
    path('<int:pk>/vista-previa/', views.VistaPreviaView.as_view(), name='vista_previa'),
    path('enviar/<int:pk>/', views.EnviarView.as_view(), name='enviar'),
    path('solicitud/<int:pk>/anular/', views.AnularView.as_view(), name='anular'),
    path('solicitud/<int:pk>/pdf/', views.PdfView.as_view(), name='pdf'),
]

# Lo que abre el lead, sin login. El token (32 bytes aleatorios) es la llave.
publicas = ([
    path('<str:token>/', views.PublicoView.as_view(), name='formulario'),
    path('<str:token>/pdf/', views.PdfPublicoView.as_view(), name='pdf'),
], 'firmas_publico')

api_urlpatterns = [
    path('api/v1/firmas/', views.FirmaApiView.as_view(), name='api_firmas'),
]
