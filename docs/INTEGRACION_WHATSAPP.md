# Integración con WhatsApp (multinúmero, multiproveedor)

Guía de referencia para conectar líneas de WhatsApp al CRM. Cada **línea** (número) elige su proveedor y tiene sus
propias credenciales y su propio webhook; todas conviven en el mismo inbox. Sirve para este proyecto y como base para otros.

> **Estado:** implementado; probado con tests que simulan los webhooks de los tres proveedores (incluida la firma de
> Meta) y con la línea "Simulada". **Falta probar contra cuentas reales** de Meta y Twilio en este proyecto; Evolution
> es el mismo esquema que ya funciona en los conectores anteriores (`conector-multi`, `crmsupreg`).

---

## 1. Qué proveedor usar

| Proveedor | Cuándo | Costo por mensaje | Regla de 24 h | Plantillas |
|---|---|---|---|---|
| **Evolution API** (QR) | Arrancar rápido, sin trámites con Meta. WhatsApp común o Business. | No | No | Se envían como texto |
| **Meta Cloud API** (oficial) | Operación a escala con cuenta verificada | Sí (Meta cobra por conversación) | Sí | Aprobadas por Meta |
| **Twilio** (oficial vía BSP) | Oficial mientras se tramita Meta | Sí (Twilio + Meta) | Sí | Content Templates (ContentSid `HX…`) |
| **Simulado** | Demos y capacitación | — | — | — |

**Regla de 24 h (Meta y Twilio):** solo se puede escribir texto libre dentro de las 24 h desde el último mensaje del
cliente; fuera de esa ventana, solo **plantillas aprobadas**. El CRM lo controla y avisa en el chat.

---

## 2. Conceptos del CRM

- **Línea** (*Configuración → Líneas WhatsApp*): nombre, proveedor, número, credenciales, **embudo para contactos
  nuevos** (si escribe alguien que no está en el CRM se crea la oportunidad ahí; vacío = solo contacto y chat),
  **agentes que la usan** (vacío = todos) y **segundos mínimos entre envíos automáticos** (anti-bloqueo).
- **Webhook por línea:** `https://<dominio>/whatsapp/webhook/<proveedor>/<clave-secreta>/`. La clave es única por
  línea y la identifica; la URL exacta se ve en la configuración de la línea.
- **Conversación** = línea + teléfono. El mismo cliente escribiendo a dos líneas son dos chats pero **un solo contacto**.
- **Deduplicación:** el teléfono se normaliza (`+549…`), así WhatsApp, Anura, Excel y API terminan en el mismo contacto.
- **Idempotencia:** cada mensaje se guarda una sola vez por su ID del proveedor (los proveedores reenvían webhooks).
- **Elegir desde qué número escribir (ficha):** en la pestaña WhatsApp de la ficha, si el agente tiene más de una
  línea habilitada, aparece *Escribir desde [línea ▾]* con todas sus líneas (las que ya tienen chat con esa persona
  muestran la fecha del último mensaje; las otras, "sin chat todavía"). Elegir una sin chat muestra el chat vacío y el
  primer envío abre la conversación en esa línea. En una línea oficial sin conversación reciente solo se puede iniciar
  con plantilla aprobada. Los chats en líneas que el agente no tiene habilitadas se ven, pero en solo lectura.
  Por defecto se abre el chat más reciente (o la línea sugerida: la del embudo / la última usada).
- **Asignación:** el chat queda con el agente dueño de la oportunidad; si no hay, cae en "Sin asignar" para que lo tome
  alguien de esa línea.

---

## 3. Evolution API (QR)

**Requisitos:** un servidor de Evolution API v2 (el `docker-compose.yml` lo trae como perfil opcional:
`docker compose --profile evolution up -d`). Variables globales en `.env`: `EVOLUTION_API_URL`, `EVOLUTION_API_KEY`
(cada línea puede pisarlas).

**Pasos:**
1. *Líneas WhatsApp → Conectar número* → proveedor **Evolution**. El nombre de instancia se genera solo
   (`crm-linea-<id>`) si se deja vacío.
2. Guardar → en la misma pantalla, **Mostrar QR** → en el teléfono: *WhatsApp → Dispositivos vinculados → Vincular
   dispositivo* → escanear.
3. El CRM crea la instancia si no existe y configura el webhook en Evolution (botón *Configurarlo en Evolution* para
   repetirlo). Eventos: `MESSAGES_UPSERT`, `MESSAGES_UPDATE`, `CONNECTION_UPDATE`, `QRCODE_UPDATED`.

**Notas:**
- Se ignoran grupos y estados; se resuelven los chats con identificador `@lid` usando el teléfono real que manda Evolution.
- La media entrante se descarga desencriptada (`getBase64FromMediaMessage`); la saliente se manda en base64 (no necesita URL pública).
- **Anti-bloqueo:** para envíos automáticos/masivos usar 6–15 s entre mensajes (campo de la línea).

---

## 4. Meta Cloud API (oficial)

**Qué se necesita de Meta Business:**
- **Phone Number ID** y **WhatsApp Business Account ID (WABA)** (*Meta for Developers → app → WhatsApp → Configuración de la API*).
- **Access token permanente**: crear un *System User* en Business Manager y generarle un token con permisos
  `whatsapp_business_messaging` y `whatsapp_business_management` (el token temporal de 24 h no sirve para producción).
- **App secret** (*Configuración → Básica* de la app) → el CRM valida la firma `X-Hub-Signature-256` de cada webhook.
- Un **verify token** inventado por uno (se carga igual en el CRM y en Meta).

**Pasos:**
1. *Conectar número* → proveedor **Meta** → cargar los datos anteriores → guardar (el estado debe quedar "Conectada").
2. En Meta: *WhatsApp → Configuración → Webhook* → URL del webhook de la línea + el mismo verify token → Meta hace el
   handshake (GET) y el CRM lo responde.
3. Suscribirse al campo **`messages`**.
4. Plantillas: crearlas en el CRM (*Plantillas WA*) y enviarlas a revisión desde ahí, o crearlas en Meta con el mismo
   nombre técnico. El estado de aprobación se **sincroniza cada hora** (o botón *Sincronizar plantillas desde Meta*).

**Notas:** la media saliente se envía por link, así que el CRM tiene que estar publicado con `SITE_URL` correcto (los
adjuntos salientes quedan en una ruta pública con nombre aleatorio; el resto de la media exige sesión). Sugerido 1 s
entre envíos automáticos.

---

## 5. Twilio (oficial vía BSP)

**Qué se necesita:** **Account SID**, **Auth Token** y el **número remitente** habilitado para WhatsApp en Twilio.

**Pasos:**
1. *Conectar número* → proveedor **Twilio** → cargar los tres datos → guardar.
2. En Twilio, en el número/sender de WhatsApp: *"A message comes in"* → URL del webhook de la línea, método **POST**.
   Los estados de entrega (enviado/entregado/leído) llegan solos: el CRM manda `StatusCallback` en cada mensaje.
3. Plantillas: crearlas en el *Content Template Builder* de Twilio y pegar su **ContentSid** (`HX…`) en la plantilla
   del CRM (con las variables en el mismo orden).

**Notas:** el CRM valida la firma `X-Twilio-Signature` con el Auth Token; para que coincida, `SITE_URL` tiene que ser
exactamente la URL pública (con `https`). Sin Auth Token cargado no valida.

---

## 6. Plantillas y variables

- En el cuerpo se usan `{{1}}`, `{{2}}`… y para cada una se elige de qué dato sale: nombre, primer nombre, teléfono,
  email, embudo, etapa, agente, fecha de atención o **cualquier campo personalizado**.
- En textos libres (automatizaciones, respuestas rápidas) se usan llaves simples: `{primer_nombre}`, `{agente}`,
  `{embudo}`, `{<clave_de_campo_personalizado>}`.
- En Evolution la plantilla se envía como texto; en Meta tiene que estar **aprobada**; en Twilio necesita **ContentSid**.

---

## 7. Envíos automáticos y masivos

- Las automatizaciones por etapa (bienvenida, seguimiento, confirmación, despedida) usan la línea del chat existente,
  la del embudo o la configurada en la acción.
- **Nunca** se envía a contactos "no contactar", con teléfono inválido o con la oportunidad pausada.
- En líneas oficiales, si la ventana de 24 h está cerrada y la acción no tiene plantilla, **no se envía** (queda
  registrado como "omitida" con el motivo).
- **Ritmo por línea:** cada envío automático reserva un turno en Redis según los segundos de la línea; los mensajes se
  programan con demora en lugar de dejar procesos esperando.

---

## 8. Checklist de puesta en marcha (por línea)

1. Crear la línea y ver estado **Conectada**.
2. Escribirle desde un celular que **no** esté en el CRM → aparece en el inbox; se crea contacto (+ oportunidad si la
   línea tiene embudo) y se asigna.
3. Responder desde el inbox → llega al celular; el estado pasa a enviado / entregado / leído.
4. Mandar una imagen y un audio en ambos sentidos.
5. (Oficiales) Esperar >24 h o usar un número sin conversación → el CRM exige plantilla; enviar una aprobada.
6. Activar una automatización de prueba y moverle una tarjeta → sale el mensaje automático respetando el ritmo.

---

## 9. Dónde está el código (para reutilizar)

| Archivo | Qué hace |
|---|---|
| `app/apps/whatsapp/proveedores/base.py` | Interfaz común, `MensajeEntrante`, log de llamadas HTTP, guardado de media |
| `app/apps/whatsapp/proveedores/evolution.py` | Envío, QR, instancia, webhook, parser (incluye `@lid`), descarga de media |
| `app/apps/whatsapp/proveedores/meta.py` | Envío, plantillas (crear / sincronizar), handshake y firma del webhook, parser, media |
| `app/apps/whatsapp/proveedores/twilio.py` | Envío, ContentSid, firma del webhook, parser, media |
| `app/apps/whatsapp/proveedores/demo.py` | Línea simulada (responde sola a veces) |
| `app/apps/whatsapp/services.py` | Procesamiento de entrantes (dedupe, CRM, asignación), envío con regla de 24 h, envío automático, ritmo por línea (Lua en Redis) |
| `app/apps/whatsapp/views.py` | Webhook por línea, inbox, envío, líneas (QR / estado), plantillas |
| `app/apps/whatsapp/tests.py` | Tests de los tres webhooks, idempotencia, ventana de 24 h, ritmo |

Para otro proyecto: copiar la carpeta `proveedores/` + `services.py` y adaptar la parte CRM de
`procesar_mensaje_entrante` (creación de contacto/oportunidad) a los modelos de ese proyecto.
