from django.urls import path

from . import views

app_name = 'users'

urlpatterns = [
    path('login/', views.LoginView.as_view(), name='login'),
    path('logout/', views.LogoutView.as_view(), name='logout'),
    path('recuperar/', views.RecuperoView.as_view(), name='recupero'),
    path('recuperar/enviado/', views.RecuperoEnviadoView.as_view(), name='recupero_enviado'),
    path('recuperar/<uidb64>/<token>/', views.RecuperoConfirmarView.as_view(), name='recupero_confirmar'),
    path('recuperar/listo/', views.RecuperoListoView.as_view(), name='recupero_listo'),
    path('password/', views.CambiarPasswordView.as_view(), name='password_change'),
    path('perfil/', views.PerfilView.as_view(), name='perfil'),
    path('notificaciones/', views.NotificacionesView.as_view(), name='notificaciones'),
    path('', views.UsuarioListView.as_view(), name='lista'),
    path('nuevo/', views.UsuarioEditarView.as_view(), name='nuevo'),
    path('<int:pk>/', views.UsuarioEditarView.as_view(), name='editar'),
    path('<int:pk>/acceso/', views.UsuarioAccesoView.as_view(), name='acceso'),
    path('auditoria/', views.AuditoriaView.as_view(), name='auditoria'),
    path('roles/', views.RolListView.as_view(), name='roles'),
    path('roles/nuevo/', views.RolEditarView.as_view(), name='rol_nuevo'),
    path('roles/<int:pk>/', views.RolEditarView.as_view(), name='rol_editar'),
]
