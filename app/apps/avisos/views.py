from django import forms
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View

from core.permisos import PermisoRequeridoMixin

from .models import EVENTOS, TEXTOS, EnvioAviso, ReglaAviso


class ReglaForm(forms.ModelForm):
    class Meta:
        model = ReglaAviso
        fields = ['nombre', 'activa', 'evento', 'embudo', 'minutos', 'evento_nombre', 'al_vendedor', 'a_supervisores',
                  'usuarios', 'emails_extra', 'whatsapps_extra', 'por_campanita', 'por_email', 'por_whatsapp', 'linea',
                  'texto', 'desde', 'hasta']
        widgets = {'usuarios': forms.CheckboxSelectMultiple, 'texto': forms.Textarea(attrs={'rows': 3}),
                   'desde': forms.TimeInput(attrs={'type': 'time'}, format='%H:%M'),
                   'hasta': forms.TimeInput(attrs={'type': 'time'}, format='%H:%M')}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.users.models import User
        self.fields['usuarios'].queryset = User.objects.filter(is_active=True).order_by('first_name', 'username')
        self.fields['usuarios'].label_from_instance = lambda u: f'{u.display_name}' + ('' if u.whatsapp else ' (sin WhatsApp)')
        for f in self.fields.values():
            w = f.widget
            if isinstance(w, forms.CheckboxInput):
                w.attrs['class'] = 'form-check-input'
            elif isinstance(w, forms.CheckboxSelectMultiple):
                continue
            elif isinstance(w, forms.Select):
                w.attrs['class'] = 'form-select'
            else:
                w.attrs['class'] = 'form-control'

    def clean(self):
        d = super().clean()
        if d.get('por_whatsapp') and not d.get('linea'):
            self.add_error('linea', 'Elegí por qué línea mandar los avisos de WhatsApp.')
        if not (d.get('por_campanita') or d.get('por_email') or d.get('por_whatsapp')):
            self.add_error('por_campanita', 'Elegí al menos un canal.')
        if not (d.get('al_vendedor') or d.get('a_supervisores') or d.get('usuarios') or d.get('emails_extra')
                or d.get('whatsapps_extra')):
            self.add_error('al_vendedor', 'Elegí a quién avisar.')
        if bool(d.get('desde')) != bool(d.get('hasta')):
            self.add_error('hasta', 'Completá las dos horas (o ninguna).')
        return d


class ListaView(PermisoRequeridoMixin, View):
    permiso = 'automatizaciones'

    def get(self, request):
        return render(request, 'avisos/lista.html', {
            'reglas': ReglaAviso.objects.select_related('embudo', 'linea').prefetch_related('usuarios'),
            'envios': EnvioAviso.objects.select_related('regla')[:60], 'seccion': 'avisos',
        })


class ReglaView(PermisoRequeridoMixin, View):
    permiso = 'automatizaciones'

    def get(self, request, pk=None):
        regla = get_object_or_404(ReglaAviso, pk=pk) if pk else None
        inicial = {} if regla else {'evento': request.GET.get('evento') or 'humano'}
        return self._render(request, ReglaForm(instance=regla, initial=inicial), regla)

    def post(self, request, pk=None):
        regla = get_object_or_404(ReglaAviso, pk=pk) if pk else None
        if regla and request.POST.get('eliminar'):
            regla.delete()
            messages.success(request, 'Aviso eliminado.')
            return redirect('avisos:lista')
        form = ReglaForm(request.POST, instance=regla)
        if not form.is_valid():
            return self._render(request, form, regla)
        regla = form.save()
        if request.POST.get('probar'):
            from .services import aplicar
            aplicar(regla, motivo='(aviso de prueba)')
            messages.info(request, 'Mandé un aviso de prueba con datos vacíos. Fijate en la campanita, tu email o WhatsApp.')
            return redirect('avisos:editar', pk=regla.pk)
        messages.success(request, f'Aviso «{regla}» guardado.')
        return redirect('avisos:lista')

    def _render(self, request, form, regla):
        return render(request, 'avisos/regla.html', {'form': form, 'regla': regla, 'seccion': 'avisos',
                                                     'textos': TEXTOS, 'eventos': EVENTOS})
