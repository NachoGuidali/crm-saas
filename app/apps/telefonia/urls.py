from django.urls import path

from . import views

app_name = 'telefonia'

urlpatterns = [
    path('config/', views.ConfigView.as_view(), name='config'),
    path('llamadas/', views.LlamadasView.as_view(), name='llamadas'),
    path('campanias/', views.CampaniaListView.as_view(), name='campanias'),
    path('campanias/nueva/', views.CampaniaEditarView.as_view(), name='campania_nueva'),
    path('campanias/<int:pk>/', views.CampaniaDetalleView.as_view(), name='campania'),
    path('campanias/<int:pk>/editar/', views.CampaniaEditarView.as_view(), name='campania_editar'),
    path('webhook/<str:token>/', views.AnuraWebhookView.as_view(), name='webhook'),
]

# Rutas exactas de la especificación (Spec_Implementacion_Anura_CRM_RAS)
api_urlpatterns = [
    path('api/integrations/anura/webhook', views.AnuraWebhookView.as_view(), name='anura_webhook_spec'),
    path('api/telephony/dial', views.DialView.as_view(), name='dial'),
    path('api/telephony/hangup/<str:call_id>', views.HangupView.as_view(), name='hangup'),
    path('api/telephony/estado/', views.EstadoView.as_view(), name='estado'),
    path('api/telephony/calificar/<int:pk>', views.CalificarView.as_view(), name='calificar'),
    path('api/telephony/softphone/', views.SoftphoneView.as_view(), name='softphone'),
    path('api/telephony/softphone/registro/', views.SoftphoneRegistroView.as_view(), name='softphone_registro'),
    path('api/telephony/softphone/corto/', views.SoftphoneCortoView.as_view(), name='softphone_corto'),
    path('api/telephony/quien/', views.QuienLlamaView.as_view(), name='quien'),
    path('api/telephony/agentes/', views.AgentesView.as_view(), name='agentes'),
    path('telefono/', views.MarcoView.as_view(), name='marco'),
]
