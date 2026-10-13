from django.urls import path

from . import views

app_name = 'panel'

urlpatterns = [
    path('login/', views.PanelLoginView.as_view(), name='login'),
    path('logout/', views.PanelLogoutView.as_view(), name='logout'),
    path('password/', views.PanelPasswordView.as_view(), name='password'),
    path('', views.ClienteListView.as_view(), name='clientes'),
    path('clientes/nuevo/', views.ClienteNuevoView.as_view(), name='cliente_nuevo'),
    path('clientes/<int:pk>/', views.ClienteDetalleView.as_view(), name='cliente'),
    path('pagos/', views.PagoListView.as_view(), name='pagos'),
    path('auditoria/', views.AuditoriaPanelView.as_view(), name='auditoria'),
    path('planes/', views.PlanListView.as_view(), name='planes'),
    path('planes/nuevo/', views.PlanEditarView.as_view(), name='plan_nuevo'),
    path('planes/<int:pk>/', views.PlanEditarView.as_view(), name='plan_editar'),
]
