from django.urls import path

from . import views

app_name = 'integraciones'

urlpatterns = [
    path('', views.ApiKeysView.as_view(), name='lista'),
    path('sms/webhook/<str:token>/', views.SMSWebhookView.as_view(), name='sms_webhook'),
]

api_urlpatterns = [
    path('api/v1/leads/', views.LeadCrearView.as_view(), name='api_leads'),
    path('api/v1/leads/buscar/', views.LeadBuscarView.as_view(), name='api_leads_buscar'),
    path('api/v1/whatsapp/enviar/', views.EnviarWhatsAppView.as_view(), name='api_whatsapp_enviar'),
]
