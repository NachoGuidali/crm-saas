from django import forms

from apps.users.models import User
from .models import (AvisoRecurrente, Cuenta, EstadoProyecto, Proyecto, RecordatorioPersonal,
                     TareaPostventa)
from . import services


def _bootstrap(form):
    """Pone las clases de Bootstrap a los campos, como el resto de los forms del CRM."""
    for field in form.fields.values():
        w = field.widget
        if isinstance(w, (forms.CheckboxInput, forms.CheckboxSelectMultiple)):
            w.attrs.setdefault('class', 'form-check-input')
        elif isinstance(w, forms.SelectMultiple):
            w.attrs.setdefault('class', 'form-select')
        elif isinstance(w, forms.Select):
            w.attrs.setdefault('class', 'form-select')
        else:
            w.attrs.setdefault('class', 'form-control')


class CuentaForm(forms.ModelForm):
    class Meta:
        model = Cuenta
        fields = ['nombre', 'responsable', 'email', 'telefono', 'activa', 'notas']
        widgets = {'notas': forms.Textarea(attrs={'rows': 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['responsable'].queryset = User.objects.filter(is_active=True)
        # Los contactos se asocian/quitan desde la ficha de la cuenta (con buscador), no en este form.
        _bootstrap(self)


class ProyectoForm(forms.ModelForm):
    class Meta:
        model = Proyecto
        fields = ['nombre', 'descripcion', 'estado', 'responsable', 'monto', 'fecha_inicio', 'fecha_entrega']
        widgets = {
            'descripcion': forms.Textarea(attrs={'rows': 3}),
            'fecha_inicio': forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'),
            'fecha_entrega': forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['responsable'].queryset = User.objects.filter(is_active=True)
        self.fields['estado'].queryset = EstadoProyecto.objects.all()
        for f in ('fecha_inicio', 'fecha_entrega'):
            self.fields[f].input_formats = ['%Y-%m-%d']
        _bootstrap(self)


class EstadoProyectoForm(forms.ModelForm):
    class Meta:
        model = EstadoProyecto
        fields = ['nombre', 'color', 'final']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _bootstrap(self)


class TareaPostventaForm(forms.ModelForm):
    class Meta:
        model = TareaPostventa
        fields = ['titulo', 'descripcion', 'asignado_a', 'proyecto', 'vence_at']
        widgets = {
            'descripcion': forms.Textarea(attrs={'rows': 2}),
            'vence_at': forms.DateTimeInput(attrs={'type': 'datetime-local'}, format='%Y-%m-%dT%H:%M'),
        }

    def __init__(self, *args, cuenta=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['asignado_a'].queryset = User.objects.filter(is_active=True)
        self.fields['vence_at'].input_formats = ['%Y-%m-%dT%H:%M']
        if cuenta is not None:
            self.fields['proyecto'].queryset = cuenta.proyectos.all()
        self.fields['proyecto'].required = False
        _bootstrap(self)


class AvisoRecurrenteForm(forms.ModelForm):
    class Meta:
        model = AvisoRecurrente
        fields = ['contacto', 'linea', 'plantilla', 'mensaje', 'cada_meses', 'dia_mes']
        widgets = {'mensaje': forms.Textarea(attrs={'rows': 3})}

    def __init__(self, *args, cuenta=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.cuenta = cuenta
        from apps.whatsapp.models import LineaWhatsApp, Plantilla
        if cuenta is not None:
            self.fields['contacto'].queryset = cuenta.contactos.all()
        self.fields['contacto'].required = False
        self.fields['linea'].queryset = LineaWhatsApp.objects.filter(activa=True)
        self.fields['linea'].required = False
        self.fields['plantilla'].queryset = Plantilla.objects.all()
        self.fields['plantilla'].required = False
        _bootstrap(self)

    def clean(self):
        d = super().clean()
        if not d.get('plantilla') and not (d.get('mensaje') or '').strip():
            self.add_error('mensaje', 'Cargá un texto o elegí una plantilla.')
        return d

    def save(self, commit=True):
        aviso = super().save(commit=False)
        if self.cuenta is not None:
            aviso.cuenta = self.cuenta
        aviso.proximo_envio = services.primer_envio(aviso.cada_meses, aviso.dia_mes)
        if commit:
            aviso.save()
        return aviso


class RecordatorioForm(forms.ModelForm):
    class Meta:
        model = RecordatorioPersonal
        fields = ['titulo', 'fecha', 'dias_antes', 'canal', 'linea', 'nota']
        widgets = {
            'fecha': forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'),
            'nota': forms.Textarea(attrs={'rows': 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.whatsapp.models import LineaWhatsApp
        self.fields['fecha'].input_formats = ['%Y-%m-%d']
        self.fields['linea'].queryset = LineaWhatsApp.objects.filter(activa=True)
        self.fields['linea'].required = False
        self.fields['nota'].required = False
        _bootstrap(self)
