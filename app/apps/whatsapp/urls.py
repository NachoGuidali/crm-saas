from django.urls import path

from . import views

app_name = 'whatsapp'

urlpatterns = [
    path('', views.InboxView.as_view(), name='inbox'),
    path('lista/', views.ListaView.as_view(), name='lista'),
    path('enviar/', views.EnviarView.as_view(), name='enviar_nuevo'),
    path('conversacion/<int:pk>/mensajes/', views.MensajesView.as_view(), name='mensajes'),
    path('mensaje/<int:pk>/guardar-adjunto/', views.GuardarAdjuntoView.as_view(), name='guardar_adjunto'),
    path('conversacion/<int:pk>/enviar/', views.EnviarView.as_view(), name='enviar'),
    path('conversacion/<int:pk>/<slug:accion>/', views.ConversacionAccionView.as_view(), name='conv_accion'),
    path('lineas/', views.LineaListView.as_view(), name='lineas'),
    path('lineas/nueva/', views.LineaEditarView.as_view(), name='linea_nueva'),
    path('lineas/<int:pk>/', views.LineaEditarView.as_view(), name='linea_editar'),
    path('lineas/<int:pk>/conexion/', views.LineaConexionView.as_view(), name='linea_conexion'),
    path('plantillas/', views.PlantillaListView.as_view(), name='plantillas'),
    path('plantillas/nueva/', views.PlantillaEditarView.as_view(), name='plantilla_nueva'),
    path('plantillas/<int:pk>/', views.PlantillaEditarView.as_view(), name='plantilla_editar'),
    path('respuestas/', views.RespuestaRapidaView.as_view(), name='respuestas'),
    path('webhook/<slug:proveedor>/<str:key>/', views.WebhookView.as_view(), name='webhook'),
]
