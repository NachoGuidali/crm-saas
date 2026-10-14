# Integración de un CRM con Anura (telefonía en la nube)

Guía completa y reutilizable para conectar un CRM web con **Anura** (Argentina / Perú): llamar con un clic, registrar
todas las llamadas, grabaciones, discador progresivo y **teléfono dentro del CRM** (atender, llamar y cortar sin abrir
Anura). Escrita a partir de la documentación de Anura, de lo que confirmó su soporte (sep–oct 2026) y de las pruebas
reales hechas en este proyecto.

> **Estado (14/10/2026):** probado contra Anura real: Click2Dial, eventos START/TALK/END, entrantes y salientes,
> teléfono dentro del CRM (JsSIP) con saliente, entrante y corte desde el CRM. Implementados también los controles
> durante la llamada (espera propia, transferir por REFER, disponible/no disponible) y el acceso temporal a la ficha
> de otra cartera cuando se atiende una llamada de ese cliente (ver 7.5).

**Índice:** 1. Arquitectura · 2. Qué pedirle al cliente · 3. Panel de Anura paso a paso · 4. Click2Dial ·
5. Eventos (webhooks) · 6. Cómo procesar los eventos · 7. Teléfono dentro del CRM · 8. Discador progresivo ·
9. Configuración del CRM · 10. Checklist de puesta en marcha · 11. Problemas conocidos y soluciones ·
12. Código para reutilizar

---

## 1. Arquitectura

```
          ┌──────────── Navegador del agente ────────────┐
          │  Marco fijo: barra del teléfono (JsSIP)       │◄── SIP sobre WebSocket ──► wss://sipwss.anura.dev
          │  └─ iframe: pantallas del CRM                 │                              (registro y audio)
          └───────────────┬───────────────────────────────┘
                          │ "Llamar" (fetch)
                          ▼
                    Servidor del CRM ── POST Click2Dial ──► Anura (api.anura.com.ar)
                          ▲                                       │
                          └──── Webhooks START / TALK / END ◄─────┘ (por cuenta / interno)
```

- **Anura sigue siendo la central.** No hace falta troncal SIP ni central propia.
- **Salientes:** el CRM pide la llamada por **Click2Dial**; Anura llama primero a la terminal principal del agente y,
  cuando atiende, disca al cliente.
- **Registro:** Anura avisa cada llamada con **webhooks** (START / TALK / END). El CRM los procesa en un solo lugar.
- **Teléfono dentro del CRM (opcional):** una **terminal SIP propia** en el navegador (JsSIP) registrada en Anura
  como una terminal más del interno. Atiende, corta, silencia, pone en espera. Las pantallas cargan en un **iframe**
  para que navegar no corte la llamada.

### Qué permite Anura y qué no

| Necesidad | ¿Se puede? | Cómo |
|---|---|---|
| Originar una llamada desde el CRM | Sí | API **Click2Dial** (token Bearer) |
| Pasar datos propios a la llamada (id del CRM) | Sí | `customs` de Click2Dial → vuelven como `{{ custom1 }}`… en los eventos |
| Enterarse de las llamadas | Sí | **Eventos (WebHooks templetizados)** por cuenta |
| Grabaciones | Sí | Variable `{{ audio_file_mp3 }}` en el evento de fin |
| Cortar una llamada por API | **No** | No hay API (Tenant CallManager no disponible). Se corta desde la terminal → por eso el teléfono propio |
| Atender / cortar desde la web del CRM | Sí | **Terminal SIP propia con JsSIP** por WebSocket |
| Variables custom llamando directo desde JsSIP | **No** | Anura lo confirmó: seguir originando con **Click2Dial** |
| Discado predictivo | No | Click2Dial es de a una llamada → **discador progresivo** |
| WebPhone de Anura embebido | Solo salientes | Descartado para este uso |
| API de tenant (CDRs, bloqueados) | Sin confirmar | Opcional, apagada por defecto |

---

## 2. Qué pedirle al cliente (admin de su cuenta de Anura)

1. **Token de Click2Dial** (panel → *Integraciones → Click 2 Dial*). Soporte de Anura: 011-5263-0000 opción 3,
   soporte@anura.com.ar, chat del panel.
2. **Interno (extensión) de cada agente.**
3. **Dominio de la central** (ej. `empresa.grancentral.com.ar`) y puerto (Anura nos indicó **55090**) — para el
   teléfono dentro del CRM.
4. Por cada agente que use el teléfono del CRM: **una terminal nueva** en su interno (ver 3.3) y su **usuario y
   contraseña SIP**.
5. **Números (DID) y colas** de entrada y a qué embudo/campaña va cada uno.
6. Alguien con rol de configuración para cargar los **Eventos** (o que los carguen con esta guía).
7. Saber si comparten la cuenta con otras áreas (para filtrar qué llamadas procesa el CRM).

Datos de Anura útiles: cada interno admite **hasta 3 terminales SIP sin costo**; los internos extra se cobran.

---

## 3. Panel de Anura paso a paso

### 3.1 Plantilla de eventos (una sola vez)
*Configuración → Eventos → Agregar*:

| Campo | Valor |
|---|---|
| Protocolo / Puerto | HTTPS / 443 |
| Host | dominio del CRM (ej. `crm.cliente.com.ar`) |
| Ruta | `api/integrations/anura/webhook` (sin barra inicial) |
| Método / Content-Type | POST / JSON |
| Autorización | Bearer `<token del webhook del CRM>` — o en la ruta: `api/integrations/anura/webhook?token=<token>` |
| Cuerpo | la plantilla de la sección 5 |
| **Activo** | ✔ (si no, no llega nada) |

### 3.2 Triggers en cada cuenta (interno de cada agente, y cuentas de colas / DID)
*Configuración → Cuentas → (cuenta) → Modificar → pestaña **Eventos** → Agregar*, tres veces con la misma plantilla:

| Nombre | Evento | Dirección | Para qué |
|---|---|---|---|
| CRM inicio | `START` | **BOTH** | Empieza a sonar |
| CRM atendida | `TALK` | **BOTH** | Alguien atendió |
| CRM fin | `END` | **BOTH** | Duración, resultado, grabación |

Etiquetas y filtros vacíos, **Activo** ✔. Dirección **BOTH** (si queda en OUT, las entrantes no se registran).
Una cuenta sin triggers no avisa nada al CRM.

### 3.3 Terminal para el teléfono del CRM (por agente)
Artículo: <https://kb.anura.com.ar/es/articles/2170210-como-crear-una-terminal-para-usar-en-un-softphone-de-forma-manual>

1. *Configuración → Cuentas → (interno del agente) → Modificar → pestaña **Terminales** → **Agregar**.*
2. **Nombre:** "CRM". **Usuario:** a elección, único (ej. `126crm`) → **es el usuario SIP**.
3. **Contraseña:** la genera Anura (se ve al modificar la terminal o se pide a soporte).
4. **Aplicar** y marcar la terminal como **principal** (en la misma sección). La principal es la que Click2Dial
   llama primero.
5. **Entrantes por grupo de ringueo o cola:** agregar la terminal al grupo y, si el grupo apunta a una cola, a la cola.
   Las llamadas **directas al interno** suenan en todas sus terminales.
6. **Auto answer** (*Cuentas → (cuenta) → Avanzado*): con el teléfono del CRM **desactivarlo** (ver 11).
7. **Límites** (seguridad): por interno se pueden restringir salientes a celular, internacional, etc.:
   <https://kb.anura.com.ar/es/articles/2411033-como-limitar-o-restringir-las-llamadas-salientes>

### 3.4 Datos de conexión SIP (los dio Anura)

| Dato | Valor |
|---|---|
| WebSocket | `wss://sipwss.anura.dev` |
| Dominio | el de la central del cliente (`<empresa>.grancentral.com.ar`) |
| Puerto | `55090` |
| Credenciales | usuario y contraseña SIP **de la terminal** |
| STUN / TURN | **No hace falta** |
| Códecs | G711A (PCMA), G711U (PCMU) o G722 |
| Eventos y grabación | Iguales para cualquier terminal: se disparan **por cuenta (interno)**, no por terminal |

---

## 4. Originar llamadas: Click2Dial

Doc: <https://kb.anura.com.ar/es/articles/3250664-api-click-to-dial-click2dial>

```
POST https://api.anura.com.ar/adapter/default/click2call        (Perú: https://api.anura.pe/...)
Authorization: Bearer <TOKEN_CLICK2DIAL>
Content-Type: application/x-www-form-urlencoded

called=5491155554444&extension=126&customs=crm-1234
```

| Campo | Qué es |
|---|---|
| `called` | Número a llamar, sin `+` |
| `extension` | Interno del agente |
| `customs` | Hasta 6 (se repite el campo). Vuelven como `{{ custom1 }}`…`{{ custom6 }}` |

- Responde **200 con cuerpo vacío** si la acepta (no devuelve id).
- **Unir la llamada con sus eventos:** crear el registro de la llamada **antes** de llamar y mandar `crm-<id>` en
  `customs`. El evento trae `custom1 = crm-<id>`.
- **Errores:**

| Respuesta | Significa |
|---|---|
| 400 `No Originate terminal for extension` | El interno no tiene terminal principal |
| 400 `Originate Terminal is not registered` | La terminal principal no está conectada (el teléfono del CRM cerrado, la app apagada…) |
| 401 `SecretId [n] not found` | Token inválido |

- **Verificar el token sin llamar a nadie:** POST con una extensión inexistente → token válido = 400, inválido = 401.

---

## 5. Eventos (webhooks)

Docs: <https://kb.anura.com.ar/es/articles/2579270-webhooks> ·
variables: <https://kb.anura.com.ar/es/articles/2579414-variables-eventos-templetizados>

### Cuerpo de la plantilla
```json
{
  "callId": "{{ cdrid }}",
  "event": "{{ hooktrigger }}",
  "direction": "{{ direction }}",
  "status": "{{ status }}",
  "calling": "{{ calling }}",
  "callingName": "{{ callingname }}",
  "called": "{{ called }}",
  "dialTime": "{{ dialtime }}",
  "billSeconds": "{{ billseconds }}",
  "wasRecorded": "{{ wasrecorded }}",
  "answerExtension": "{{ answerextension }}",
  "answerTerminal": "{{ answerterminal }}",
  "queueAgentExtension": "{{ queueagentextension }}",
  "accountExtension": "{{ accountextension }}",
  "accountName": "{{ accountname }}",
  "queueId": "{{ queueid }}",
  "queueName": "{{ queuename }}",
  "custom1": "{{ custom1 }}",
  "recordingUrl": "{{ audio_file_mp3 }}"
}
```

### Ejemplos reales recibidos
Entrante al interno 126 (START → TALK → END):
```json
{"accountExtension": "126", "accountName": "Empresa", "answerExtension": "", "answerTerminal": "",
 "billSeconds": "0", "callId": "1385390057", "called": "126", "calling": "1155550000", "custom1": "",
 "dialTime": "2026-10-12 21:56:24", "direction": "IN", "event": "START", "queueId": "0",
 "recordingUrl": "", "status": "DIALING", "wasRecorded": "false"}
```
TALK de la misma: `"event": "TALK", "status": "TALKING", "billSeconds": "1"`.
Saliente por Click2Dial: `"direction": "OUT", "calling": "126", "called": "5491155550000", "custom1": "crm-585"`.

**Notas importantes de lo observado:**
- `answerExtension` y `answerTerminal` llegaron **vacíos** (incluso atendidas). Para saber el agente usar
  `accountExtension` (y el usuario SIP de la terminal como alias).
- Todos los valores llegan como **texto** (`"billSeconds": "0"`, `"wasRecorded": "false"`).
- `dialTime` llega como `AAAA-MM-DD HH:MM:SS` en **hora local** (Argentina), sin zona.
- `status` visto: `DIALING`, `TALKING` en START/TALK; en END: ANSWER/ANSWERED, NOANSWER, BUSY, FAILED, CANCEL…
- Anura puede **reenviar** eventos: procesar de forma **idempotente** por `callId`.

---

## 6. Cómo procesar los eventos (lógica recomendada)

Una sola función recibe todo (webhook, modo demo, polling, y los avisos del teléfono del CRM):

1. **Responder 200 enseguida** y procesar en segundo plano (Anura reintenta si tardás). Validar el token.
2. **Guardar cada aviso** con el resultado ("registrada / ignorada y por qué") y mostrarlo en la configuración:
   es la herramienta de diagnóstico más útil (ver 11).
3. **Encontrar la llamada**, en este orden:
   - `custom1 = crm-<id>` → la que creó el CRM al llamar (si tenía un id provisorio, reemplazarlo por el `callId`).
   - `callId` ya conocido.
   - Saliente sin id aún: misma línea, mismo número, mismo agente, últimos 15 min.
   - Entrante ya registrada por el teléfono del CRM (id `sip-…`): mismo número y agente, últimos 15 min.
   - Si no, crear.
4. **Número externo:** IN → `calling`; OUT → `called`. Normalizar (en AR: `011 15…`, `+54 9…`, `11…` → `+549…`).
5. **Agente:** por interno / alias / **usuario SIP de la terminal**, en orden: quien atendió (`answerExtension`,
   `answerTerminal`, `queueAgentExtension`) → cuenta (`accountExtension`, `accountName`).
6. **Estado:** START → sonando · TALK → en curso · END → final (atendida si `billSeconds > 0` o status ANSWER; si no,
   no atendida / ocupado según `status`).
7. **CRM:** número conocido → a su ficha; nuevo → contacto + oportunidad (en entrantes, de quien atiende);
   no atendida → aviso + tarea "Devolver llamada"; saliente → intento de contacto.
8. **Grabación:** con el END, encolar la descarga de `recordingUrl` (reintentos) y adjuntarla.
9. **Filtro opcional** "solo llamadas propias": procesar solo si es `crm-…`, de un interno/alias conocido, de una
   cola/DID cargada, o una llamada ya seguida. Útil si la cuenta de Anura se comparte.

---

## 7. Teléfono dentro del CRM (JsSIP)

### 7.1 Librería
JsSIP (MIT) <https://jssip.net/>. El paquete de npm **no trae un archivo para el navegador**: se empaqueta una vez
con esbuild y se sirve desde el propio CRM:
```bash
docker run --rm node:20-alpine sh -c "cd /tmp && npm init -y >/dev/null && npm i -s jssip@3.13.8 esbuild@0.24.0 >/dev/null 2>&1 \
  && echo 'window.JsSIP = require(\"jssip\");' > e.js && npx esbuild e.js --bundle --format=iife --minify --target=es2019" \
  > static/vendor/jssip-3.13.8.min.js
```

### 7.2 Registro
```js
const ua = new JsSIP.UA({
  sockets: [new JsSIP.WebSocketInterface('wss://sipwss.anura.dev')],
  uri: 'sip:<usuarioSIP>@<dominio>',
  authorization_user: '<usuarioSIP>',
  password: '<contraseñaSIP>',
  registrar_server: 'sip:<dominio>:55090',
  register: true, session_timers: false,
  connection_recovery_min_interval: 2, connection_recovery_max_interval: 30,
});
ua.on('registered', …); ua.on('registrationFailed', e => e.cause /* 'Authentication Error' = credenciales */);
ua.on('newRTCSession', e => { /* e.originator === 'remote' → llamada que entra a la terminal */ });
ua.start();
```
- **Audio:** `session.on('peerconnection', e => e.peerconnection.addEventListener('track', ev => audio.srcObject = ev.streams[0]))`.
- **Atender:** `session.answer({mediaConstraints: {audio: true, video: false}})`.
- **Controles:** `terminate()`, `mute/unmute({audio:true})`, `hold/unhold()`, `sendDTMF('1')`.
- **Credenciales:** guardarlas **cifradas** en el servidor y entregarlas solo al agente logueado por un `POST`
  (`Cache-Control: no-store`). Nunca escribirlas en el HTML.

### 7.3 Cuándo atender solo (clave)
Click2Dial hace que Anura **llame a la terminal** del agente: para el teléfono es una llamada que entra. Hay que
distinguirla de una entrante real:
- **Atender solo** únicamente si el CRM **está discando** una llamada de ese agente: el agente acaba de tocar Llamar
  (ventana de ~45 s) o el CRM tiene una saliente viva en estado discando/sonando (cubre el discador).
- **No confiar** en la marca de auto-atención que manda Anura (`Call-Info: answer-after` / `Alert-Info`): con el
  **Auto answer** de la cuenta activo, Anura la pone **también en las entrantes** y el teléfono las atendería solas.
- Todo lo demás: **suena** (timbre + nombre del cliente buscado por número + botón para abrir la ficha).

### 7.4 Navegar sin cortar la llamada (marco fijo)
Si cada pantalla recarga la página, se corta la llamada. Solución usada:
- Un **middleware** del servidor: si el agente tiene teléfono y la navegación es de página completa
  (encabezado `Sec-Fetch-Dest: document` + `Sec-Fetch-Mode: navigate`), devuelve el **marco**: barra del teléfono
  arriba y la pantalla pedida en un **iframe**. Dentro del iframe (`Sec-Fetch-Dest: iframe`) las pantallas se sirven
  normales. Excluir API, archivos, login, webhooks y descargas.
- El marco sincroniza la URL (`history.replaceState`) y el título con el iframe → F5 vuelve a la misma pantalla.
- `X-Frame-Options: SAMEORIGIN`. Login y logout salen del marco (`target="_top"` y salida automática del login).
- Las pantallas hablan con el teléfono por `window.top.CRMTelefono` (avisar que se va a llamar, cortar, mostrar la
  llamada en la barra).
- **Una sola pestaña registra** el teléfono: `navigator.locks.request('crm-telefono', …)` (si hay otra, espera y lo
  toma al cerrarse; también cubre el instante de una recarga).
- Aviso `beforeunload` si hay una llamada en curso. `Sec-Fetch-*` solo se envía en **HTTPS** (o localhost).

### 7.5 Controles durante la llamada
- **Espera:** la espera SIP estándar (re-INVITE `hold()`) **Anura la rechazó y cortó la llamada**. Se usa una espera
  propia: `mute()` del micrófono + silenciar el audio entrante. El cliente escucha silencio (sin música).
- **Transferir:** `session.refer('sip:<interno>@<dominio>')` (transferencia directa). Lista de agentes con interno y
  estado en vivo (`GET /api/telephony/agentes/`). Si Anura rechaza el REFER, la llamada sigue y se muestra el motivo.
  Alternativa si no la acepta: códigos de transferencia de Anura por teclado (DTMF), a pedir a Anura.
- **No disponible:** `ua.unregister()` (la cola deja de ofrecerle llamadas); al tocar Llamar, `ua.register()` y se
  espera el registro antes del Click2Dial; al terminar, se vuelve a desconectar. Al volver a Disponible, `register()`.
- **Ficha de otra cartera:** quien atendió la llamada ve la ficha por un rato (nota, tarea para la dueña, transferirle).

### 7.6 Respaldos que conviene tener
- **Entrantes atendidas en el teléfono:** el teléfono avisa al CRM al atender y al cortar (`sip-<id>`). Si Anura no
  manda el evento igual queda registrada; cuando llega, se une a la misma llamada.
- **Al cortar:** si a los 8 s Anura no avisó el END, el CRM cierra la llamada con la duración medida (si Anura avisó
  TALK, desde ese momento). El END tardío completa id real y grabación.
- **Llamada trabada:** si llega una entrante y hay una sesión terminada que no se limpió, limpiarla en vez de
  responder "ocupado".
- **Mostrar siempre algo:** si la central avisa una llamada que no llegó a la terminal, mostrarla en la barra con el
  motivo (no llegó / no conectado / falló por X).

---

## 8. Discador progresivo

- Campañas con agentes, horario, reintentos y espera entre llamadas.
- Un proceso periódico (cada ~10 s) toma el próximo contacto para cada agente **libre** (conectado, sin llamada viva,
  pasado el tiempo entre llamadas **y sin llamadas atendidas sin calificar**), y llama con Click2Dial.
- La pantalla abre la ficha sola; el teléfono del CRM atiende solo (7.3).
- Al terminar: estado del contacto en la campaña (atendió / reintentar / no contesta) según el END.
- Cerrar llamadas que quedan "discando" sin ningún aviso de Anura (~3 min) para no trabar al agente.

---

## 9. Configuración en el CRM

| Campo | Valor |
|---|---|
| Integración activa | Sí |
| Modo demostración | No (en sí simula llamadas: para capacitar y demos) |
| Token / URL de Click2Dial | El del cliente / `https://api.anura.com.ar/adapter/default/click2call` |
| Token del webhook | Generado por el CRM; va en la plantilla de Anura |
| Teléfono dentro del CRM | Activo + WebSocket `wss://sipwss.anura.dev` + dominio + puerto 55090 |
| Por agente | Interno · alias (opcional) · usuario y contraseña SIP de la terminal CRM |
| Embudo para desconocidos · rutas DID/cola → embudo | Según el cliente |
| Tarjeta en salientes nuevas · tarea en perdidas · descargar grabaciones | Sí |
| Solo llamadas propias | Si la cuenta se comparte con otras áreas |

---

## 10. Checklist de puesta en marcha

1. Token de Click2Dial cargado y verificado.
2. Plantilla de eventos + triggers START/TALK/END **BOTH** en las cuentas, todo **Activo**.
3. Interno de cada agente en el CRM. Saliente de prueba → aparece en "Últimos avisos" y en Llamadas.
4. Terminal "CRM" creada en el interno, **principal**, Auto answer **desactivado**; usuario/contraseña SIP en el CRM.
5. Recargar el CRM → barra en verde ("Teléfono listo · interno X"). Cerrar app / panel de Anura de ese interno.
6. **Saliente** desde una ficha → atiende solo, se habla por la compu, **cortar desde el CRM** → registro con duración
   y pedido de resultado de la gestión.
7. **Entrante directa al interno** → suena en la barra con nombre → Atender → registro y resultado al cortar.
8. **Entrante por menú / grupo / cola** → idem (si no suena, sumar la terminal al grupo / cola).
9. Silenciar, espera, teclado. Navegar con la llamada en curso (no se corta).
10. Discador con 2–3 números de prueba: ficha automática, atención automática, no avanza sin calificar.
11. Grabación descargada en la ficha.

---

## 11. Problemas conocidos y soluciones

| Síntoma | Causa | Solución |
|---|---|---|
| No llega ningún evento | Plantilla o triggers sin **Activo** | Tildar Activo en ambos |
| Llegan las salientes pero no las entrantes | Triggers solo en OUT | Dirección **BOTH** |
| Llega el evento pero la llamada queda sin agente / ignorada | Anura nombra al agente distinto (vacío `answer*`, usuario SIP) | Identificar por `accountExtension` y usar el **usuario SIP como alias** |
| `Originate Terminal is not registered` | La terminal principal no está conectada | Abrir el CRM (teléfono en verde) o marcar como principal la terminal que sí está conectada |
| Una entrante se atiende sola en 1 s (`TALKING`, billSeconds 1) y no deja atender | **Auto answer** de la cuenta + teléfono que confiaba en la marca de Anura, u otra terminal con auto-atención | Desactivar Auto answer; el teléfono atiende solo únicamente si el CRM está discando (7.3) |
| La llamada suena en otro lado y no en el CRM | App / panel de Anura abiertos con el mismo interno; o va a un grupo / cola sin la terminal | Cerrarlos; sumar la terminal al grupo / cola |
| Llamar a otro interno suena en el CRM | Desvío de ese interno al del agente | Normal; no es un error |
| Cortaste pero el CRM dice "en curso" | No llegó el END de Anura (otro dispositivo, demora) | Cierre automático desde el teléfono a los 8 s (7.5) |
| Barra "No se pudo registrar … usuario o contraseña" | Credenciales SIP o dominio mal | Revisar usuario de la terminal, contraseña y dominio |
| No aparece el teléfono | Falta interno del usuario / teléfono no activo / sin HTTPS | La barra debe decir qué falta; usar HTTPS |
| Al recargar dice "abierto en otra pestaña" | La página anterior no soltó el bloqueo | Esperar el bloqueo en vez de fallar (7.4) |
| "Poner en espera" corta la llamada | Anura rechaza el re-INVITE de la espera SIP | Espera propia (silenciar en ambos sentidos) |
| La cola le pasa llamadas a quien está en pausa | El teléfono sigue registrado | "No disponible" desregistra la terminal |
| Un agente atiende un cliente de otra cartera y la ficha da 404 | Solo ve lo suyo | Acceso temporal por llamada (nota, tarea para la dueña, transferir) |
| "Llamar" falla desde la oportunidad 1000 en adelante | IDs con separador de miles (`1.000`) en botones / JS | Nunca formatear IDs; separador solo para mostrar |

**Herramienta de diagnóstico imprescindible:** guardar los últimos ~500 avisos de Anura con su resultado y mostrarlos
en la configuración (fecha, evento, dirección, de → a, resultado, botón con el JSON). Responde en segundos "¿llegó el
aviso? ¿con qué nombre vino el agente? ¿por qué se ignoró?". Y en el navegador, loguear en consola las decisiones del
teléfono (`[teléfono] INVITE {auto, discando, callInfo}`, motivo de fallas).

---

## 12. Código para reutilizar (este proyecto, Django)

| Archivo | Qué hace |
|---|---|
| `app/apps/telefonia/client.py` | Click2Dial, verificar token, errores, descarga de grabaciones, cliente demo |
| `app/apps/telefonia/services.py` | `normalizar_evento`, `procesar_evento_llamada`, registro de avisos, unión con el teléfono, `discar`, discador, `PLANTILLA_WEBHOOK`, teléfono (`credenciales_softphone`, `registrar_desde_softphone`, `cerrar_desde_softphone`), `agentes_para_transferir`, `acceso_por_llamada` (ficha de otra cartera por llamada), resultado de la gestión |
| `app/apps/telefonia/views.py` | Webhook, `/api/telephony/dial`, estado, calificar, `softphone/` (credenciales), `softphone/registro/`, `softphone/corto/`, `quien/`, `agentes/` (transferencia), configuración |
| `app/apps/telefonia/models.py` | `ConfigAnura`, `InternoAnura` (interno, alias, usuario SIP, contraseña cifrada), `RutaAnura`, `Llamada`, `EventoAnura`, campañas |
| `app/core/marco.py` · `templates/telefonia/marco.html` | Marco fijo (middleware + barra + iframe) |
| `app/static/js/telefono.js` · `static/vendor/jssip-3.13.8.min.js` | Teléfono JsSIP (espera propia, transferir por REFER, disponible/no disponible, respaldo al atender/cortar) |
| `app/static/js/app.js` | Llamar, aviso de llamada, integración con el teléfono del marco |
| `app/core/cifrado.py` | Cifrado de la contraseña SIP (Fernet, clave derivada de SECRET_KEY) |
| `app/core/phone.py` | Normalización de teléfonos argentinos |
| `app/apps/telefonia/tests.py` | Tests con la plantilla real (START → TALK → END, colas, teléfono, avisos) |

Para otro proyecto: copiar cliente + `normalizar_evento` / `procesar_evento_llamada` + el teléfono (marco, JS,
endpoints) y adaptar la parte "CRM" (`_vincular_crm`, `_al_finalizar`) a los modelos de ese proyecto. Los pasos del
panel de Anura y la plantilla son los mismos.
