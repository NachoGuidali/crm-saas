from django.urls import path

from . import views

app_name = 'avisos'

urlpatterns = [
    path('', views.ListaView.as_view(), name='lista'),
    path('nueva/', views.ReglaView.as_view(), name='nueva'),
    path('<int:pk>/', views.ReglaView.as_view(), name='editar'),
]
