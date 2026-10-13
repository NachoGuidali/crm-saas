from django import forms
from django.core.exceptions import ValidationError

from .models import Cliente, Pago, Plan
from .services import validar_subdominio


class _Bootstrap:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            css = 'form-select' if isinstance(field.widget, forms.Select) else 'form-control'
            if isinstance(field.widget, forms.CheckboxInput):
                css = 'form-check-input'
            field.widget.attrs.setdefault('class', css)


class NuevoClienteForm(_Bootstrap, forms.Form):
    nombre = forms.CharField(label='Empresa', max_length=150)
    subdominio = forms.CharField(label='Subdominio', max_length=40,
                                 help_text='Con el que entra al CRM: subdominio.tudominio.com')
    plan = forms.ModelChoiceField(queryset=Plan.objects.filter(activo=True), required=False, empty_label='Sin plan')
    estado = forms.ChoiceField(choices=[(Cliente.ESTADO_PRUEBA, 'En prueba'), (Cliente.ESTADO_ACTIVO, 'Activo')],
                               initial=Cliente.ESTADO_PRUEBA)
    admin_nombre = forms.CharField(label='Nombre del administrador', max_length=150)
    admin_email = forms.EmailField(label='Email del administrador')
    admin_usuario = forms.CharField(label='Usuario', max_length=150, initial='admin')
    admin_password = forms.CharField(
        label='Contraseña', required=False, widget=forms.PasswordInput(render_value=False),
        help_text='Vacío = se genera una temporal y se le pide cambiarla al primer ingreso.',
    )
    contacto_telefono = forms.CharField(label='Teléfono de contacto', max_length=40, required=False)

    def clean_subdominio(self):
        try:
            return validar_subdominio(self.cleaned_data['subdominio'])
        except ValidationError as e:
            raise forms.ValidationError(e.messages)


class ClienteForm(_Bootstrap, forms.ModelForm):
    class Meta:
        model = Cliente
        fields = ['nombre', 'estado', 'plan', 'max_usuarios', 'max_lineas_whatsapp', 'max_wa_dia', 'prueba_hasta',
                  'cobro', 'precio_acordado', 'pagado_hasta', 'suspension_automatica',
                  'contacto_nombre', 'contacto_email', 'contacto_telefono', 'notas']
        widgets = {'prueba_hasta': forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'),
                   'pagado_hasta': forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'),
                   'notas': forms.Textarea(attrs={'rows': 3})}
        help_texts = {'contacto_email': 'Le llegan los avisos de vencimiento.'}


class PagoForm(_Bootstrap, forms.Form):
    monto = forms.DecimalField(max_digits=12, decimal_places=2, min_value=0)
    meses = forms.IntegerField(min_value=1, max_value=24, initial=1, label='Meses que paga')
    periodo_desde = forms.DateField(label='Cubre desde', widget=forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'),
                                    help_text='Sugerido: el vencimiento actual (mantiene el ciclo).')
    fecha = forms.DateField(label='Fecha del pago', widget=forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'))
    medio = forms.ChoiceField(choices=Pago.MEDIO_CHOICES)
    referencia = forms.CharField(max_length=120, required=False, help_text='Nº de operación o comprobante.')
    notas = forms.CharField(max_length=255, required=False)
    reactivar = forms.BooleanField(required=False, initial=True, label='Reactivar el acceso si está suspendido')


class SuscripcionMPForm(_Bootstrap, forms.Form):
    email_pagador = forms.EmailField(label='Email de la cuenta de Mercado Pago del cliente',
                                     help_text='Tiene que ser el email con el que el cliente entra a Mercado Pago.')


class LinkPagoMPForm(_Bootstrap, forms.Form):
    meses = forms.IntegerField(min_value=1, max_value=24, initial=1, label='Meses')


class PlanForm(_Bootstrap, forms.ModelForm):
    class Meta:
        model = Plan
        fields = ['nombre', 'precio_mensual', 'max_usuarios', 'max_lineas_whatsapp', 'max_wa_dia', 'activo']


class IngresarForm(_Bootstrap, forms.Form):
    empresa = forms.CharField(label='Subdominio de tu empresa', max_length=40)
