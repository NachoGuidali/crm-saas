from django.urls import path

from . import views

app_name = 'agente'

urlpatterns = [
    path('', views.ConfigView.as_view(), name='config'),
    path('conocimiento/', views.ConocimientoView.as_view(), name='conocimiento'),
    path('conocimiento/nuevo/', views.DocumentoView.as_view(), name='documento_nuevo'),
    path('conocimiento/<int:pk>/', views.DocumentoView.as_view(), name='documento'),
    path('lineas/', views.LineasView.as_view(), name='lineas'),
    path('simulador/', views.SimuladorView.as_view(), name='simulador'),
    path('registro/', views.RegistroView.as_view(), name='registro'),
]

publicas = ([path('<slug:slug>/', views.EnlacePublicoView.as_view(), name='enlace')], 'agente_publico')
