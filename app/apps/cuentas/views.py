import json

from django.contrib import messages
from django.db.models import Count, Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views import View

from core.permisos import PermisoRequeridoMixin
from core.tenancy import tiene_funcion
from .forms import (AvisoRecurrenteForm, CuentaForm, EstadoProyectoForm, ProyectoForm, RecordatorioForm,
                    TareaPostventaForm)
from .models import (AvisoRecurrente, Cuenta, EstadoProyecto, Proyecto, RecordatorioPersonal,
                     TareaPostventa)
from . import services


class PostventaMixin(PermisoRequeridoMixin):
    """Requiere el permiso 'postventa' y que el cliente tenga habilitada la función extra."""
    permiso = 'postventa'

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not tiene_funcion('postventa'):
            raise Http404('El módulo de Clientes / Postventa no está habilitado.')
        return super().dispatch(request, *args, **kwargs)


# ── Cuentas ──────────────────────────────────────────────────────────────────

class CuentaListView(PostventaMixin, View):
    def get(self, request):
        q = (request.GET.get('q') or '').strip()
        ver_baja = request.GET.get('baja') == '1'
        cuentas = Cuenta.objects.select_related('responsable').annotate(
            n_proyectos=Count('proyectos', distinct=True),
            n_tareas=Count('tareas', filter=Q(tareas__estado=TareaPostventa.ESTADO_PENDIENTE), distinct=True),
        )
        if not ver_baja:
            cuentas = cuentas.filter(activa=True)
        if q:
            cuentas = cuentas.filter(Q(nombre__icontains=q) | Q(email__icontains=q) | Q(telefono__icontains=q))
        return render(request, 'cuentas/lista.html', {'cuentas': cuentas, 'q': q, 'ver_baja': ver_baja})


def _contexto_detalle(cuenta, **extra):
    tareas = cuenta.tareas.select_related('asignado_a', 'proyecto')
    ctx = {
        'cuenta': cuenta,
        'proyectos': cuenta.proyectos.select_related('estado', 'responsable'),
        'tareas_pend': tareas.filter(estado=TareaPostventa.ESTADO_PENDIENTE),
        'tareas_hechas': tareas.exclude(estado=TareaPostventa.ESTADO_PENDIENTE)[:20],
        'contactos': cuenta.contactos.all(),
        'form_tarea': TareaPostventaForm(cuenta=cuenta),
        'avisos': cuenta.avisos.select_related('contacto', 'linea', 'plantilla'),
        'form_aviso': AvisoRecurrenteForm(cuenta=cuenta),
    }
    ctx.update(extra)
    return ctx


class CuentaDetailView(PostventaMixin, View):
    def get(self, request, pk):
        cuenta = get_object_or_404(Cuenta.objects.select_related('responsable'), pk=pk)
        return render(request, 'cuentas/detalle.html', _contexto_detalle(cuenta))


class CuentaContactoView(PostventaMixin, View):
    """Asociar o quitar contactos de una cuenta (con buscador)."""
    def post(self, request, pk):
        cuenta = get_object_or_404(Cuenta, pk=pk)
        accion = request.POST.get('accion')
        from apps.crm.models import Contacto
        if accion == 'agregar' and request.POST.get('contacto'):
            contacto = get_object_or_404(Contacto, pk=request.POST['contacto'])
            cuenta.contactos.add(contacto)
            messages.success(request, f'{contacto.nombre} asociado a la cuenta.')
            return redirect('cuentas:detalle', pk=cuenta.pk)
        if accion == 'quitar' and request.POST.get('contacto'):
            cuenta.contactos.remove(request.POST['contacto'])
            messages.success(request, 'Contacto quitado de la cuenta.')
            return redirect('cuentas:detalle', pk=cuenta.pk)
        # Buscar: muestra candidatos para elegir (no redirige, re-renderiza la ficha).
        q = (request.POST.get('q') or '').strip()
        candidatos = []
        if q:
            candidatos = list(Contacto.objects.filter(
                Q(nombre__icontains=q) | Q(telefono__icontains=q) | Q(email__icontains=q)
            ).exclude(cuentas=cuenta)[:15])
        return render(request, 'cuentas/detalle.html',
                      _contexto_detalle(cuenta, candidatos=candidatos, q_contacto=q, buscado=True))


class CuentaEditarView(PostventaMixin, View):
    def get(self, request, pk=None):
        cuenta = get_object_or_404(Cuenta, pk=pk) if pk else None
        return render(request, 'cuentas/cuenta_form.html', {'cuenta': cuenta, 'form': CuentaForm(instance=cuenta)})

    def post(self, request, pk=None):
        cuenta = get_object_or_404(Cuenta, pk=pk) if pk else None
        form = CuentaForm(request.POST, instance=cuenta)
        if not form.is_valid():
            return render(request, 'cuentas/cuenta_form.html', {'cuenta': cuenta, 'form': form})
        cuenta = form.save()
        messages.success(request, 'Cuenta guardada.')
        return redirect('cuentas:detalle', pk=cuenta.pk)


# ── Proyectos (kanban) ─────────────────────────────────────────────────────────

class TableroView(PostventaMixin, View):
    def get(self, request):
        estados = services.estados_proyecto()
        q = (request.GET.get('q') or '').strip()
        proyectos = Proyecto.objects.select_related('cuenta', 'estado', 'responsable')
        if q:
            proyectos = proyectos.filter(Q(nombre__icontains=q) | Q(cuenta__nombre__icontains=q))
        por_estado = {e.pk: [] for e in estados}
        sin_estado = []
        for p in proyectos:
            if p.estado_id in por_estado:
                por_estado[p.estado_id].append(p)
            else:
                sin_estado.append(p)
        columnas = [{'estado': e, 'proyectos': por_estado[e.pk]} for e in estados]
        return render(request, 'cuentas/tablero.html', {
            'columnas': columnas, 'sin_estado': sin_estado, 'q': q, 'estados': estados,
        })


class ProyectoEditarView(PostventaMixin, View):
    def _cuenta_de(self, request, proyecto):
        if proyecto is not None:
            return proyecto.cuenta
        cuenta_id = request.GET.get('cuenta') or request.POST.get('cuenta')
        return get_object_or_404(Cuenta, pk=cuenta_id) if cuenta_id else None

    def get(self, request, pk=None):
        proyecto = get_object_or_404(Proyecto, pk=pk) if pk else None
        cuenta = self._cuenta_de(request, proyecto)
        if cuenta is None:
            raise Http404('Falta la cuenta del proyecto.')
        return render(request, 'cuentas/proyecto_form.html',
                      {'proyecto': proyecto, 'cuenta': cuenta, 'form': ProyectoForm(instance=proyecto)})

    def post(self, request, pk=None):
        proyecto = get_object_or_404(Proyecto, pk=pk) if pk else None
        cuenta = self._cuenta_de(request, proyecto)
        if cuenta is None:
            raise Http404('Falta la cuenta del proyecto.')
        form = ProyectoForm(request.POST, instance=proyecto)
        if not form.is_valid():
            return render(request, 'cuentas/proyecto_form.html',
                          {'proyecto': proyecto, 'cuenta': cuenta, 'form': form})
        proyecto = form.save(commit=False)
        proyecto.cuenta = cuenta
        if proyecto.estado_id is None:
            proyecto.estado = EstadoProyecto.objects.first()
        proyecto.save()
        messages.success(request, 'Proyecto guardado.')
        return redirect('cuentas:detalle', pk=cuenta.pk)


class ProyectoMoverView(PostventaMixin, View):
    """Endpoint AJAX del kanban: cambia el estado (columna) de un proyecto."""
    def post(self, request, pk):
        proyecto = get_object_or_404(Proyecto, pk=pk)
        try:
            datos = json.loads(request.body or '{}')
        except ValueError:
            datos = {}
        estado = get_object_or_404(EstadoProyecto, pk=datos.get('estado'))
        services.mover_proyecto(proyecto, estado)
        return JsonResponse({'ok': True})


# ── Configuración de estados (columnas del kanban) ─────────────────────────────

class EstadosView(PostventaMixin, View):
    permiso = 'embudos'  # configurar las columnas es estructura: mismo permiso que los embudos

    def get(self, request):
        services.estados_proyecto()
        return render(request, 'cuentas/estados.html',
                      {'estados': EstadoProyecto.objects.annotate(n=Count('proyectos'))})

    def post(self, request):
        accion = request.POST.get('accion')
        if accion == 'nuevo':
            form = EstadoProyectoForm(request.POST)
            if form.is_valid():
                estado = form.save(commit=False)
                estado.orden = (EstadoProyecto.objects.count())
                estado.save()
                messages.success(request, 'Estado agregado.')
            else:
                messages.error(request, 'Revisá los datos del estado.')
        elif accion == 'borrar':
            estado = get_object_or_404(EstadoProyecto, pk=request.POST.get('pk'))
            estado.proyectos.update(estado=None)
            estado.delete()
            messages.success(request, 'Estado eliminado. Los proyectos quedaron sin estado.')
        elif accion == 'orden':
            for i, pk in enumerate(request.POST.getlist('orden[]')):
                EstadoProyecto.objects.filter(pk=pk).update(orden=i)
            return JsonResponse({'ok': True})
        return redirect('cuentas:estados')


# ── Tareas de postventa ────────────────────────────────────────────────────────

class TareaNuevaView(PostventaMixin, View):
    def post(self, request):
        cuenta = get_object_or_404(Cuenta, pk=request.POST.get('cuenta'))
        form = TareaPostventaForm(request.POST, cuenta=cuenta)
        if form.is_valid():
            tarea = form.save(commit=False)
            tarea.cuenta = cuenta
            tarea.creada_por = request.user
            if tarea.asignado_a_id is None:
                tarea.asignado_a = request.user
            tarea.save()
            messages.success(request, 'Tarea creada.')
        else:
            messages.error(request, 'No se pudo crear la tarea: revisá los datos.')
        return redirect('cuentas:detalle', pk=cuenta.pk)


class TareaCompletarView(PostventaMixin, View):
    def post(self, request, pk):
        tarea = get_object_or_404(TareaPostventa, pk=pk)
        if tarea.estado == TareaPostventa.ESTADO_PENDIENTE:
            tarea.estado = TareaPostventa.ESTADO_COMPLETADA
            tarea.completada_at = timezone.now()
            tarea.save(update_fields=['estado', 'completada_at'])
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'ok': True})
        return redirect('cuentas:detalle', pk=tarea.cuenta_id)


# ── Avisos recurrentes de WhatsApp ─────────────────────────────────────────────

class AvisoNuevaView(PostventaMixin, View):
    def post(self, request):
        cuenta = get_object_or_404(Cuenta, pk=request.POST.get('cuenta'))
        form = AvisoRecurrenteForm(request.POST, cuenta=cuenta)
        if form.is_valid():
            form.save()
            messages.success(request, 'Aviso programado.')
        else:
            messages.error(request, 'No se pudo programar el aviso: ' + ' '.join(
                f'{campo}: {", ".join(errs)}' for campo, errs in form.errors.items()))
        return redirect('cuentas:detalle', pk=cuenta.pk)


class AvisoAccionView(PostventaMixin, View):
    """Pausar/reactivar o borrar un aviso."""
    def post(self, request, pk):
        aviso = get_object_or_404(AvisoRecurrente, pk=pk)
        cuenta_id = aviso.cuenta_id
        accion = request.POST.get('accion')
        if accion == 'borrar':
            aviso.delete()
            messages.success(request, 'Aviso eliminado.')
        elif accion == 'toggle':
            aviso.activo = not aviso.activo
            aviso.save(update_fields=['activo'])
        return redirect('cuentas:detalle', pk=cuenta_id)


# ── Recordatorios personales (el CRM te avisa a vos) ───────────────────────────

class RecordatorioListView(PostventaMixin, View):
    """Mis recordatorios: el CRM me avisa a mí antes de una fecha (email / WhatsApp)."""
    def get(self, request):
        recs = RecordatorioPersonal.objects.filter(usuario=request.user).select_related(
            'linea', 'cuenta', 'proyecto')
        return render(request, 'cuentas/recordatorios.html', {
            'form': RecordatorioForm(),
            'pendientes': recs.filter(estado=RecordatorioPersonal.ESTADO_PENDIENTE),
            'historial': recs.exclude(estado=RecordatorioPersonal.ESTADO_PENDIENTE)[:30],
        })

    def post(self, request):
        form = RecordatorioForm(request.POST)
        proyecto = None
        if request.POST.get('proyecto'):
            proyecto = get_object_or_404(Proyecto.objects.select_related('cuenta'),
                                         pk=request.POST['proyecto'])
        if not form.is_valid():
            if proyecto is not None:
                messages.error(request, 'No se pudo crear el recordatorio: revisá la fecha.')
                return redirect('cuentas:proyecto_editar', pk=proyecto.pk)
            recs = RecordatorioPersonal.objects.filter(usuario=request.user).select_related(
                'linea', 'cuenta', 'proyecto')
            return render(request, 'cuentas/recordatorios.html', {
                'form': form,
                'pendientes': recs.filter(estado=RecordatorioPersonal.ESTADO_PENDIENTE),
                'historial': recs.exclude(estado=RecordatorioPersonal.ESTADO_PENDIENTE)[:30],
            })
        rec = form.save(commit=False)
        rec.usuario = request.user
        if proyecto is not None:
            rec.proyecto = proyecto
            rec.cuenta = proyecto.cuenta
        rec.save()
        messages.success(request, f'Listo: te vamos a avisar el {rec.enviar_el:%d/%m/%Y}.')
        if proyecto is not None:
            return redirect('cuentas:proyecto_editar', pk=proyecto.pk)
        return redirect('cuentas:recordatorios')


class RecordatorioAccionView(PostventaMixin, View):
    """Cancelar, reactivar o borrar uno de mis recordatorios."""
    def post(self, request, pk):
        rec = get_object_or_404(RecordatorioPersonal, pk=pk, usuario=request.user)
        accion = request.POST.get('accion')
        if accion == 'borrar':
            rec.delete()
            messages.success(request, 'Recordatorio eliminado.')
        elif accion == 'cancelar':
            rec.estado = RecordatorioPersonal.ESTADO_CANCELADO
            rec.save(update_fields=['estado'])
        elif accion == 'reactivar':
            rec.estado = RecordatorioPersonal.ESTADO_PENDIENTE
            rec.save(update_fields=['estado'])
        destino = request.POST.get('next')
        if destino == 'proyecto' and rec.proyecto_id:
            return redirect('cuentas:proyecto_editar', pk=rec.proyecto_id)
        return redirect('cuentas:recordatorios')
