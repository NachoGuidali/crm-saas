/* CRM Ventas — JS común (sin frameworks). Expone window.CRM */
(function () {
  'use strict';

  function cookie(nombre) {
    const m = document.cookie.match('(^|;)\\s*' + nombre + '\\s*=\\s*([^;]+)');
    return m ? decodeURIComponent(m.pop()) : '';
  }
  const csrf = () => cookie('csrftoken') || (document.querySelector('[name=csrfmiddlewaretoken]') || {}).value || '';
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));

  function toast(msg, tipo) {
    let cont = document.getElementById('toasts');
    if (!cont) { cont = document.createElement('div'); cont.id = 'toasts'; document.body.appendChild(cont); }
    const el = document.createElement('div');
    el.className = 'toast-msg ' + (tipo || 'ok');
    el.textContent = msg;
    cont.appendChild(el);
    setTimeout(() => { el.style.opacity = '0'; el.style.transition = 'opacity .3s'; }, tipo === 'error' ? 6000 : 3500);
    setTimeout(() => el.remove(), tipo === 'error' ? 6400 : 3900);
  }

  /** POST/PUT/GET JSON. data puede ser objeto (se manda JSON) o FormData. */
  async function api(url, opciones) {
    opciones = opciones || {};
    const metodo = opciones.method || (opciones.data ? 'POST' : 'GET');
    const headers = {'X-CSRFToken': csrf(), 'X-Requested-With': 'XMLHttpRequest'};
    let body;
    if (opciones.data instanceof FormData) body = opciones.data;
    else if (opciones.data) { body = JSON.stringify(opciones.data); headers['Content-Type'] = 'application/json'; }
    let resp;
    try {
      resp = await fetch(url, {method: metodo, headers, body, credentials: 'same-origin'});
    } catch (e) {
      if (!opciones.silencioso) toast('Sin conexión con el servidor', 'error');
      throw e;
    }
    let json = {};
    try { json = await resp.json(); } catch (e) { json = {ok: resp.ok}; }
    if (!resp.ok || json.ok === false) {
      if (!opciones.silencioso) toast(json.error || ('Error ' + resp.status), 'error');
      const err = new Error(json.error || resp.status); err.data = json; throw err;
    }
    return json;
  }

  function hace(seg) {
    seg = Math.max(0, seg | 0);
    const m = Math.floor(seg / 60), s = seg % 60, h = Math.floor(m / 60);
    return h ? `${h}:${String(m % 60).padStart(2, '0')}:${String(s).padStart(2, '0')}` : `${m}:${String(s).padStart(2, '0')}`;
  }

  /* ── Sidebar móvil ───────────────────────────────────────────────── */
  function initSidebar() {
    const t = document.getElementById('sidebarToggle'), sb = document.getElementById('sidebar'), ov = document.getElementById('sidebarOverlay');
    if (!t) return;
    t.addEventListener('click', () => { sb.classList.toggle('show'); ov.classList.toggle('show'); });
    ov.addEventListener('click', () => { sb.classList.remove('show'); ov.classList.remove('show'); });
  }

  /* ── Búsqueda global (Ctrl+K) ────────────────────────────────────── */
  function initBuscador() {
    const input = document.getElementById('buscadorGlobal');
    if (!input) return;
    const res = document.getElementById('buscadorRes');
    let timer, sel = -1;
    const pintar = items => {
      sel = -1;
      if (!items.length) { res.innerHTML = '<div class="p-3 small muted">Sin resultados</div>'; res.style.display = 'block'; return; }
      res.innerHTML = items.map(i => `<a href="${esc(i.url)}"><b>${esc(i.nombre)}</b> <span class="muted">${esc(i.telefono || i.email)}</span><small>${esc(i.detalle)}</small></a>`).join('');
      res.style.display = 'block';
    };
    input.addEventListener('input', () => {
      clearTimeout(timer);
      const q = input.value.trim();
      if (q.length < 2) { res.style.display = 'none'; return; }
      timer = setTimeout(() => api('/contactos/buscar/?q=' + encodeURIComponent(q), {silencioso: true}).then(d => pintar(d.resultados)).catch(() => {}), 220);
    });
    input.addEventListener('keydown', e => {
      const links = res.querySelectorAll('a');
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        sel = Math.max(0, Math.min(links.length - 1, sel + (e.key === 'ArrowDown' ? 1 : -1)));
        links.forEach((l, i) => l.classList.toggle('sel', i === sel));
      } else if (e.key === 'Enter' && links.length) {
        e.preventDefault(); window.location = (links[sel] || links[0]).href;
      } else if (e.key === 'Escape') { res.style.display = 'none'; input.blur(); }
    });
    document.addEventListener('click', e => { if (!e.target.closest('.buscador')) res.style.display = 'none'; });
    document.addEventListener('keydown', e => { if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); input.focus(); input.select(); } });
  }

  /* ── Notificaciones ──────────────────────────────────────────────── */
  function initNotificaciones() {
    const btn = document.getElementById('notifBtn');
    if (!btn) return;
    btn.addEventListener('show.bs.dropdown', () => {
      api('/usuarios/notificaciones/?json=1', {silencioso: true}).then(d => {
        const lista = document.getElementById('notifLista');
        if (!d.items.length) { lista.innerHTML = '<div class="p-3 small muted text-center">Sin notificaciones</div>'; return; }
        lista.innerHTML = d.items.map(n => `<a class="dropdown-item py-2 ${n.leida ? 'opacity-50' : ''}" href="${esc(n.url || '#')}" data-notif="${n.id}" style="white-space:normal">
          <div class="d-flex gap-2"><i class="bi bi-${esc(n.icono)} mt-1"></i><div><div class="small fw-semibold">${esc(n.titulo)}</div>
          ${n.cuerpo ? `<div class="small muted">${esc(n.cuerpo)}</div>` : ''}<div class="muted" style="font-size:10.5px">${esc(n.hace)}</div></div></div></a>`).join('');
        lista.querySelectorAll('[data-notif]').forEach(a => a.addEventListener('click', () => {
          const fd = new FormData(); fd.append('id', a.dataset.notif);
          navigator.sendBeacon ? fetch('/usuarios/notificaciones/', {method: 'POST', body: fd, headers: {'X-CSRFToken': csrf()}, keepalive: true}) : null;
        }));
      }).catch(() => {});
    });
    const todas = document.getElementById('notifLeerTodas');
    if (todas) todas.addEventListener('click', e => {
      e.preventDefault(); e.stopPropagation();
      api('/usuarios/notificaciones/', {data: new FormData()}).then(() => { setBadge('badgeNotif', 0); toast('Notificaciones marcadas como leídas'); });
    });
  }

  function setBadge(id, n) {
    document.querySelectorAll('[data-badge="' + id + '"]').forEach(el => {
      el.textContent = n > 99 ? '99+' : n;
      el.style.display = n > 0 ? '' : 'none';
    });
  }

  /* ── Pulso: contadores + estado de llamada (un solo request liviano) ── */
  let pulsoTimer = null, llamadaActual = null, ultimaLlamadaId = null, relojTimer = null, prevNotif = null;
  // Id de esta pestaña (se mantiene al navegar dentro del CRM): para saber si cerró la última pestaña abierta.
  const tabId = (() => {
    try {
      let t = sessionStorage.getItem('crm_tab');
      if (!t) { t = Math.random().toString(36).slice(2, 12); sessionStorage.setItem('crm_tab', t); }
      return t;
    } catch (e) { return 'x' + Math.random().toString(36).slice(2, 10); }
  })();
  // Al cerrar la pestaña (o pasar a otra página) se avisa: si no vuelve en unos segundos, queda desconectada.
  window.addEventListener('pagehide', () => {
    if (document.body.dataset.auth !== '1' || !navigator.sendBeacon) return;
    const fd = new FormData(); fd.append('tab', tabId); fd.append('csrfmiddlewaretoken', csrf());
    navigator.sendBeacon('/pulso/salir/', fd);
  });
  // Volvió a la página desde el historial (atrás/adelante): avisar enseguida que sigue acá.
  window.addEventListener('pageshow', e => { if (e.persisted && document.body.dataset.auth === '1') pulso(); });
  // Último uso del CRM (mouse, teclado, clics), compartido entre pestañas: para el estado "Ausente" en Supervisión.
  let usoLocal = Date.now();
  const marcarUso = () => {
    const ahora = Date.now();
    if (ahora - usoLocal < 5000) return;
    usoLocal = ahora;
    try { localStorage.setItem('crm_uso', String(ahora)); } catch (e) {}
  };
  ['mousemove', 'keydown', 'click', 'scroll', 'touchstart', 'focus'].forEach(ev => window.addEventListener(ev, marcarUso, {passive: true}));
  const segundosSinUso = () => {
    let ultimo = usoLocal;
    try { ultimo = Math.max(ultimo, parseInt(localStorage.getItem('crm_uso') || '0')); } catch (e) {}
    return Math.max(0, Math.round((Date.now() - ultimo) / 1000));
  };
  function pulso() {
    api('/pulso/?tab=' + encodeURIComponent(tabId) + '&sin_uso=' + segundosSinUso(), {silencioso: true}).then(d => {
      setBadge('badgeNotif', d.notif); setBadge('badgeTareas', d.tareas); setBadge('badgeWa', d.wa);
      if (prevNotif !== null && d.notif > prevNotif) sonido();
      prevNotif = d.notif;
      pintarLlamada(d.llamada);
      pedirResultado(d.calificar);
    }).catch(() => {}).finally(() => {
      clearTimeout(pulsoTimer);
      pulsoTimer = setTimeout(pulso, intervaloPulso());
    });
  }

  // Con llamada en curso: 3 s. Usuarios con interno de Anura: 4 s (para que la ficha aparezca apenas suena
  // una entrante). Resto: 15 s. El endpoint responde en ~3 ms, así que el costo es mínimo.
  function intervaloPulso() {
    if (llamadaActual && llamadaActual.viva) return 3000;
    return document.body.dataset.interno === '1' ? 4000 : 15000;
  }

  function sonido() {
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.connect(g); g.connect(ctx.destination); o.frequency.value = 880; g.gain.value = 0.04;
      o.start(); o.stop(ctx.currentTime + 0.12);
    } catch (e) { /* sin audio */ }
  }

  /* ── Resultado de la gestión: obligatorio después de cada llamada atendida ── */
  let resultadoPendiente = null;
  function pedirResultado(p) {
    const m = document.getElementById('modalResultado');
    if (!m || !window.bootstrap) return;
    const modal = bootstrap.Modal.getOrCreateInstance(m);
    if (!p) { if (resultadoPendiente) { resultadoPendiente = null; modal.hide(); } return; }
    if (resultadoPendiente && resultadoPendiente.id === p.id) return;
    resultadoPendiente = p;
    m.querySelector('.quien').textContent = `${p.nombre} · ${p.cuando}`;
    const ver = m.querySelector('.ver-ficha');
    ver.href = p.url || '#'; ver.style.display = p.url && location.pathname !== p.url ? '' : 'none';
    const box = m.querySelector('.opciones');
    box.innerHTML = '';
    p.resultados.forEach(r => {
      const l = document.createElement('label');
      l.className = 'd-flex gap-2 align-items-center border rounded px-2 py-1';
      l.innerHTML = `<input type="radio" name="resultado" class="form-check-input mt-0" value="${r.id}"> <span></span>`;
      l.querySelector('span').textContent = r.nombre;
      l.querySelector('input').dataset.fecha = r.pide_fecha ? '1' : '';
      box.appendChild(l);
    });
    m.querySelector('[name=nota]').value = '';
    m.querySelector('[name=fecha]').value = '';
    m.querySelector('.fecha-wrap').style.display = 'none';
    m.querySelector('.guardar').disabled = true;
    modal.show();
  }

  function initResultado() {
    const m = document.getElementById('modalResultado');
    if (!m) return;
    m.addEventListener('change', e => {
      if (e.target.name !== 'resultado') return;
      m.querySelector('.fecha-wrap').style.display = e.target.dataset.fecha ? '' : 'none';
      m.querySelector('.guardar').disabled = false;
    });
    m.querySelector('.guardar').addEventListener('click', async ev => {
      const r = m.querySelector('[name=resultado]:checked');
      if (!r || !resultadoPendiente) return;
      const fecha = m.querySelector('[name=fecha]').value;
      if (r.dataset.fecha && !fecha) { toast('Indicá cuándo volver a llamar', 'error'); return; }
      ev.target.disabled = true;
      try {
        const d = await api('/api/telephony/calificar/' + resultadoPendiente.id,
          {data: {resultado: r.value, nota: m.querySelector('[name=nota]').value, volver_a_llamar: fecha}});
        toast('Resultado guardado');
        resultadoPendiente = null;
        bootstrap.Modal.getOrCreateInstance(m).hide();
        document.dispatchEvent(new CustomEvent('crm:llamada-calificada'));
        if (d.siguiente) setTimeout(() => pedirResultado(d.siguiente), 400);
      } catch (e) { ev.target.disabled = false; }
    });
  }

  /* ── Adjunto de WhatsApp → campo de tipo archivo del contacto ── */
  function initGuardarAdjunto() {
    document.addEventListener('click', async e => {
      const b = e.target.closest('.guardar-adjunto');
      if (!b) return;
      e.preventDefault();
      const url = `/whatsapp/mensaje/${b.dataset.msg}/guardar-adjunto/`;
      let d;
      try { d = await api(url); } catch (err) { return; }
      if (!d.campos.length) { toast('Primero creá un campo de tipo "Archivo" en Configuración → Campos personalizados', 'error'); return; }
      let campo = d.campos[0];
      if (d.campos.length > 1) {
        const n = prompt('¿En qué campo lo guardo?\n' + d.campos.map((c, i) => `${i + 1}. ${c.nombre}`).join('\n'), '1');
        if (n === null) return;
        campo = d.campos[parseInt(n, 10) - 1];
        if (!campo) { toast('Opción inválida', 'error'); return; }
      } else if (!confirm(`¿Guardar el adjunto en «${campo.nombre}»?`)) return;
      try {
        const r = await api(url, {data: {campo: campo.slug}});
        toast(r.mensaje);
        b.innerHTML = '<i class="bi bi-check2"></i>';
      } catch (err) { /* toast ya mostrado */ }
    });
  }

  /* ── Widget de llamada (vive en base.html: sobrevive a la navegación) ── */
  function pintarLlamada(ll) {
    const w = document.getElementById('callWidget');
    if (!w) return;
    const telMarco = telefono();
    if (telMarco && telMarco.reflejar) telMarco.reflejar(ll);
    if (!ll) {
      if (llamadaActual && llamadaActual.viva) { // terminó
        llamadaActual.viva = false;
        mostrarFin(llamadaActual);
      }
      return;
    }
    if (ll.viva && ll.direccion === 'IN' && ll.id !== ultimaLlamadaId) sonido();
    // Discador: cada llamada nueva de la campaña abre la ficha del lead
    if (ll.viva && ll.campania && ll.url && ll.id !== ultimaLlamadaId && location.pathname !== ll.url.split('?')[0]) {
      ultimaLlamadaId = ll.id;
      location.href = ll.url;
      return;
    }
    llamadaActual = Object.assign({}, ll, {recibida: Date.now()});
    ultimaLlamadaId = ll.id;
    w.className = 'show ' + (ll.viva ? ll.estado : 'fin');
    w.querySelector('.quien').innerHTML = ll.url ? `<a href="${esc(ll.url)}">${esc(ll.nombre)}</a>` : esc(ll.nombre);
    const tel = telefono(), puedeCortar = ll.puede_cortar || !!tel;
    w.querySelector('.cortar').style.display = ll.viva && puedeCortar ? '' : 'none';
    w.querySelector('.hint').style.display = ll.viva && !puedeCortar ? '' : 'none';
    // Si Anura no avisó nada a los 20 s, se ofrece descartar el aviso (se cierra solo a los 3 min)
    w.querySelector('.descartar').style.display = ll.viva && ll.sin_aviso && !puedeCortar && ll.segundos >= 20 ? '' : 'none';
    const ir = w.querySelector('.ir-anura');
    ir.style.display = ll.viva && ll.anura_url && !tel ? '' : 'none';
    ir.dataset.url = ll.anura_url || '';
    if (!ll.viva) { mostrarFin(ll); return; }
    clearInterval(relojTimer);
    const tick = () => {
      const seg = llamadaActual.segundos + Math.floor((Date.now() - llamadaActual.recibida) / 1000);
      w.querySelector('.est').textContent = (ll.atendida ? 'En curso · ' + hace(seg) : ll.estado_display + '…') + (ll.campania ? ' · ' + ll.campania : '');
    };
    tick(); relojTimer = setInterval(tick, 1000);
  }

  function mostrarFin(ll) {
    const w = document.getElementById('callWidget');
    clearInterval(relojTimer);
    w.className = 'show fin';
    w.querySelector('.cortar').style.display = 'none';
    w.querySelector('.hint').style.display = 'none';
    w.querySelector('.ir-anura').style.display = 'none';
    api('/api/telephony/estado/?ultima=' + (ll.id || ultimaLlamadaId), {silencioso: true}).then(d => {
      const f = d.llamada || ll;
      w.querySelector('.est').textContent = 'Finalizada · ' + (f.estado_display || '') + (f.segundos ? ' · ' + hace(f.segundos) : '');
    }).catch(() => {});
    document.dispatchEvent(new CustomEvent('crm:llamada-fin', {detail: ll}));
    clearTimeout(pulsoTimer); pulsoTimer = setTimeout(pulso, 1500);  // trae el pedido de resultado enseguida
    setTimeout(() => { if (!llamadaActual || !llamadaActual.viva) w.className = ''; }, 8000);
  }

  /** Teléfono del CRM (marco fijo), si esta pantalla está dentro de él. */
  function telefono() {
    try { return window.top !== window && window.top.CRMTelefono ? window.top.CRMTelefono : null; } catch (e) { return null; }
  }

  async function llamar(opciones) {
    const tel = telefono();
    if (tel) tel.esperarSaliente();  // la terminal del CRM atiende sola cuando Anura la llame
    try {
      const d = await api('/api/telephony/dial', {data: {oportunidadId: opciones.op || null, contactoId: opciones.contacto || null,
                                                          phoneNumber: opciones.tel || ''}});
      toast(tel ? 'Llamando…' : 'Llamando… atendé tu teléfono de Anura y se disca al cliente');
      pintarLlamada(d.llamada);
      clearTimeout(pulsoTimer); pulsoTimer = setTimeout(pulso, 2000);
    } catch (e) { /* toast ya mostrado */ }
  }

  function initWidget() {
    const w = document.getElementById('callWidget');
    if (!w) return;
    w.querySelector('.cortar').addEventListener('click', async () => {
      if (!llamadaActual) return;
      const tel = telefono();
      if (tel && tel.cortar()) { toast('Cortando…'); }
      else { try { await api('/api/telephony/hangup/' + llamadaActual.id, {method: 'PUT'}); toast('Cortando…'); } catch (e) {} }
      clearTimeout(pulsoTimer); pulsoTimer = setTimeout(pulso, 1500);
    });
    w.querySelector('.cerrar').addEventListener('click', () => { w.className = ''; });
    w.querySelector('.descartar').addEventListener('click', async () => {
      if (!llamadaActual || !confirm('Anura no avisó nada de esta llamada. ¿Cerrar el aviso? (queda registrada como fallida)')) return;
      try { await api('/api/telephony/hangup/' + llamadaActual.id + '?descartar=1', {method: 'PUT'}); toast('Aviso descartado'); } catch (e) {}
      clearTimeout(pulsoTimer); pulsoTimer = setTimeout(pulso, 500);
    });
    // Abre Anura en una pestaña con nombre fijo: si ya se abrió desde el CRM, vuelve a esa misma pestaña.
    w.querySelector('.ir-anura').addEventListener('click', e => {
      const url = e.currentTarget.dataset.url;
      if (!url) return;
      const ventana = window.open('', 'anura_web');
      if (!ventana) { toast('El navegador bloqueó la ventana: permití las ventanas emergentes para este sitio', 'warn'); return; }
      let vacia = false;
      try { vacia = ventana.location.href === 'about:blank'; } catch (err) { vacia = false; }  // otro dominio = ya está Anura
      if (vacia) ventana.location.href = url;
      ventana.focus();
    });
    document.addEventListener('click', e => {
      const b = e.target.closest('[data-llamar]');
      if (!b) return;
      e.preventDefault(); e.stopPropagation();
      llamar({op: b.dataset.op, contacto: b.dataset.contacto, tel: b.dataset.tel});
    });
  }

  /* ── Acciones AJAX genéricas: <button data-accion="/oportunidades/5/pausar/" data-confirm="…"> ── */
  function initAcciones() {
    document.addEventListener('click', async e => {
      const b = e.target.closest('[data-accion]');
      if (!b) return;
      e.preventDefault();
      if (b.dataset.confirm && !confirm(b.dataset.confirm)) return;
      let data = {};
      try { data = b.dataset.payload ? JSON.parse(b.dataset.payload) : {}; } catch (err) {}
      b.disabled = true;
      try {
        const r = await api(b.dataset.accion, {data});
        if (r.mensaje) toast(r.mensaje);
        if (r.sugerencia) toast(r.sugerencia, 'warn');
        if (r.recargar || b.dataset.recargar) setTimeout(() => location.reload(), 350);
        document.dispatchEvent(new CustomEvent('crm:accion-ok', {detail: {boton: b, respuesta: r}}));
      } catch (err) { /* toast */ } finally { b.disabled = false; }
    });
    // Formularios AJAX: <form data-ajax> → envía por fetch, sin recargar
    document.addEventListener('submit', async e => {
      const f = e.target.closest('form[data-ajax]');
      if (!f) return;
      e.preventDefault();
      const btn = f.querySelector('[type=submit]');
      if (btn) btn.disabled = true;
      try {
        const r = await api(f.action, {data: new FormData(f)});
        if (r.mensaje) toast(r.mensaje);
        f.dispatchEvent(new CustomEvent('crm:ok', {detail: r}));
        if (r.recargar || f.dataset.recargar) setTimeout(() => location.reload(), 350);
        else if (f.dataset.reset !== undefined) f.reset();
      } catch (err) { /* toast */ } finally { if (btn) btn.disabled = false; }
    });
    // Copiar al portapapeles
    document.addEventListener('click', e => {
      const c = e.target.closest('.copiable');
      if (!c) return;
      navigator.clipboard.writeText(c.dataset.valor || c.textContent.trim()).then(() => toast('Copiado'));
    });
  }

  /* ── Mover de etapa (con datos obligatorios por etapa) ─────────────── */
  function inputPara(f) {
    const n = `name="${esc(f.clave)}"`, cls = 'class="form-control" required';
    if (f.tipo === 'lista') return `<select ${n} class="form-select" required><option value="">—</option>${f.opciones.map(o => `<option>${esc(o)}</option>`).join('')}</select>`;
    if (f.tipo === 'sino') return `<select ${n} class="form-select" required><option value="">—</option><option value="true">Sí</option><option value="false">No</option></select>`;
    if (f.tipo === 'fecha') return `<input type="date" ${n} ${cls}>`;
    if (f.tipo === 'numero') return `<input type="number" step="any" ${n} ${cls}>`;
    if (f.tipo === 'email') return `<input type="email" ${n} ${cls}>`;
    if (f.tipo === 'texto_largo') return `<textarea rows="2" ${n} ${cls}></textarea>`;
    if (f.tipo === 'archivo') return `<input type="file" ${n} ${cls}>`;
    return `<input type="text" ${n} ${cls}>`;
  }

  /** Pide los datos faltantes en una ventanita. Resuelve cuando se guardaron; rechaza si se cancela. */
  function pedirCampos(op, info) {
    return new Promise((resolve, reject) => {
      const el = document.getElementById('modalFaltan');
      el.querySelector('.etapa').textContent = info.etapa_nombre || '';
      const form = el.querySelector('form');
      form.querySelector('.campos').innerHTML = info.faltan.map(f =>
        `<div class="mb-2"><label class="form-label">${esc(f.nombre)} *</label>${inputPara(f)}</div>`).join('');
      const modal = bootstrap.Modal.getOrCreateInstance(el);
      let listo = false;
      form.onsubmit = async ev => {
        ev.preventDefault();
        const btn = form.querySelector('[type=submit]'); btn.disabled = true;
        try { await api(`/oportunidades/${op}/completar/`, {data: new FormData(form)}); listo = true; modal.hide(); resolve(); }
        catch (e) { /* toast */ } finally { btn.disabled = false; }
      };
      el.addEventListener('hidden.bs.modal', () => { if (!listo) reject(new Error('cancelado')); }, {once: true});
      modal.show();
      setTimeout(() => { const i = form.querySelector('input,select,textarea'); if (i) i.focus(); }, 300);
    });
  }

  async function mover(op, data) {
    try {
      return await api(`/oportunidades/${op}/mover/`, {data, silencioso: true});
    } catch (e) {
      if (e.data && e.data.faltan && e.data.faltan.length) {
        await pedirCampos(op, e.data);          // si cancela, rechaza y no se mueve
        return mover(op, data);
      }
      toast(e.message, 'error');
      throw e;
    }
  }

  /* ── Modal de cierre: Venta / No venta con tipificación obligatoria ── */
  function tipificaciones() {
    const el = document.getElementById('tipificaciones-data');
    return el ? JSON.parse(el.textContent) : [];
  }

  function abrirCierre(opts) {
    // opts: {op, etapa, tipo: 'ganado'|'perdido', nombre, onOk, onCancel}
    const modalEl = document.getElementById('modalCierre');
    if (!modalEl) return;
    const venta = opts.tipo === 'ganado';
    const lista = tipificaciones().filter(t => t.resultado === (venta ? 'venta' : 'no_venta'));
    modalEl.querySelector('.modal-title').innerHTML = venta
      ? '<i class="bi bi-trophy text-success"></i> Cerrar como Venta' : '<i class="bi bi-x-octagon text-danger"></i> Cerrar como No venta';
    modalEl.querySelector('.quien').textContent = opts.nombre || '';
    modalEl.querySelector('.venta-extra').style.display = venta ? '' : 'none';
    const cont = modalEl.querySelector('.tips');
    const grupos = {};
    lista.forEach(t => { (grupos[t.categoria || 'Otras'] = grupos[t.categoria || 'Otras'] || []).push(t); });
    cont.innerHTML = Object.keys(grupos).map(g => `<div class="section-title mt-2">${esc(g)}</div>` + grupos[g].map(t => `
      <label class="d-flex gap-2 p-2 rounded border mb-1" style="border-color:var(--border2)!important;cursor:pointer">
        <input type="radio" name="tip" value="${t.id}" class="form-check-input mt-1" data-accion-tip="${esc(t.accion)}" data-nota="${t.requiere_nota ? 1 : 0}">
        <span><span class="fw-semibold small">${esc(t.nombre)}</span>${t.accion === 'postergar' ? ' <span class="chip warn">no cierra: pausa y agenda</span>' : ''}
        ${t.accion === 'no_contactar' ? ' <span class="chip danger">bloquea contactos</span>' : ''}
        <span class="d-block muted" style="font-size:11.5px">${esc(t.descripcion)}</span></span></label>`).join('')).join('');
    const fecha = modalEl.querySelector('.fecha-wrap'), nota = modalEl.querySelector('[name=nota]');
    fecha.style.display = 'none';
    nota.value = '';
    modalEl.querySelector('[name=valor]').value = '';
    cont.querySelectorAll('input[name=tip]').forEach(r => r.addEventListener('change', () => {
      fecha.style.display = r.dataset.accionTip === 'postergar' ? '' : 'none';
      nota.placeholder = r.dataset.nota === '1' ? 'Obligatoria para esta tipificación' : 'Opcional';
    }));
    const modal = bootstrap.Modal.getOrCreateInstance(modalEl);
    let confirmado = false;
    const btn = modalEl.querySelector('.confirmar');
    btn.onclick = async () => {
      const sel = cont.querySelector('input[name=tip]:checked');
      if (!sel) { toast('Elegí una tipificación', 'warn'); return; }
      const data = {etapa: opts.etapa, tipificacion: sel.value, nota: nota.value,
                    fecha: modalEl.querySelector('[name=fecha]').value, valor: modalEl.querySelector('[name=valor]').value};
      btn.disabled = true;
      confirmado = true; modal.hide();
      try {
        const r = await mover(opts.op, data);
        toast(r.mensaje || 'Listo');
        if (opts.onOk) opts.onOk(r, sel.dataset.accionTip === 'postergar'); else setTimeout(() => location.reload(), 350);
      } catch (e) { if (opts.onCancel) opts.onCancel(); } finally { btn.disabled = false; }
    };
    modalEl.addEventListener('hidden.bs.modal', () => { if (!confirmado && opts.onCancel) opts.onCancel(); }, {once: true});
    modal.show();
  }

  document.addEventListener('DOMContentLoaded', () => {
    initSidebar(); initBuscador(); initNotificaciones(); if (telefono()) document.body.classList.add('en-marco');  // la barra del teléfono muestra la llamada
    initWidget(); initResultado(); initGuardarAdjunto(); initAcciones();
    if (document.body.dataset.auth === '1') pulso();
  });

  // El teléfono del marco avisa cuando atiende o corta: se refresca el aviso de llamada al toque
  const pulsoYa = () => { clearTimeout(pulsoTimer); pulsoTimer = setTimeout(pulso, 300); };
  window.CRM = {api, toast, esc, hace, llamar, abrirCierre, csrf, mover, pulsoYa};
})();
