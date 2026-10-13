from django.urls import path

from . import views

app_name = 'reportes'

urlpatterns = [
    path('', views.DashboardView.as_view(), name='dashboard'),
    path('ranking.csv', views.ExportarRankingView.as_view(), name='ranking_csv'),
    path('actividad.csv', views.ExportarActividadView.as_view(), name='actividad_csv'),
    path('envios/', views.CalidadEnviosView.as_view(), name='calidad_envios'),
    path('mis-numeros/', views.MisNumerosView.as_view(), name='mis_numeros'),
]
