from django import forms
from django.db.models import Q

from core.phone import normalizar_telefono

from .models import (CampoPersonalizado, Contacto, Embudo, Etapa, Etiqueta, Oportunidad, ReglaAsignacion, ResultadoGestion, Tarea,
                     Tipificacion)


class BootstrapMixin:
    def _estilizar(self):
        for f in self.fields.values():
            w = f.widget
            if isinstance(w, (forms.CheckboxInput, forms.CheckboxSelectMultiple, forms.RadioSelect)):
                w.attrs.setdefault('class', 'form-check-input')
            elif isinstance(w, (forms.Select, forms.SelectMultiple)):
                w.attrs.setdefault('class', 'form-select')
            else:
                w.attrs.setdefault('class', 'form-control')


# ── Campos personalizados ───────────────────────────────────────────────────

PREFIJO_CP = 'cp__'


def campo_formulario(campo):
    """Campo de formulario de Django para un CampoPersonalizado."""
    T = CampoPersonalizado
    kw = {'label': campo.nombre, 'required': False, 'help_text': campo.ayuda}
    if campo.tipo == T.TIPO_TEXTO_LARGO:
        return forms.CharField(widget=forms.Textarea(attrs={'rows': 2}), **kw)
    if campo.tipo == T.TIPO_NUMERO:
        return forms.DecimalField(max_digits=18, decimal_places=4, **kw)
    if campo.tipo == T.TIPO_FECHA:
        return forms.DateField(widget=forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'), **kw)
    if campo.tipo == T.TIPO_SINO:
        return forms.TypedChoiceField(choices=[('', '—'), ('true', 'Sí'), ('false', 'No')], **kw)
    if campo.tipo == T.TIPO_LISTA:
        return forms.ChoiceField(choices=[('', '—')] + [(o, o) for o in campo.opciones], **kw)
    if campo.tipo == T.TIPO_EMAIL:
        return forms.EmailField(**kw)
    if campo.tipo == T.TIPO_URL:
        return forms.URLField(**kw)
    return forms.CharField(max_length=500, **kw)


def valor_para_guardar(campo, valor):
    """Valor del formulario → valor JSON guardado en datos_extra."""
    if valor in (None, ''):
        return None
    T = CampoPersonalizado
    if campo.tipo == T.TIPO_FECHA:
        return valor.isoformat()
    if campo.tipo == T.TIPO_NUMERO:
        return int(valor) if valor == int(valor) else float(valor)
    if campo.tipo == T.TIPO_SINO:
        return valor == 'true'
    if campo.tipo == T.TIPO_TELEFONO:
        return normalizar_telefono(str(valor)) or str(valor)
    return str(valor).strip()


def valor_para_form(campo, valor):
    if valor in (None, ''):
        return None
    if campo.tipo == CampoPersonalizado.TIPO_SINO:
        return 'true' if valor in (True, 'true', 'True', '1', 'si', 'sí') else 'false'
    if campo.tipo == CampoPersonalizado.TIPO_FECHA:
        from datetime import date
        try:
            return date.fromisoformat(str(valor)[:10])
        except ValueError:
            return None
    return valor


class CamposPersonalizadosMixin:
    """Agrega los campos personalizados activos al formulario y los guarda en datos_extra."""

    def agregar_campos_personalizados(self, datos_extra=None, embudo=None):
        # Los de tipo archivo se cargan aparte (subida o desde WhatsApp), no en el formulario
        self.campos_personalizados = [c for c in CampoPersonalizado.activos(embudo) if not c.es_archivo]
        datos_extra = datos_extra or {}
        for c in self.campos_personalizados:
            f = campo_formulario(c)
            if c.embudo_id and embudo is None:
                f.help_text = f'Solo para {c.embudo}. {f.help_text}'.strip()
            f.initial = valor_para_form(c, datos_extra.get(c.slug))
            self.fields[PREFIJO_CP + c.slug] = f

    def validar_requeridos(self, embudo=None):
        for c in getattr(self, 'campos_personalizados', []):
            if c.requerido and (c.embudo_id is None or (embudo and c.embudo_id == embudo.pk)):
                if self.cleaned_data.get(PREFIJO_CP + c.slug) in (None, ''):
                    self.add_error(PREFIJO_CP + c.slug, 'Este campo es obligatorio.')

    def valores_personalizados(self):
        valores = {}
        for c in getattr(self, 'campos_personalizados', []):
            valor = valor_para_guardar(c, self.cleaned_data.get(PREFIJO_CP + c.slug))
            valores[c.slug] = valor
        return valores

    def campos_personalizados_bound(self):
        return [self[PREFIJO_CP + c.slug] for c in getattr(self, 'campos_personalizados', [])]


class ContactoForm(CamposPersonalizadosMixin, BootstrapMixin, forms.ModelForm):
    class Meta:
        model = Contacto
        fields = ['nombre', 'telefono', 'telefono_alternativo', 'email', 'dni', 'fecha_nacimiento', 'localidad',
                  'provincia', 'fecha_atencion', 'especialidad_atencion', 'etiquetas', 'no_contactar']
        widgets = {
            'fecha_nacimiento': forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'),
            'fecha_atencion': forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'),
            'etiquetas': forms.SelectMultiple(attrs={'size': 4}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.agregar_campos_personalizados(self.instance.datos_extra if self.instance.pk else {})
        self._estilizar()

    def campos_base(self):
        return [f for f in self if not f.name.startswith(PREFIJO_CP)]

    def clean_telefono(self):
        tel = self.cleaned_data.get('telefono', '')
        if not tel:
            return ''
        norm = normalizar_telefono(tel)
        if not norm:
            raise forms.ValidationError('El teléfono no parece válido.')
        otro = Contacto.objects.filter(telefono=norm).exclude(pk=self.instance.pk).first()
        if otro:
            raise forms.ValidationError(f'Ya existe un contacto con ese teléfono: {otro.nombre} (#{otro.pk}).')
        return norm

    def clean(self):
        data = super().clean()
        if not (data.get('telefono') or data.get('email') or data.get('dni')):
            raise forms.ValidationError('Cargá al menos teléfono, email o DNI.')
        self.validar_requeridos()
        return data

    def save(self, commit=True):
        extra = dict(self.instance.datos_extra or {})
        for slug, valor in self.valores_personalizados().items():
            if valor is None:
                extra.pop(slug, None)
            else:
                extra[slug] = valor
        self.instance.datos_extra = extra
        return super().save(commit)


class NuevaOportunidadForm(CamposPersonalizadosMixin, BootstrapMixin, forms.Form):
    """Alta manual: contacto + oportunidad en un paso (con detección de duplicados)."""
    nombre = forms.CharField(max_length=200, label='Nombre y apellido')
    telefono = forms.CharField(max_length=40, label='Teléfono', required=False)
    email = forms.EmailField(required=False)
    dni = forms.CharField(max_length=12, required=False, label='DNI')
    fecha_atencion = forms.DateField(required=False, label='Fecha de atención',
                                     widget=forms.DateInput(attrs={'type': 'date'}))
    especialidad_atencion = forms.CharField(max_length=120, required=False, label='Motivo / especialidad')
    embudo = forms.ModelChoiceField(Embudo.objects.filter(activo=True))
    agente = forms.ModelChoiceField(queryset=None, required=False, label='Asignar a',
                                    help_text='Vacío = asignación automática del embudo.')
    valor = forms.DecimalField(required=False, max_digits=12, decimal_places=2, label='Valor / cuota ($)')
    pauta = forms.ModelChoiceField(queryset=None, required=False, label='Pauta / campaña',
                                   help_text='De qué publicidad vino (si corresponde).')
    nota = forms.CharField(required=False, widget=forms.Textarea(attrs={'rows': 2}), label='Nota inicial')

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.users.models import User
        from apps.pautas.models import Pauta
        self.fields['pauta'].queryset = Pauta.objects.filter(activa=True)
        self.fields['agente'].queryset = User.objects.filter(is_active=True)
        if user is not None and not user.tiene_permiso('reasignar'):
            self.fields['agente'].queryset = User.objects.filter(pk=user.pk)
            self.fields['agente'].initial = user.pk
            self.fields['agente'].help_text = ''
        if user is not None and not user.ve_todo:
            self.fields['embudo'].queryset = Embudo.objects.filter(activo=True, agentes=user)
        self.agregar_campos_personalizados()
        self._estilizar()

    def clean(self):
        data = super().clean()
        if data.get('telefono'):
            data['telefono'] = normalizar_telefono(data['telefono'])
            if not data['telefono']:
                self.add_error('telefono', 'El teléfono no parece válido.')
        if not (data.get('telefono') or data.get('email') or data.get('dni')):
            raise forms.ValidationError('Cargá al menos teléfono, email o DNI.')
        self.validar_requeridos(data.get('embudo'))
        return data


class TareaForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = Tarea
        fields = ['tipo', 'titulo', 'vence_at', 'prioridad', 'descripcion', 'asignado_a']
        widgets = {'vence_at': forms.DateTimeInput(attrs={'type': 'datetime-local'}, format='%Y-%m-%dT%H:%M'),
                   'descripcion': forms.Textarea(attrs={'rows': 2})}

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.users.models import User
        self.fields['asignado_a'].queryset = User.objects.filter(is_active=True)
        self.fields['asignado_a'].required = False
        if user is not None and not user.tiene_permiso('reasignar'):
            self.fields.pop('asignado_a')
        self._estilizar()


class EmbudoForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = Embudo
        fields = ['nombre', 'descripcion', 'color', 'activo', 'orden', 'agentes', 'supervisores', 'modo_asignacion',
                  'asignar_entre', 'sin_conectados', 'sla_minutos', 'sla_accion', 'sla_max_reasignaciones',
                  'respetar_horario', 'horario_desde', 'horario_hasta', 'fuera_de_horario', 'crear_tarea_al_asignar',
                  'linea_whatsapp', 'max_intentos_sin_respuesta', 'dias_inactividad_recordatorio',
                  'dias_estancado_alerta', 'notificar_venta_supervisores', 'reingreso_perdidos', 'exigir_resultado',
                  'vence_dias', 'vence_accion', 'vence_hasta_etapa', 'socios_a', 'socios_usuario', 'etiqueta_venta', 'clientes_de_otros']
        widgets = {
            'descripcion': forms.Textarea(attrs={'rows': 2}),
            'horario_desde': forms.TimeInput(attrs={'type': 'time'}, format='%H:%M'),
            'horario_hasta': forms.TimeInput(attrs={'type': 'time'}, format='%H:%M'),
            'agentes': forms.CheckboxSelectMultiple, 'supervisores': forms.CheckboxSelectMultiple,
        }

    DIAS = [(0, 'Lun'), (1, 'Mar'), (2, 'Mié'), (3, 'Jue'), (4, 'Vie'), (5, 'Sáb'), (6, 'Dom')]
    dias_habiles = forms.TypedMultipleChoiceField(choices=DIAS, coerce=int, required=False,
                                                  widget=forms.CheckboxSelectMultiple, label='Días de atención')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.users.models import User
        activos = User.objects.filter(is_active=True)
        self.fields['agentes'].queryset = activos
        self.fields['supervisores'].queryset = activos.exclude(rol=User.ROL_AGENTE)
        self.fields['dias_habiles'].initial = self.instance.dias_habiles or [0, 1, 2, 3, 4]
        from .models import Etapa
        self.fields['vence_hasta_etapa'].queryset = (
            Etapa.objects.filter(embudo=self.instance, tipo=Etapa.TIPO_NORMAL).order_by('orden')
            if self.instance.pk else Etapa.objects.none())
        self.fields['vence_hasta_etapa'].empty_label = 'Vence en cualquier etapa'
        self.fields['socios_usuario'].queryset = activos
        self.fields['socios_usuario'].empty_label = '—'
        self.fields['etiqueta_venta'].empty_label = 'Ninguna'
        self._estilizar()
        for nombre in ('agentes', 'supervisores', 'dias_habiles'):
            self.fields[nombre].widget.attrs['class'] = 'form-check-input'

    def save(self, commit=True):
        self.instance.dias_habiles = self.cleaned_data.get('dias_habiles') or []
        return super().save(commit)


class EtapaForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = Etapa
        fields = ['nombre', 'descripcion', 'color', 'tipo', 'marca_contacto_efectivo']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._estilizar()


class TipificacionForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = Tipificacion
        fields = ['resultado', 'categoria', 'nombre', 'descripcion', 'accion', 'requiere_nota', 'activa']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._estilizar()


class ResultadoGestionForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = ResultadoGestion
        fields = ['nombre', 'contactado', 'pide_fecha', 'mover_a', 'orden', 'activo']

    def __init__(self, *args, embudo=None, **kwargs):
        super().__init__(*args, **kwargs)
        from .models import Etapa
        self.fields['mover_a'].queryset = (Etapa.objects.filter(embudo=embudo, tipo=Etapa.TIPO_NORMAL).order_by('orden')
                                           if embudo else Etapa.objects.none())
        self.fields['mover_a'].empty_label = 'No mover'
        self._estilizar()


class EtiquetaForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = Etiqueta
        fields = ['nombre', 'color']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._estilizar()


class ImportacionForm(BootstrapMixin, forms.Form):
    archivo = forms.FileField(help_text='Excel (.xlsx) o CSV. La primera fila debe tener los títulos de las columnas.')
    embudo = forms.ModelChoiceField(Embudo.objects.filter(activo=True))
    fuente = forms.CharField(max_length=150, required=False, label='Nombre de la base',
                             help_text='Queda como origen de cada prospecto. Ej: "Centro médico – septiembre 2026".')

    def clean_archivo(self):
        f = self.cleaned_data['archivo']
        if not f.name.lower().endswith(('.xlsx', '.xlsm', '.csv', '.txt')):
            raise forms.ValidationError('Formato no soportado: subí un .xlsx o .csv')
        if f.size > 20 * 1024 * 1024:
            raise forms.ValidationError('El archivo supera los 20 MB.')
        return f

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._estilizar()


def etapas_de(embudo):
    return list(embudo.etapas.all().order_by('orden', 'pk'))


def tipificaciones_de(embudo):
    return list(Tipificacion.objects.filter(Q(embudo=embudo) | Q(embudo__isnull=True), activa=True)
                .order_by('resultado', 'orden', 'categoria', 'nombre'))


def oportunidad_estado_choices():
    return Oportunidad.ESTADO_CHOICES


class CampoPersonalizadoForm(BootstrapMixin, forms.ModelForm):
    opciones_texto = forms.CharField(required=False, label='Opciones', widget=forms.Textarea(attrs={'rows': 3}),
                                     help_text='Una opción por línea (solo para "Lista de opciones").')

    class Meta:
        model = CampoPersonalizado
        fields = ['nombre', 'tipo', 'ayuda', 'embudo', 'requerido', 'mostrar_en_tarjeta', 'mostrar_en_lista',
                  'filtrable', 'orden', 'activo']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['opciones_texto'].initial = '\n'.join(self.instance.opciones or [])
        self._estilizar()

    def clean(self):
        d = super().clean()
        opciones = [o.strip() for o in (d.get('opciones_texto') or '').splitlines() if o.strip()]
        if d.get('tipo') == CampoPersonalizado.TIPO_LISTA and not opciones:
            self.add_error('opciones_texto', 'Cargá al menos una opción.')
        self.instance.opciones = list(dict.fromkeys(opciones))
        from django.utils.text import slugify
        nombre = slugify(d.get('nombre') or '').replace('-', '_')
        reservados = {'nombre', 'telefono', 'email', 'dni', 'localidad', 'provincia', 'fecha_atencion', 'embudo',
                      'etapa', 'agente', 'primer_nombre', 'fuente', 'origen', 'valor', 'nota'}
        if not self.instance.pk and nombre in reservados:
            self.add_error('nombre', 'Ese nombre ya lo usa un campo fijo del CRM.')
        return d


class ReglaAsignacionForm(BootstrapMixin, forms.ModelForm):
    canales = forms.MultipleChoiceField(choices=Oportunidad.ORIGEN_CHOICES, required=False,
                                        widget=forms.CheckboxSelectMultiple, label='Canal de ingreso')
    textos = forms.CharField(
        required=False, label='El origen contiene', widget=forms.Textarea(attrs={'rows': 3}),
        help_text='Un texto por línea (ej: "instagram", "referidos"). Se busca dentro del origen / pauta / fuente del '
                  'lead, sin distinguir mayúsculas, acentos ni guiones.',
    )

    class Meta:
        model = ReglaAsignacion
        fields = ['nombre', 'activa', 'orden', 'canales', 'pautas', 'accion', 'agentes', 'asignar_entre', 'si_no_hay']
        widgets = {'pautas': forms.CheckboxSelectMultiple, 'agentes': forms.CheckboxSelectMultiple,
                   'accion': forms.RadioSelect}

    def __init__(self, *args, embudo=None, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.pautas.models import Pauta
        from apps.users.models import User
        self.embudo = embudo
        self.fields['agentes'].queryset = User.objects.filter(is_active=True).order_by('first_name', 'username')
        self.fields['pautas'].queryset = Pauta.objects.filter(Q(embudo=embudo) | Q(embudo__isnull=True)).order_by('nombre')
        self.fields['textos'].initial = '\n'.join(self.instance.textos_origen or [])
        self.fields['canales'].initial = self.instance.canales or []
        self._estilizar()
        self.fields['accion'].widget.attrs.pop('class', None)

    def clean(self):
        d = super().clean()
        self.instance.textos_origen = [t.strip() for t in (d.get('textos') or '').splitlines() if t.strip()]
        self.instance.canales = d.get('canales') or []
        if not (self.instance.textos_origen or self.instance.canales or d.get('pautas')):
            raise forms.ValidationError('Cargá al menos una condición (canal, pauta o texto del origen).')
        if d.get('accion') == ReglaAsignacion.ACCION_AGENTES and not d.get('agentes'):
            self.add_error('agentes', 'Elegí al menos un agente.')
        return d

    def save(self, commit=True):
        self.instance.embudo = self.embudo
        return super().save(commit)
