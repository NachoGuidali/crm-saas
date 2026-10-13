import logging
import secrets

from django.contrib import messages
from django.contrib.auth import login, logout, update_session_auth_hash
from django.contrib.auth import views as auth_views
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View

from core import tenancy
from core.permisos import AdminRequeridoMixin, PermisoRequeridoMixin
from core.utils import paginar

from . import services
from .forms import LoginForm, NuevaPasswordForm, PerfilForm, RecuperoForm, RolForm, UsuarioForm
from django.db import models
from core.auditoria import auditar
from .models import NotificacionInterna, RegistroAuditoria, RolPersonalizado, User, permisos_agrupados

logger = logging.getLogger('apps.users')


# ── Acceso ──────────────────────────────────────────────────────────────────

class LoginView(View):
    template_name = 'users/login.html'

    def get(self, request):
        if request.user.is_authenticated:
            return redirect('inicio')
        return render(request, self.template_name, {'form': LoginForm(request)})

    def post(self, request):
        username = request.POST.get('username', '').strip()
        ip = services.ip_de(request)
        form = LoginForm(request, data=request.POST)
        if services.login_bloqueado(username, ip):
            form.errors.clear()
            messages.error(request, 'Demasiados intentos fallidos. Esperá unos minutos o recuperá tu contraseña.')
            return render(request, self.template_name, {'form': LoginForm(request), 'bloqueado': True})
        if form.is_valid():
            services.limpiar_login_fallido(username, ip)
            login(request, form.get_user())  # la presencia la marca la primera página (cada pestaña con su id)
            auditar(RegistroAuditoria.LOGIN, request=request, usuario=form.get_user(), ip=ip)
            destino = request.GET.get('next', '')
            if not url_has_allowed_host_and_scheme(destino, {request.get_host()}, request.is_secure()):
                destino = ''
            return redirect(destino or 'inicio')
        services.registrar_login_fallido(username, ip)
        logger.warning('Login fallido para "%s" desde %s', username, ip)
        auditar(RegistroAuditoria.LOGIN_FALLIDO, request=request, username=username, ip=ip)
        return render(request, self.template_name, {'form': form})


class LogoutView(View):
    def post(self, request):
        from core import presencia
        if request.user.is_authenticated:
            presencia.desconectar(request.user)
        logout(request)
        return redirect('users:login')


class RecuperoView(auth_views.PasswordResetView):
    """El usuario pide el link de recupero. Por seguridad la respuesta es la misma exista o no el email."""
    template_name = 'users/recupero.html'
    form_class = RecuperoForm
    email_template_name = 'users/emails/reset_body.txt'
    html_email_template_name = 'users/emails/reset_body.html'
    subject_template_name = 'users/emails/reset_subject.txt'
    success_url = reverse_lazy('users:recupero_enviado')

    def get_extra_email_context(self):
        from django.conf import settings
        return {'crm_nombre': settings.CRM_NOMBRE, 'bienvenida': False}

    extra_email_context = property(get_extra_email_context)

    def form_valid(self, form):
        from django.core.cache import cache
        ip = services.ip_de(self.request)
        clave = f'recupero_ip_{ip}'
        if not cache.add(clave, 1, 3600):
            try:
                if cache.incr(clave) > 10:
                    messages.error(self.request, 'Demasiadas solicitudes. Probá de nuevo en una hora.')
                    return redirect('users:recupero')
            except ValueError:
                pass
        return super().form_valid(form)

    def post(self, request, *args, **kwargs):
        self.domain_override = tenancy.host_sitio()
        self.use_https = tenancy.usa_https()
        return super().post(request, *args, **kwargs)


class RecuperoEnviadoView(auth_views.PasswordResetDoneView):
    template_name = 'users/recupero_enviado.html'


class RecuperoConfirmarView(auth_views.PasswordResetConfirmView):
    template_name = 'users/recupero_confirmar.html'
    form_class = NuevaPasswordForm
    success_url = reverse_lazy('users:recupero_listo')

    def form_valid(self, form):
        response = super().form_valid(form)
        User.objects.filter(pk=form.user.pk).update(debe_cambiar_password=False)
        return response


class RecuperoListoView(auth_views.PasswordResetCompleteView):
    template_name = 'users/recupero_listo.html'


class CambiarPasswordView(LoginRequiredMixin, View):
    template_name = 'users/cambiar_password.html'

    def get(self, request):
        return render(request, self.template_name, {'form': self._form(request.user)})

    def post(self, request):
        form = self._form(request.user, request.POST)
        if form.is_valid():
            user = form.save()
            User.objects.filter(pk=user.pk).update(debe_cambiar_password=False)
            update_session_auth_hash(request, user)
            messages.success(request, 'Contraseña actualizada.')
            return redirect('inicio')
        return render(request, self.template_name, {'form': form})

    @staticmethod
    def _form(user, data=None):
        form = PasswordChangeForm(user, data)
        for f in form.fields.values():
            f.widget.attrs['class'] = 'form-control'
        return form


# ── Perfil ──────────────────────────────────────────────────────────────────

class PerfilView(LoginRequiredMixin, View):
    def get(self, request):
        return render(request, 'users/perfil.html', {'form': PerfilForm(instance=request.user)})

    def post(self, request):
        if 'disponible' in request.POST:
            request.user.disponible = request.POST['disponible'] == '1'
            request.user.save(update_fields=['disponible'])
            messages.success(request, 'Ahora estás ' + ('disponible para recibir prospectos.' if request.user.disponible
                                                        else 'NO disponible: no vas a recibir prospectos nuevos.'))
            return redirect(request.META.get('HTTP_REFERER') or 'users:perfil')
        form = PerfilForm(request.POST, request.FILES, instance=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, 'Perfil actualizado.')
            return redirect('users:perfil')
        return render(request, 'users/perfil.html', {'form': form})


# ── ABM de usuarios ─────────────────────────────────────────────────────────

class UsuarioListView(PermisoRequeridoMixin, View):
    permiso = 'usuarios'

    def get(self, request):
        qs = User.objects.select_related('rol_custom').annotate(
            abiertas=Count('oportunidades', filter=Q(oportunidades__estado='abierta'), distinct=True),
        ).order_by('-is_active', 'first_name', 'username')
        q = request.GET.get('q', '').strip()
        if q:
            qs = qs.filter(Q(username__icontains=q) | Q(first_name__icontains=q) | Q(last_name__icontains=q)
                           | Q(email__icontains=q))
        if request.GET.get('rol'):
            qs = qs.filter(rol=request.GET['rol'])
        return render(request, 'users/usuarios.html', {'page': paginar(request, qs, 50), 'q': q,
                                                       'roles': User.ROL_CHOICES, 'filtros': request.GET})


class UsuarioEditarView(PermisoRequeridoMixin, View):
    permiso = 'usuarios'

    def _objeto(self, request, pk):
        if pk is None:
            return None
        obj = get_object_or_404(User, pk=pk)
        if obj.is_admin and not request.user.is_admin:
            raise PermissionDenied('Solo un administrador puede editar a otro administrador.')
        return obj

    def get(self, request, pk=None):
        obj = self._objeto(request, pk)
        return self._render(request, obj, UsuarioForm(instance=obj, editor=request.user))

    def post(self, request, pk=None):
        obj = self._objeto(request, pk)
        estaba_activo = obj is not None and obj.is_active
        form = UsuarioForm(request.POST, request.FILES, instance=obj, editor=request.user)
        if not form.is_valid():
            return self._render(request, obj, form)
        if form.cleaned_data.get('is_active', True) and not estaba_activo:
            alcanzado, limite = tenancy.limite_alcanzado('usuarios')
            if alcanzado:
                form.add_error(None, f'Tu plan permite hasta {limite} usuarios activos. Desactivá alguno o '
                                     'pedinos ampliar el plan.')
                return self._render(request, obj, form)
        nuevo = obj is None
        rol_previo = obj.rol if obj is not None else None
        user = form.save(commit=False)
        if nuevo:
            if form.cleaned_data.get('modo_password') == UsuarioForm.MODO_MANUAL:
                user.set_password(form.cleaned_data['password_temporal'])
                user.debe_cambiar_password = True
            else:
                user.set_password(secrets.token_urlsafe(24))  # la define el usuario con el link
        user.save()
        self._guardar_asignaciones(request, user)
        if nuevo:
            auditar(RegistroAuditoria.USUARIO_ALTA, request=request,
                    objeto=f'{user.username} ({user.get_full_name()})', detalle=f'rol={user.rol}')
        elif estaba_activo and not user.is_active:
            auditar(RegistroAuditoria.USUARIO_BAJA, request=request, objeto=user.username)
        else:
            cambios = 'reactivado; ' if (not estaba_activo and user.is_active) else ''
            if rol_previo != user.rol:
                cambios += f'rol {rol_previo}→{user.rol}'
            auditar(RegistroAuditoria.USUARIO_CAMBIO, request=request, objeto=user.username,
                    detalle=cambios or 'datos')
        if nuevo and form.cleaned_data.get('modo_password') != UsuarioForm.MODO_MANUAL:
            if services.enviar_link_password(user, request, bienvenida=True):
                messages.success(request, f'Usuario creado. Le enviamos a {user.email} el link para definir su contraseña.')
            else:
                messages.warning(request, 'Usuario creado, pero no se pudo enviar el email. Usá "Enviar link de acceso".')
        else:
            messages.success(request, 'Usuario guardado.')
        if obj is not None and not user.is_active:
            self._al_desactivar(request, user)
        return redirect('users:editar', pk=user.pk)

    def _guardar_asignaciones(self, request, user):
        """Embudos y líneas de WhatsApp en los que trabaja + interno de Anura."""
        from apps.crm.models import Embudo
        from apps.telefonia.models import InternoAnura
        from apps.whatsapp.models import LineaWhatsApp
        if 'embudos_enviados' in request.POST:
            user.embudos.set(Embudo.objects.filter(pk__in=request.POST.getlist('embudos')))
        if 'lineas_enviadas' in request.POST:
            user.lineas_whatsapp.set(LineaWhatsApp.objects.filter(pk__in=request.POST.getlist('lineas')))
        if 'interno_anura' in request.POST:
            interno = request.POST.get('interno_anura', '').strip()
            alias = request.POST.get('alias_anura', '')
            if interno:
                conflicto = InternoAnura.conflicto([interno] + InternoAnura.limpiar_alias(alias), user)
                if conflicto:
                    messages.error(request, f'"{conflicto[0]}" ya está asignado a {conflicto[1].usuario}.')
                else:
                    InternoAnura.objects.update_or_create(usuario=user, defaults={
                        'interno': interno, 'alias': alias, 'activo': True})
            else:
                InternoAnura.objects.filter(usuario=user).delete()
            from django.core.cache import cache
            cache.delete(f'tiene_interno_{user.pk}')

    def _al_desactivar(self, request, user):
        from apps.crm.models import Oportunidad
        abiertas = Oportunidad.objects.filter(agente=user, estado__in=Oportunidad.ESTADOS_ACTIVOS).count()
        if abiertas:
            messages.warning(request, f'{user.display_name} tiene {abiertas} oportunidades activas: '
                                      'redistribuilas desde Supervisión.')

    def _render(self, request, obj, form):
        from apps.crm.models import Embudo
        from apps.telefonia.models import InternoAnura
        from apps.whatsapp.models import LineaWhatsApp
        return render(request, 'users/usuario_form.html', {
            'obj': obj, 'form': form, 'embudos': Embudo.objects.filter(activo=True),
            'lineas': LineaWhatsApp.objects.filter(activa=True),
            'embudos_sel': set(obj.embudos.values_list('pk', flat=True)) if obj else set(),
            'lineas_sel': set(obj.lineas_whatsapp.values_list('pk', flat=True)) if obj else set(),
            'interno': InternoAnura.objects.filter(usuario=obj).first() if obj else None,
            'roles_custom': RolPersonalizado.objects.all(),
        })


class UsuarioAccesoView(PermisoRequeridoMixin, View):
    """Acciones de acceso: enviar link, forzar contraseña temporal, desbloquear."""
    permiso = 'usuarios'

    def post(self, request, pk):
        user = get_object_or_404(User, pk=pk)
        if user.is_admin and not request.user.is_admin:
            raise PermissionDenied
        accion = request.POST.get('accion')
        if accion == 'enviar_link':
            if services.enviar_link_password(user, request):
                messages.success(request, f'Link de acceso enviado a {user.email}.')
            else:
                messages.error(request, 'No se pudo enviar: el usuario no tiene email o está inactivo.')
        elif accion == 'temporal':
            temporal = secrets.token_urlsafe(8)
            user.set_password(temporal)
            user.debe_cambiar_password = True
            user.save(update_fields=['password', 'debe_cambiar_password'])
            messages.success(request, f'Contraseña temporal de {user.username}: {temporal}  '
                                      '(se le pedirá cambiarla al ingresar).')
        return redirect('users:editar', pk=user.pk)


# ── Roles personalizados ────────────────────────────────────────────────────

class RolListView(AdminRequeridoMixin, View):
    def get(self, request):
        return render(request, 'users/roles.html', {
            'roles': RolPersonalizado.objects.annotate(n=Count('usuarios')),
            'permisos_por_rol': [(label, sorted(User.PERMISOS_POR_ROL[rol])) for rol, label in User.ROL_CHOICES],
            'permisos_grupos': permisos_agrupados(),
        })


class RolEditarView(AdminRequeridoMixin, View):
    def get(self, request, pk=None):
        rol = get_object_or_404(RolPersonalizado, pk=pk) if pk else None
        return render(request, 'users/rol_form.html', {'rol': rol, 'form': RolForm(instance=rol),
                                                       'grupos': permisos_agrupados(),
                                                       'seleccionados': set(rol.permisos) if rol else set()})

    def post(self, request, pk=None):
        rol = get_object_or_404(RolPersonalizado, pk=pk) if pk else None
        if request.POST.get('eliminar') and rol:
            if rol.usuarios.exists():
                messages.error(request, 'No se puede eliminar: hay usuarios con este rol.')
                return redirect('users:rol_editar', pk=rol.pk)
            rol.delete()
            messages.success(request, 'Rol eliminado.')
            return redirect('users:roles')
        form = RolForm(request.POST, instance=rol)
        if form.is_valid():
            rol = form.save(commit=False)
            rol.permisos = form.permisos_validos(request.POST.getlist('permisos'))
            rol.save()
            messages.success(request, f'Rol "{rol}" guardado.')
            return redirect('users:roles')
        return render(request, 'users/rol_form.html', {'rol': rol, 'form': form, 'grupos': permisos_agrupados(),
                                                       'seleccionados': set(request.POST.getlist('permisos'))})


# ── Notificaciones ──────────────────────────────────────────────────────────

class NotificacionesView(LoginRequiredMixin, View):
    def get(self, request):
        qs = request.user.notificaciones.all()
        if request.GET.get('json'):
            items = [{'id': n.pk, 'titulo': n.titulo, 'cuerpo': n.cuerpo[:120], 'url': n.url, 'leida': n.leida,
                      'icono': n.icono, 'hace': timezone.localtime(n.created_at).strftime('%d/%m %H:%M')}
                     for n in qs[:15]]
            return JsonResponse({'items': items, 'no_leidas': services.contar_no_leidas(request.user)})
        return render(request, 'users/notificaciones.html', {'page': paginar(request, qs, 50)})

    def post(self, request):
        pk = request.POST.get('id')
        qs = NotificacionInterna.objects.filter(destinatario=request.user, leida=False)
        if pk:
            qs = qs.filter(pk=pk)
        qs.update(leida=True)
        services.invalidar_contador(request.user)
        return JsonResponse({'ok': True})


class AuditoriaView(AdminRequeridoMixin, View):
    """Bitácora de acciones sensibles de este cliente (solo administradores)."""
    template_name = 'users/auditoria.html'

    def get(self, request):
        from django.core.paginator import Paginator
        qs = RegistroAuditoria.objects.select_related('usuario').all()
        accion = request.GET.get('accion', '')
        if accion:
            qs = qs.filter(accion=accion)
        q = request.GET.get('q', '').strip()
        if q:
            qs = qs.filter(models.Q(usuario_desc__icontains=q) | models.Q(objeto__icontains=q) |
                           models.Q(ip__icontains=q))
        pagina = Paginator(qs, 50).get_page(request.GET.get('page'))
        return render(request, self.template_name, {
            'pagina': pagina, 'accion': accion, 'q': q, 'acciones': RegistroAuditoria.ACCION_CHOICES})
