from django.urls import path

from . import views, views_correo

app_name = 'automatizaciones'

urlpatterns = [
    path('', views.AccionListView.as_view(), name='lista'),
    path('nueva/', views.AccionEditarView.as_view(), name='nueva'),
    path('<int:pk>/', views.AccionEditarView.as_view(), name='editar'),
    path('ejecuciones/', views.EjecucionesView.as_view(), name='ejecuciones'),
    path('plantillas-email/', views_correo.PlantillasEmailView.as_view(), name='plantillas_email'),
    path('plantillas-email/<int:pk>/', views_correo.PlantillasEmailView.as_view(), name='plantilla_email'),
    path('correo/', views_correo.ConfigEmailView.as_view(), name='config_email'),
    path('difusiones/', views_correo.DifusionListView.as_view(), name='difusiones'),
    path('difusiones/<int:pk>/', views_correo.DifusionDetalleView.as_view(), name='difusion'),
]
