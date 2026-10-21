from django.urls import path

from . import views

app_name = 'cuentas'

urlpatterns = [
    path('', views.CuentaListView.as_view(), name='lista'),
    path('nueva/', views.CuentaEditarView.as_view(), name='nueva'),
    path('tablero/', views.TableroView.as_view(), name='tablero'),
    path('estados/', views.EstadosView.as_view(), name='estados'),
    path('proyecto/nuevo/', views.ProyectoEditarView.as_view(), name='proyecto_nuevo'),
    path('proyecto/<int:pk>/', views.ProyectoEditarView.as_view(), name='proyecto_editar'),
    path('proyecto/<int:pk>/mover/', views.ProyectoMoverView.as_view(), name='proyecto_mover'),
    path('tarea/nueva/', views.TareaNuevaView.as_view(), name='tarea_nueva'),
    path('tarea/<int:pk>/completar/', views.TareaCompletarView.as_view(), name='tarea_completar'),
    path('aviso/nuevo/', views.AvisoNuevaView.as_view(), name='aviso_nuevo'),
    path('aviso/<int:pk>/', views.AvisoAccionView.as_view(), name='aviso_accion'),
    path('<int:pk>/', views.CuentaDetailView.as_view(), name='detalle'),
    path('<int:pk>/editar/', views.CuentaEditarView.as_view(), name='editar'),
]
