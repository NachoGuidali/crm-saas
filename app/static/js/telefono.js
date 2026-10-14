/*
 * Teléfono del CRM: terminal SIP por WebSocket (JsSIP) que vive en el marco fijo (telefonia/marco.html).
 * Las pantallas del CRM cargan en el iframe #pantalla; navegar no recarga esta página, así que la llamada sigue.
 *
 * - Salientes: las sigue originando Click2Dial (Anura llama primero a esta terminal, que es la principal) y el
 *   teléfono atiende solo las llamadas que dispara el CRM.
 * - Entrantes: suenan acá con el nombre del cliente y el botón para abrir su ficha.
 * - Una sola pestaña registra el teléfono (Web Locks); las demás muestran el aviso.
 * Desde las pantallas: window.top.CRMTelefono.{esperarSaliente, cortar, enLlamada}.
 */
(function () {
  'use strict';
  const $ = s => document.querySelector(s);
  const bar = $('#tel'), frame = $('#pantalla'), audio = $('#audioRemoto');
  const csrf = () => $('#csrf').value;
  let ua = null, sesion = null, inicio = null, reloj = null, salienteHasta = 0, timbre = null;
  let micSilenciado = false, enEspera = false;       // controles de la llamada en curso
  let disponible = true, autoRegistroSaliente = false, internoActual = '';  // disponibilidad en la cola

  /* ── Navegación: la URL y el título de la pestaña siguen a la pantalla del iframe ── */
  frame.addEventListener('load', () => {
    try {
      const l = frame.contentWindow.location;
      if (l.pathname.startsWith('/usuarios/login')) { location.replace(l.href); return; }
      history.replaceState(null, '', l.pathname + l.search + l.hash);
      document.title = frame.contentDocument.title;
    } catch (e) { /* pantalla de otro dominio: no se sincroniza */ }
  });

  /* ── Estado en la barra ── */
  function estado(clave, texto) {
    bar.dataset.estado = clave;
    $('#tel .estado .txt').textContent = texto;
  }
  function aviso(texto) { $('#tel .aviso').textContent = texto || ''; }

  function mostrarLlamada(modo, quien) {
    bar.classList.toggle('sonando', modo === 'sonando');
    bar.classList.toggle('en-llamada', modo === 'en-llamada');
    if (quien !== undefined) pintarQuien(quien);
    const enCurso = modo === 'en-llamada';
    $('#tel .atender').hidden = modo !== 'sonando';
    ['.silenciar', '.espera', '.transferir', '.abrir-teclado'].forEach(s => { $('#tel ' + s).hidden = !enCurso; });
    $('#tel .cortar').innerHTML = modo === 'sonando' ? '<i class="bi bi-telephone-x"></i> Rechazar'
                                                      : '<i class="bi bi-telephone-x"></i> Cortar';
    if (!modo) { $('#teclado').hidden = true; $('#transferir').hidden = true; $('#tel .reloj').textContent = ''; }
    const disp = $('#tel .disponible');  // la disponibilidad solo se cambia fuera de una llamada
    if (disp) disp.hidden = enCurso || modo === 'sonando' || !ua;
  }

  function pintarQuien(q) {
    const el = $('#tel .quien');
    el.textContent = '';
    const b = document.createElement('b');
    b.textContent = q.nombre || q.numero || 'Número privado';
    el.appendChild(b);
    if (q.nombre && q.numero) el.appendChild(document.createTextNode(' · ' + q.numero));
    const ficha = $('#tel .ver-ficha');
    ficha.hidden = !q.url;
    ficha.dataset.url = q.url || '';
  }

  $('#tel .ver-ficha').addEventListener('click', e => {
    e.preventDefault();
    if (e.currentTarget.dataset.url) frame.src = e.currentTarget.dataset.url;
  });

  function arrancarReloj() {
    inicio = Date.now();
    clearInterval(reloj);
    const tick = () => {
      const s = Math.floor((Date.now() - inicio) / 1000);
      $('#tel .reloj').textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
    };
    tick(); reloj = setInterval(tick, 1000);
  }

  /* ── Timbre (Web Audio; el navegador lo permite después de un clic en el CRM) ── */
  function sonarTimbre() {
    pararTimbre();
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      const pulso = () => {
        [0, 0.45].forEach(t => {
          const o = ctx.createOscillator(), g = ctx.createGain();
          o.frequency.value = 440; o.connect(g); g.connect(ctx.destination);
          g.gain.setValueAtTime(0.06, ctx.currentTime + t);
          o.start(ctx.currentTime + t); o.stop(ctx.currentTime + t + 0.35);
        });
      };
      pulso();
      timbre = {ctx, id: setInterval(pulso, 3000)};
    } catch (e) { timbre = null; }
  }
  function pararTimbre() {
    if (timbre) { clearInterval(timbre.id); timbre.ctx.close().catch(() => {}); timbre = null; }
  }

  /* ── Avisar a la pantalla (iframe) para que refresque el estado de la llamada ── */
  function avisarPantalla() {
    try { frame.contentWindow.CRM && frame.contentWindow.CRM.pulsoYa && frame.contentWindow.CRM.pulsoYa(); } catch (e) { /* */ }
  }

  /* ── Sesiones ── */
  function conectarAudio(s) {
    s.on('peerconnection', e => {
      e.peerconnection.addEventListener('track', ev => { audio.srcObject = ev.streams[0]; });
    });
  }

  // Respaldo del teléfono: avisa al CRM al atender y al cortar, por si el webhook de Anura no llega.
  function avisarBackend(accion, cuerpo) {
    return fetch('/api/telephony/softphone/' + accion + '/', {
      method: 'POST', credentials: 'same-origin',
      headers: {'X-CSRFToken': csrf(), 'Content-Type': 'application/json'},
      body: JSON.stringify(cuerpo || {}),
    }).then(r => r.json()).catch(() => null);
  }
  // El cierre se pospone 8 s: si el END de Anura ya llegó, el servidor no hace nada (idempotente).
  function cerrarBackend() { setTimeout(() => avisarBackend('corto', {}), 8000); }

  function terminar() {
    pararTimbre();
    clearInterval(reloj);
    sesion = null;
    micSilenciado = false; enEspera = false;
    audio.muted = false;
    $('#tel .silenciar').classList.remove('active');
    $('#tel .espera').classList.remove('active');
    mostrarLlamada(null);
    audio.srcObject = null;
    avisarPantalla();
    // Si nos habíamos registrado solo para esta saliente estando en pausa, volvemos a "No disponible".
    if (autoRegistroSaliente) { autoRegistroSaliente = false; ponerDisponible(false); }
  }

  /* ── Mic / espera: Anura rechaza el hold SIP y corta, así que la espera silencia en ambos sentidos ── */
  function aplicarAudio() {
    if (!sesion) return;
    const mutear = micSilenciado || enEspera;
    try { if (sesion.isMuted().audio !== mutear) (mutear ? sesion.mute({audio: true}) : sesion.unmute({audio: true})); } catch (e) { /* */ }
    audio.muted = enEspera;  // en espera el cliente no nos escucha ni nosotros a él (silencio, sin música)
  }

  /** ¿Es la pata del agente de una llamada que disparó el CRM (Click2Dial)? Devuelve {auto, quien}. */
  async function llamadaDelCRM(request) {
    let ll = null;
    try {  // la llamada saliente en curso del agente según el CRM (Llamar o discador)
      const r = await fetch('/api/telephony/estado/', {credentials: 'same-origin'});
      ll = (await r.json()).llamada;
    } catch (e) { /* sin datos */ }
    const saliente = !!(ll && ll.viva && ll.direccion === 'OUT');
    const quien = saliente ? {nombre: ll.nombre, numero: ll.numero, url: ll.url} : null;
    // 1) La pantalla acaba de pedir una llamada  2) Anura la marca para atender sola  3) el CRM tiene una saliente viva
    const info = (request.getHeader('Call-Info') || '') + ' ' + (request.getHeader('Alert-Info') || '');
    const auto = Date.now() < salienteHasta || /answer-after|auto[-_ ]?answer|intercom/i.test(info) || saliente;
    return {auto, quien};
  }

  function atender() {
    if (!sesion) return;
    pararTimbre();
    try {
      sesion.answer({mediaConstraints: {audio: true, video: false}});
    } catch (e) {
      aviso('No se pudo atender: ' + e.message);
    }
  }

  async function nuevaSesion(e) {
    const s = e.session;
    if (e.originator !== 'remote') return;
    if (sesion) { s.terminate({status_code: 486, reason_phrase: 'Busy Here'}); return; }
    sesion = s;
    conectarAudio(s);
    const numero = s.remote_identity && s.remote_identity.uri ? s.remote_identity.uri.user : '';
    const nombreSip = s.remote_identity ? s.remote_identity.display_name : '';
    s.on('accepted', () => {
      mostrarLlamada('en-llamada'); arrancarReloj(); avisarPantalla();
      // Respaldo por si no llega el webhook; la respuesta trae la ficha (sirve si el cliente es de otra cartera).
      avisarBackend('registro', {numero: numero, direccion: 'IN'}).then(d => {
        if (sesion === s && d && d.llamada && (d.llamada.nombre || d.llamada.url)) {
          pintarQuien({nombre: d.llamada.nombre, numero: d.llamada.numero, url: d.llamada.url});
        }
      });
    });
    s.on('confirmed', () => { mostrarLlamada('en-llamada'); });
    s.on('ended', () => { cerrarBackend(); terminar(); });
    s.on('failed', () => { cerrarBackend(); terminar(); });
    s.on('getusermediafailed', () => {
      aviso('El navegador no dejó usar el micrófono: habilitalo (ícono del candado en la barra de direcciones).');
      const m = $('#tel .microfono'); if (m) m.hidden = false;
    });

    const crm = await llamadaDelCRM(e.request);
    if (crm.auto) {
      salienteHasta = 0;
      mostrarLlamada('en-llamada', crm.quien || {nombre: 'Llamando…', numero: ''});
      atender();
      return;
    }
    mostrarLlamada('sonando', {nombre: nombreSip, numero: numero});
    sonarTimbre();
    fetch('/api/telephony/quien/?numero=' + encodeURIComponent(numero), {credentials: 'same-origin'})
      .then(r => r.json()).then(q => { if (sesion === s && (q.nombre || q.url)) pintarQuien(q); }).catch(() => {});
  }

  /* ── Controles ── */
  $('#tel .atender').addEventListener('click', atender);
  $('#tel .cortar').addEventListener('click', () => cortar());
  $('#tel .silenciar').addEventListener('click', ev => {
    if (!sesion) return;
    micSilenciado = !micSilenciado;
    aplicarAudio();
    ev.currentTarget.classList.toggle('active', micSilenciado);
    ev.currentTarget.title = micSilenciado ? 'Activar micrófono' : 'Silenciar micrófono';
  });
  $('#tel .espera').addEventListener('click', ev => {
    if (!sesion) return;
    enEspera = !enEspera;
    aplicarAudio();
    ev.currentTarget.classList.toggle('active', enEspera);
    ev.currentTarget.title = enEspera ? 'Quitar de espera' : 'Poner en espera';
    aviso(enEspera ? 'En espera (el cliente no te escucha)' : '');
  });
  $('#tel .abrir-teclado').addEventListener('click', () => { $('#teclado').hidden = !$('#teclado').hidden; });

  /* ── Transferir: REFER directo al interno de otro agente ── */
  $('#tel .transferir').addEventListener('click', async () => {
    const panel = $('#transferir');
    if (!panel.hidden) { panel.hidden = true; return; }
    panel.textContent = 'Cargando…';
    panel.hidden = false;
    try {
      const r = await fetch('/api/telephony/agentes/', {credentials: 'same-origin'});
      const d = await r.json();
      panel.textContent = '';
      const lista = (d.agentes || []).filter(a => a.destino);
      if (!lista.length) { panel.textContent = 'No hay agentes con interno para transferir.'; return; }
      lista.forEach(a => {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'btn btn-sm btn-outline-secondary';
        const libre = a.estado === 'libre';
        const nom = document.createElement('span');
        nom.textContent = `${a.nombre} · ${a.interno}`;
        const est = document.createElement('span');
        est.className = libre ? 'text-success' : 'text-warning';
        est.textContent = libre ? 'libre' : 'en llamada';
        b.append(nom, est);
        b.addEventListener('click', () => transferir(a.destino, a.nombre));
        panel.appendChild(b);
      });
    } catch (e) { panel.textContent = 'No se pudo cargar la lista.'; }
  });

  function transferir(destino, nombre) {
    if (!sesion || !destino) return;
    $('#transferir').hidden = true;
    aviso('Transfiriendo a ' + nombre + '…');
    try {
      sesion.refer(destino, {eventHandlers: {
        // Si Anura rechaza el REFER, la llamada sigue en curso (no se corta).
        requestFailed: e => aviso('Anura rechazó la transferencia' + (e && e.cause ? ' (' + e.cause + ')' : '') + '. La llamada sigue.'),
        failed: () => aviso('No se pudo transferir. La llamada sigue.'),
        accepted: () => aviso('Transferida a ' + nombre + '.'),
      }});
    } catch (e) { aviso('No se pudo transferir: ' + e.message + '. La llamada sigue.'); }
  }

  /* ── Disponibilidad en la cola (registrar / desregistrar la terminal) ── */
  function pintarDisponible() {
    const b = $('#tel .disponible');
    if (!b) return;
    b.classList.toggle('en-pausa', !disponible);
    b.innerHTML = disponible ? '<i class="bi bi-check-circle"></i> Disponible'
                             : '<i class="bi bi-pause-circle"></i> No disponible';
    b.title = disponible ? 'Dejá de recibir llamadas de la cola' : 'Volvé a recibir llamadas de la cola';
  }
  function ponerDisponible(v) {
    if (!ua) return;
    disponible = v;
    try { v ? ua.register() : ua.unregister(); } catch (e) { /* */ }
    pintarDisponible();
  }
  // Antes de una saliente: si está en pausa, registrar y esperar el registro antes de disparar el Click2Dial.
  function asegurarRegistrado() {
    return new Promise(resolve => {
      if (!ua || ua.isRegistered()) return resolve();
      autoRegistroSaliente = !disponible;  // estaba en pausa: al terminar la llamada volvemos a pausa
      disponible = true;
      const listo = () => { try { ua.removeListener("registered", listo); } catch (e) { /* */ } clearTimeout(t); pintarDisponible(); resolve(); };
      const t = setTimeout(() => { try { ua.removeListener("registered", listo); } catch (e) { /* */ } resolve(); }, 6000);
      ua.on('registered', listo);
      try { ua.register(); } catch (e) { resolve(); }
    });
  }
  const btnDisp = $('#tel .disponible');
  if (btnDisp) btnDisp.addEventListener('click', () => { if (!sesion) ponerDisponible(!disponible); });
  $('#teclado').addEventListener('click', ev => {
    const t = ev.target.closest('[data-tono]');
    if (t && sesion) { try { sesion.sendDTMF(t.dataset.tono); } catch (e) { /* */ } }
  });
  $('#tel .microfono').addEventListener('click', async ev => {
    try {
      const st = await navigator.mediaDevices.getUserMedia({audio: true});
      st.getTracks().forEach(t => t.stop());
      ev.currentTarget.hidden = true;
      aviso('');
    } catch (e) {
      aviso('El micrófono está bloqueado: habilitalo desde el candado de la barra de direcciones.');
    }
  });

  function cortar() {
    if (demoLlamada) {  // modo demostración: corta la llamada simulada del CRM
      fetch('/api/telephony/hangup/' + demoLlamada.id, {method: 'PUT', credentials: 'same-origin',
                                                        headers: {'X-CSRFToken': csrf()}})
        .finally(avisarPantalla);
      return true;
    }
    if (!sesion) return false;
    try { sesion.terminate(); } catch (e) { terminar(); }
    return true;
  }

  /* ── La pantalla informa la llamada del CRM (nombre y ficha); en modo demostración la barra la muestra ── */
  let demoLlamada = null;
  function reflejar(ll) {
    if (sesion) {  // teléfono real: completar quién es
      if (ll && ll.viva && ll.nombre) pintarQuien({nombre: ll.nombre, numero: ll.numero, url: ll.url});
      return;
    }
    if (!window.CRMTelefono.demo) return;
    if (ll && ll.viva) {
      const nueva = !demoLlamada || demoLlamada.id !== ll.id;
      demoLlamada = ll;
      mostrarLlamada('en-llamada', {nombre: ll.nombre, numero: ll.numero, url: ll.url});
      ['.silenciar', '.espera', '.transferir', '.abrir-teclado'].forEach(x => { $('#tel ' + x).hidden = true; });
      if (nueva || ll.atendida) { inicio = Date.now() - (ll.segundos || 0) * 1000; }
      if (nueva) arrancarRelojDesde(inicio);
    } else if (demoLlamada) {
      demoLlamada = null;
      clearInterval(reloj);
      mostrarLlamada(null);
    }
  }
  function arrancarRelojDesde(desde) {
    clearInterval(reloj);
    const tick = () => {
      const s = Math.max(0, Math.floor((Date.now() - desde) / 1000));
      $('#tel .reloj').textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
    };
    tick(); reloj = setInterval(tick, 1000);
  }

  /* ── API para las pantallas del CRM (iframe) ── */
  window.CRMTelefono = {
    esperarSaliente() { salienteHasta = Date.now() + 45000; return asegurarRegistrado(); },
    cortar,
    reflejar,
    enLlamada() { return !!(sesion || demoLlamada); },
    listo() { return !!(ua && ua.isRegistered()); },
  };

  window.addEventListener('beforeunload', ev => {
    if (sesion) { ev.preventDefault(); ev.returnValue = ''; }
  });
  window.addEventListener('pagehide', () => {
    try { if (ua) ua.stop(); } catch (e) { /* */ }
  });

  /* ── Micrófono: pedir permiso de entrada si todavía no se dio ── */
  async function revisarMicrofono() {
    try {
      const p = await navigator.permissions.query({name: 'microphone'});
      const pintar = () => { const m = $('#tel .microfono'); if (m) m.hidden = p.state === 'granted'; };
      pintar(); p.onchange = pintar;
    } catch (e) { /* Firefox viejo: se pide al atender */ }
  }

  /* ── Registro SIP ── */
  async function iniciar() {
    if (!window.JsSIP) { estado('error', 'No cargó el teléfono'); return; }
    let cred;
    try {
      const r = await fetch('/api/telephony/softphone/', {method: 'POST', credentials: 'same-origin',
                                                           headers: {'X-CSRFToken': csrf()}});
      cred = await r.json();
      if (!r.ok || cred.ok === false) throw new Error(cred.error || r.status);
    } catch (e) {
      estado('error', 'Teléfono sin configurar');
      aviso(e.message);
      return;
    }
    if (cred.demo) {  // sin Anura: las llamadas las simula el CRM y se cortan desde el aviso de llamada
      estado('listo', 'Teléfono en modo demostración · interno ' + cred.interno);
      aviso('Las llamadas se simulan; para hablar de verdad, desactivá el modo demostración.');
      window.CRMTelefono.demo = true;
      $('#tel .microfono').remove();  // en demo no se usa el micrófono
      return;
    }
    if (cred.falta) {
      estado('error', 'Teléfono sin configurar · interno ' + cred.interno);
      aviso(cred.falta);
      return;
    }
    ua = new JsSIP.UA({
      sockets: [new JsSIP.WebSocketInterface(cred.ws)],
      uri: cred.uri,
      authorization_user: cred.usuario,
      password: cred.password,
      registrar_server: cred.registrar,
      display_name: cred.nombre,
      user_agent: 'CRM SupReg (JsSIP)',
      register: true,
      session_timers: false,
      connection_recovery_min_interval: 2,
      connection_recovery_max_interval: 30,
    });
    cred.password = '';
    internoActual = cred.interno;
    ua.on('connecting', () => estado('conectando', 'Conectando teléfono…'));
    ua.on('connected', () => estado('conectando', 'Registrando interno ' + cred.interno + '…'));
    ua.on('disconnected', () => estado('error', 'Sin conexión con la central (reintentando)'));
    ua.on('registered', () => {
      estado('listo', 'Teléfono listo · interno ' + cred.interno); aviso('');
      disponible = true;
      const b = $('#tel .disponible'); if (b && !sesion) b.hidden = false;
      pintarDisponible();
    });
    // "No disponible" es un unregister a propósito: ahí no es un error, es que el agente se puso en pausa.
    ua.on('unregistered', () => {
      if (!disponible) estado('listo', 'No disponible · interno ' + cred.interno);
      else estado('error', 'Teléfono desconectado');
    });
    ua.on('registrationFailed', e => {
      estado('error', 'No se pudo registrar el interno ' + cred.interno);
      aviso(e.cause === 'Authentication Error' ? 'Usuario o contraseña SIP incorrectos.' : (e.cause || ''));
    });
    ua.on('newRTCSession', nuevaSesion);
    ua.start();
  }

  revisarMicrofono();
  // Una sola pestaña registra el teléfono del agente. Si otra lo tiene, esta espera y lo toma apenas se cierre
  // (también cubre el instante de una recarga, cuando la página anterior todavía no lo soltó).
  if (navigator.locks) {
    const espera = setTimeout(() => {
      estado('error', 'El teléfono está abierto en otra pestaña');
      aviso('Las llamadas suenan en la otra pestaña del CRM. Si la cerrás, el teléfono pasa a esta.');
    }, 2500);
    navigator.locks.request('crm-telefono', () => {
      clearTimeout(espera);
      aviso('');
      iniciar();
      return new Promise(() => {});  // se libera solo al cerrar la pestaña
    });
  } else {
    iniciar();
  }
})();
