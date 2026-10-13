# Pendiente: softphone dentro del CRM (JsSIP)

Estado: **implementado (11/10/2026). Probado OK con terminal real el 12/10/2026: llamada saliente desde el CRM.** Falta probar entrante, navegar con llamada, discador, eventos y grabación. Probado en navegador: el marco fijo
navega sin recargar el teléfono y la terminal se conecta a `wss://sipwss.anura.dev` y registra (con credenciales falsas
Anura responde "autenticación incorrecta", o sea que el camino funciona).

### Cómo quedó
- **Activar:** Configuración → Telefonía Anura → "Teléfono dentro del CRM" + dominio y puerto de la central.
- **Por agente:** Internos → usuario y contraseña SIP de la terminal "CRM" (contraseña cifrada en la base, se entrega
  solo al agente logueado por `POST /api/telephony/softphone/`, nunca va en el HTML).
- **Marco fijo** (`core/marco.py` + `telefonia/marco.html`): si el agente tiene teléfono, al abrir cualquier pantalla
  (encabezado `Sec-Fetch-Dest: document`) se sirve el marco con la barra del teléfono arriba y la pantalla en un iframe.
  La URL y el título de la pestaña siguen al iframe. `?sin_marco=1` lo desactiva para una carga.
- **Teléfono** (`static/js/telefono.js`, JsSIP 3.13.8 empaquetado en `static/vendor/`): atiende solo la pata del agente
  de las llamadas que dispara el CRM (Llamar / discador: aviso de la pantalla, encabezado de auto-answer de Anura o
  saliente viva en el CRM); las entrantes suenan con nombre del cliente y botón "Ficha"; cortar, silenciar, espera,
  teclado; aviso antes de cerrar con llamada; una sola pestaña registra (Web Locks).
- El aviso de llamada de las pantallas usa el teléfono para cortar y ya no muestra "Ir a Anura".

### Para probar (interno 126)
1. En Anura: CONFIGURACIÓN → CUENTAS → interno → MODIFICAR → pestaña TERMINALES → AGREGAR (nombre, usuario a
   elección; la contraseña la genera Anura) → marcarla **principal**. Para entrantes: sumarla a grupos de ringueo y colas.
2. En el CRM: activar el teléfono, cargar dominio/puerto y las credenciales en el interno.
3. Probar: registro (barra verde), Llamar desde una ficha (atiende sola), cortar desde el CRM, entrante, navegar con
   la llamada en curso, eventos y grabación.
Última actualización: 11/10/2026.

## Respuesta de Anura a las 7 preguntas (11/10/2026)

| # | Pregunta | Respuesta |
|---|---|---|
| 1 | Servidor WebSocket y dominio | WebSocket **`wss://sipwss.anura.dev`** · dominio de la central **`agrupacionsanatorial.grancentral.com.ar`**, puerto **55090**. Cada terminal se registra con su **usuario y contraseña SIP** |
| 2 | ¿SIP sobre WebSocket? | **Sí** (dan el WSS para eso) |
| 3 | STUN/TURN y códecs | **No hace falta STUN/TURN.** Códecs: **G711A, G711U o G722** |
| 4 | Segunda terminal y principal | La crea el admin por interno: https://kb.anura.com.ar/es/articles/2170210-como-crear-una-terminal-para-usar-en-un-softphone-de-forma-manual · en la misma sección se marca la **terminal principal** |
| 5 | Variables custom llamando directo por WebSocket | **No.** Recomiendan **seguir con Click2Dial para salientes** (conviven el botón y el softphone) → confirma el diseño |
| 6 | Restringir destinos | **Por interno** (no por terminal): sección *Límites* (celular, internacional, interno…): https://kb.anura.com.ar/es/articles/2411033-como-limitar-o-restringir-las-llamadas-salientes |
| 7 | Eventos y grabación desde la terminal WebSocket | **Sí, igual:** los eventos se disparan por cuenta de interno, no por terminal; graban si está habilitado |

Pendiente de confirmar: el dominio `agrupacionsanatorial…` ¿es la central de Roisa o la del interno de prueba 126?

## Por qué

Hoy (Click2Dial + Auto answer) el agente llama con un clic, pero:
- **no puede cortar desde el CRM** (Anura no permite cortar por API);
- **las entrantes se atienden en el Anura web**, no en el CRM.

Con un teléfono propio dentro del CRM se resuelven las dos cosas.

## Qué respondió Anura (email, 30/09/2026)

- Su **WebPhone** (`aPhone_browser`) **solo hace salientes**: descartado.
  Doc: https://kb.anura.com.ar/es/articles/3250715-webphone · https://kb.anura.com.ar/es/articles/2579993-agregar-click2call-en-la-web
- Recomiendan un **softphone propio con JsSIP** (https://jssip.net/): se comporta como una terminal SIP más del
  interno, **llama y recibe**, incluidas llamadas de **colas**.
- Cada interno tiene usuario y contraseña SIP propios, que autentican contra un **dominio y proxy general** de la central.
- **Cada interno admite hasta 3 terminales SIP sin costo.** Con 2 terminales (app de Anura + CRM) **suenan las dos**.
- **Click2Dial** hace sonar la terminal marcada como **principal**.
- Todo el tráfico WebRTC va por **HTTPS, puerto 443**.
- Roisa tiene **24 internos**; internos nuevos se suman a la factura mensual.
- Advierten **no exponer credenciales SIP** en una web a la que acceda cualquiera (su Click2Call público usa
  cuentas restringidas a "solo internos" y crédito 0).

## Qué falta preguntarle a Anura

1. **Servidor WebSocket SIP** (host, puerto, ruta) y **dominio SIP** para registrar la terminal. ¿Es `DOMINIO.grancentral.com.ar`?
2. ¿Aceptan **registro SIP sobre WebSocket (SIP.js / JsSIP)**? Su widget usa Verto (FreeSWITCH).
3. ¿Recomiendan **STUN/TURN**? ¿Qué **códecs** usan?
4. ¿Cómo se crea la **segunda terminal** por interno y se marca como **principal**? ¿Lo hace el admin de Roisa desde el panel?
5. Si llamamos **directo desde JsSIP** (sin Click2Dial), ¿se pueden mandar **variables custom** (como `custom1`) que lleguen en los eventos START/TALK/END?
6. ¿Se puede **limitar por terminal** qué destinos puede llamar (por seguridad)?
7. Las llamadas atendidas o hechas desde la terminal JsSIP, ¿disparan los **mismos eventos** (START / TALK / END) y se
   **graban** igual que las del softphone de Anura?

## Diseño propuesto

- **Una terminal "CRM" por interno**, marcada como principal (la app de Anura queda como segunda terminal opcional).
- **Teléfono embebido en la misma ventana del CRM (decidido: opción "marco fijo").** Hoy cada pantalla recarga la
  página entera y eso cortaría la llamada. Solución: la barra lateral, el encabezado y el teléfono quedan siempre
  cargados y al navegar se reemplaza solo el contenido del medio (Turbo / htmx, o contenido en un marco interno).
  El registro SIP y la llamada sobreviven al pasar del tablero a una ficha, etc.
  - Hay que adaptar la navegación de todas las pantallas (formularios, redirecciones, scripts por página, modales)
    y probarlas: +2–3 días sobre el piloto.
  - Caso sin solución: recargar a mano (F5) o cerrar la pestaña corta la llamada → aviso "tenés una llamada en curso"
    (`beforeunload`) antes de salir.
  - Si hay dos pestañas del CRM abiertas, solo una registra el teléfono (coordinación con BroadcastChannel / lock).
  - Descartada la ventanita aparte (popup): más simple, pero son dos ventanas y si se cierra no entran llamadas.
- **Salientes:** se sigue usando Click2Dial (mantiene `custom1` y los eventos); la ventanita **atiende sola** las
  llamadas que dispara el CRM. Alternativa: llamar directo con JsSIP si Anura confirma el punto 5.
- **Entrantes:** suenan en la ventanita con la ficha del cliente; se atienden y cortan desde el CRM.
- **Controles:** atender, cortar, silenciar, teclado DTMF; más adelante transferir y espera.
- **Eventos y registro:** sin cambios, siguen llegando por los webhooks de Anura.
- **Credenciales:** guardadas cifradas en el servidor (por interno, en *Telefonía Anura → Internos*); se entregan solo
  al agente logueado, solo las de su terminal, nunca escritas en el HTML. Pedir a Anura restringir destinos.
- **Un solo teléfono por agente:** bloquear una segunda ventanita/registro.
- Reconexión automática, elección de micrófono y auricular, aviso si el navegador bloquea el micrófono.

## JsSIP (lo relevado)

- Librería JavaScript de SIP para navegador, licencia MIT, de los autores del RFC 7118 (SIP sobre WebSocket).
  Docs: https://jssip.net/documentation/ (API: `/documentation/api/`, interoperabilidad: `/documentation/misc/interoperability/`).
- **Requisito:** la central debe aceptar **SIP sobre WebSocket (WSS)** → pregunta 2 a Anura. Anura parece correr
  FreeSWITCH (su widget usa Verto), que soporta WSS si está habilitado.
- Usaríamos: `UA` (registro con uri, password, sockets WSS), evento `newRTCSession` (entrantes), y en `RTCSession`:
  `answer`, `terminate`, `mute`/`unmute`, `hold`/`unhold`, `sendDTMF`, `refer` (transferir); `extraHeaders` en salientes.
- Requiere HTTPS (ya está) y Chrome / Edge / Firefox actuales.
- **Leer la API completa al arrancar el piloto** (en el relevamiento solo se vieron índices).

## Plan

1. **Piloto con el interno de prueba 126** (cuenta de otro cliente): 5–8 días. Navegación con marco fijo,
   registro, entrantes con ficha, atención automática de Click2Dial, cortar, silenciar, DTMF.
2. **Afinado:** reconexión, dispositivos de audio, un teléfono por agente, credenciales cifradas desde la pantalla.
3. **Roisa:** crear la terminal "CRM" en cada interno, probar con 1–2 agentes y después pasar a todos.

## Alternativas descartadas

- **WebPhone de Anura:** solo salientes.
- **Troncal SIP + central propia:** semanas de trabajo, responsabilidad total de la telefonía; solo tendría sentido
  para discado predictivo de alto volumen.

---

# Ideas a futuro (solo relevadas, sin decidir)

Consultadas el 30/09/2026, sin pedido formal del cliente.

| Función | Qué implica | Depende de |
|---|---|---|
| **Videollamada para auditoría médica** | Proveedor embebible (Jitsi propio, Daily, Whereby, Zoom SDK); agenda con link único por WhatsApp/email; sala desde la ficha; formulario de auditoría (apto / con condiciones / no apto); grabación con consentimiento; rol "Auditor médico". Datos de salud sensibles (ley 25.326). ~1–2 semanas. | Elegir proveedor y definir formulario |
| **CODEM desde CUIL** (obra social y familiares) | Campo CUIL validado, botón "Consultar", guardar resultado y fecha, familiares como contactos vinculados. ~1 semana. | **No hay API pública de ANSES.** Acceso institucional o proveedor de datos (Nosis, Veraz, SIISA) |
| **Padrón de opciones SSS** (obra social vigente e historial) | Igual que CODEM; útil para calificar el lead y para reglas de asignación. ~1 semana compartida. | Credenciales de la Superintendencia o proveedor de datos |

Preguntas para el cliente antes de presupuestar: cómo consultan hoy CODEM y el padrón; si tienen credenciales de
ANSES / SSS o contratan un proveedor; si graban la auditoría, qué formulario usan y quiénes son los auditores.
