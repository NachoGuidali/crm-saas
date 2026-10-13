from django import forms
from django.contrib.auth.forms import AuthenticationForm, PasswordResetForm, SetPasswordForm

from .models import PERMISOS_DICT, RolPersonalizado, User


def _estilizar(form):
    for f in form.fields.values():
        w = f.widget
        if isinstance(w, forms.CheckboxInput):
            w.attrs.setdefault('class', 'form-check-input')
        elif isinstance(w, forms.Select):
            w.attrs.setdefault('class', 'form-select')
        elif not isinstance(w, forms.CheckboxSelectMultiple):
            w.attrs.setdefault('class', 'form-control')


class LoginForm(AuthenticationForm):
    username = forms.CharField(label='Usuario o email', widget=forms.TextInput(attrs={'autofocus': True,
                                                                                      'autocomplete': 'username'}))
    password = forms.CharField(label='Contraseña', strip=False,
                               widget=forms.PasswordInput(attrs={'autocomplete': 'current-password'}))
    error_messages = {
        'invalid_login': 'Usuario o contraseña incorrectos.',
        'inactive': 'Esta cuenta está desactivada. Consultá con un administrador.',
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _estilizar(self)


class RecuperoForm(PasswordResetForm):
    email = forms.EmailField(label='Email de tu cuenta', widget=forms.EmailInput(attrs={'autofocus': True}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _estilizar(self)


class NuevaPasswordForm(SetPasswordForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _estilizar(self)


class UsuarioForm(forms.ModelForm):
    MODO_INVITAR = 'invitar'
    MODO_MANUAL = 'manual'

    modo_password = forms.ChoiceField(
        choices=[(MODO_INVITAR, 'Enviar email para que defina su contraseña'),
                 (MODO_MANUAL, 'Definir contraseña temporal ahora')],
        initial=MODO_INVITAR, widget=forms.RadioSelect, required=False, label='Contraseña',
    )
    password_temporal = forms.CharField(required=False, label='Contraseña temporal', widget=forms.PasswordInput,
                                        help_text='Se le pedirá cambiarla al ingresar por primera vez.')

    class Meta:
        model = User
        fields = ['username', 'first_name', 'last_name', 'email', 'telefono', 'rol', 'rol_custom', 'disponible',
                  'is_active', 'avatar']
        labels = {'first_name': 'Nombre', 'last_name': 'Apellido', 'is_active': 'Activo (puede ingresar)',
                  'username': 'Usuario'}

    def __init__(self, *args, editor=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.editor = editor
        self.fields['email'].required = True
        if self.instance.pk:
            self.fields.pop('modo_password')
            self.fields.pop('password_temporal')
        if editor is not None and not editor.is_admin:
            # Un supervisor con permiso de usuarios no puede crear administradores
            self.fields['rol'].choices = [c for c in User.ROL_CHOICES if c[0] != User.ROL_ADMIN]
        _estilizar(self)
        if 'modo_password' in self.fields:
            self.fields['modo_password'].widget.attrs['class'] = 'form-check-input'

    def clean_email(self):
        email = (self.cleaned_data.get('email') or '').strip().lower()
        if User.objects.filter(email__iexact=email).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError('Ya hay otro usuario con ese email.')
        return email

    def clean_rol(self):
        rol = self.cleaned_data['rol']
        if rol == User.ROL_ADMIN and self.editor is not None and not self.editor.is_admin:
            raise forms.ValidationError('Solo un administrador puede crear otro administrador.')
        return rol

    def clean(self):
        data = super().clean()
        if not self.instance.pk and data.get('modo_password') == self.MODO_MANUAL:
            pwd = data.get('password_temporal')
            if not pwd:
                self.add_error('password_temporal', 'Indicá una contraseña temporal.')
            else:
                from django.contrib.auth.password_validation import validate_password
                try:
                    validate_password(pwd)
                except forms.ValidationError as e:
                    self.add_error('password_temporal', e)
        return data


class PerfilForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ['first_name', 'last_name', 'email', 'telefono', 'avatar']
        labels = {'first_name': 'Nombre', 'last_name': 'Apellido'}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _estilizar(self)


class RolForm(forms.ModelForm):
    class Meta:
        model = RolPersonalizado
        fields = ['nombre', 'descripcion']
        widgets = {'descripcion': forms.Textarea(attrs={'rows': 2})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _estilizar(self)

    def permisos_validos(self, lista):
        return [p for p in lista if p in PERMISOS_DICT]
