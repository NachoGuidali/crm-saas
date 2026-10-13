from django.urls import path

from . import views

app_name = 'pautas'

urlpatterns = [
    path('', views.AnalisisView.as_view(), name='analisis'),
    path('nueva/', views.PautaEditarView.as_view(), name='nueva'),
    path('<int:pk>/', views.PautaDetalleView.as_view(), name='detalle'),
    path('<int:pk>/editar/', views.PautaEditarView.as_view(), name='editar'),
]
