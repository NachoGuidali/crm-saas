# CRM Ventas — SupReg Solutions

CRM de ventas pensado para el circuito **Doctor Flex – Segmento Prospecto** (documento *Circuito_Bitrix_DoctorFlex*)
con **WhatsApp multinúmero** (Evolution, Meta Cloud API y Twilio conviviendo) y **telefonía Anura** integrada
(según *Spec_Implementacion_Anura_CRM_RAS*).

Stack: Django 5.1 · PostgreSQL 15 · Redis 7 · Celery (2 workers por cola + beat) · Gunicorn · Docker Compose.
Mismo stack que `crmsupreg-main`, así que se opera igual.

> **Modo SaaS (multi-cliente):** una sola instalación para todos los clientes, cada uno con su schema de Postgres
> y su subdominio (`acme.midominio.com`), más un panel en el dominio principal para darlos de alta, asignar planes
> y suspenderlos. Arquitectura, puesta en marcha y operación: [`docs/SAAS.md`](docs/SAAS.md).

---

## Levantarlo

```bash
cp .env.example .env          # completar claves, dominio, SMTP, ADMIN_PASSWORD
docker compose up -d --build  # web, worker, worker-masivos, beat, db, redis
docker compose --profile evolution up -d   # (opcional) Evolution API para líneas por QR
```

Al iniciar, `web` aplica las migraciones de todos los schemas (`migrate_schemas`) y corre `setup_plataforma`
(dominio principal + superusuario del panel). Los clientes se dan de alta desde `/panel/` (ver `docs/SAAS.md`).

El embudo Doctor Flex original (7 etapas, 15 tipificaciones, 4 plantillas y 4 automatizaciones **desactivadas**
hasta que Marketing apruebe los textos, 3 roles sugeridos, respuestas rápidas) se carga en un cliente con
`python manage.py tenant_command setup_inicial --schema=<schema>`.

**Datos para presentar la demo** (equipo, línea de WhatsApp simulada, Anura en modo demo, 300 prospectos con historia):

```bash
docker compose exec web python manage.py tenant_command datos_demo --schema=<schema del cliente demo>
# agentes: laura / martin / sofia / diego · supervisora: carla · contraseña: demo1234
```

En local: panel en **http://localhost:8010/panel/** y cada cliente en **http://&lt;subdominio&gt;.localhost:8010**.

---

## Cómo cubre el documento de Comercial

| Requerimiento (Circuito Doctor Flex) | Dónde está |
|---|---|
| Carga de la base del centro médico (Excel/CSV o integración) | **Importar base**: sube .xlsx/.csv, detecta las columnas solo, procesa en segundo plano con barra de progreso y detalle de errores. **API** `POST /api/v1/leads/` para integración directa con el sistema del centro médico. |
| Datos: nombre, teléfono, email, fecha de atención (+ especialidad a futuro) | Campos propios del contacto, incluido *motivo/especialidad* (la recomendación del documento). |
| Cada registro entra en *Prospecto (Nuevo)* | Etapa inicial del embudo (o la que se elija al importar). |
| Flujo de 7 etapas | Tablero kanban con arrastrar y soltar + stepper en la ficha. Etapas editables (se pueden fusionar/reordenar). |
| "Venta" = primer pago acreditado | Etapa *Venta (Ganado)* con valor de cuota; solo se llega eligiendo tipificación. |
| Tipificaciones de Venta y No Venta obligatorias | Modal obligatorio al cerrar (desde tablero, ficha o inbox). Agrupadas por categoría. Nota obligatoria configurable (ej. *condición especial*). |
| "Pide ser recontactado": no es pérdida definitiva | La tipificación **posterga**: pausa la oportunidad, agenda tarea de reactivación y la reactiva sola en la fecha. |
| "Solicita no ser contactado" / "Dato erróneo" | Marcan el contacto: se bloquean mensajes automáticos y discador. |
| 6.1 Asignación automática round robin | Por embudo: rotativa o por menor carga, entre agentes habilitados y **disponibles** (vacaciones), con **horario de atención**: fuera de horario queda en cola y se asigna al abrir. Reparto entre **todos** o **solo los conectados** (si no hay nadie: espera y se asigna apenas alguien entra, o entre todos). **Reglas por origen** (texto del origen, pauta o canal): a un vendedor, entre algunos, o sin asignar para hacerlo a mano. Notificación + tarea al agente. |
| 6.2 Mensajes automáticos por etapa | Bienvenida, seguimiento, confirmación y despedida por WhatsApp (plantilla o texto) y/o email; con demora, solo en horario, se omiten si el prospecto avanzó, está pausado o pidió no ser contactado. |
| 6.3 Recordatorio por inactividad / aviso de estancados / aviso de venta | Configurable por embudo (días); avisos a supervisión agrupados, no uno por prospecto. |
| Intentos antes de tipificar "Sin respuesta" | Contador de intentos por oportunidad; al llegar al estándar del embudo sugiere cerrar como *Sin respuesta*. |
| Permisos y visibilidad para supervisión | Roles base + roles personalizados con 16 permisos granulares; el agente ve solo lo suyo. |

## Anura

> Guía completa (configuración paso a paso, plantilla de eventos, errores, checklist y código reutilizable):
> [`docs/INTEGRACION_ANURA.md`](docs/INTEGRACION_ANURA.md)

Integración por **API y eventos** (Anura confirmó que es la vía correcta; no se usa troncal SIP).

| Pieza | Implementación |
|---|---|
| **Originar llamadas** | API **Click2Dial** de Anura: `POST https://api.anura.com.ar/adapter/default/click2call` con `Authorization: Bearer <token>` y `called`, `extension`, `customs`. Primero suena la terminal principal del agente y, al atender, se disca al cliente. El token lo genera un administrador de Anura en *Integraciones → Click 2 Dial*. |
| **Cortar** | Anura **no** permite cortar por API (el "Tenant CallManager" no está disponible): el agente corta desde su teléfono. El aviso flotante lo indica. En modo demo se simula. |
| **Eventos de llamadas** | WebHooks templetizados de Anura (*Configuración → Eventos*): un evento por trigger (START, TALK, END), dirección BOTH, `POST` JSON a `/api/integrations/anura/webhook` con `Authorization: Bearer <token del CRM>`. El cuerpo exacto (con `{{ cdrid }}`, `{{ answerextension }}`, `{{ custom1 }}`, `{{ audio_file_mp3 }}`…) está en *Telefonía Anura* para copiar. |
| **Unificación** | Cada llamada originada desde el CRM viaja con su id en `custom1`, así el evento de Anura se asocia sin ambigüedad. Idempotente por `cdrid`. |
| **Entrantes** | Número existente → se suma a su ficha. Número nuevo → contacto + tarjeta en el embudo según DID/cola; la tarjeta es **de quien atiende**; si nadie atiende, se asigna por la regla del embudo al terminar, con aviso de llamada perdida y tarea "Devolver llamada". |
| **Agente** | Se reconoce por interno o alias (extensión, terminal o usuario que informa Anura). |
| **Grabaciones** | Se descargan del link que llega en el evento END y quedan en la ficha. |
| **Discador progresivo** | Usa Click2Dial: una llamada por agente conectado y libre. |
| **API de tenant (opcional)** | CDRs de respaldo y números bloqueados, solo si Anura la habilita. |

## WhatsApp multinúmero

> Guía completa por proveedor (Evolution, Meta, Twilio): [`docs/INTEGRACION_WHATSAPP.md`](docs/INTEGRACION_WHATSAPP.md)

- Cada **línea** elige su proveedor: **Evolution** (QR, desde la pantalla), **Meta Cloud API** o **Twilio**.
  Todas conviven en el mismo inbox; cada una con su webhook secreto y sus credenciales.
- La línea define a qué embudo entran los números nuevos que escriben y qué agentes la usan.
- Desde la ficha, el agente con varias líneas elige **desde qué número escribir** (aunque ya exista chat en otra
  línea): cada línea tiene su propio chat con el cliente, siempre sobre el mismo contacto.
- Regla de 24 h en las líneas oficiales (fuera de la ventana solo plantilla aprobada). Sincronización de plantillas con Meta.
- **Ritmo anti-bloqueo** por línea en Redis: los envíos masivos/automáticos se espacian sin dejar workers dormidos.
- Inbox de 3 columnas (lista, chat y la tarjeta del CRM para mover de etapa sin salir), respuestas rápidas con `/`,
  adjuntos, estados enviado/entregado/leído, "Sin asignar" para tomar chats.

## Análisis de pautas (marketing)

*Análisis de pautas* (permiso `pautas`): Marketing crea cada pauta (ej. "Pauta Instagram") con los otros textos
con los que puede llegar ("También llega como") y carga la inversión por fecha. Cada lead guarda el **origen tal cual
llegó** y se vincula solo a la pauta que coincide (sin distinguir mayúsculas, acentos, guiones ni guiones bajos):

- **Formulario / API:** campo `origen` libre (si no es un canal del sistema), o `pauta`, `utm_campaign`, `campaign`, `ad_name`.
- **Excel:** columna "Pauta / origen de campaña" (o una pauta para toda la base).
- **Carga manual:** selector de pauta. **WhatsApp:** ID del anuncio de Meta ("clic para WhatsApp"), título del anuncio, o palabras clave / código en el primer mensaje (links `wa.me` con texto precargado). Todo opcional por pauta.
- **Pauta creada después:** al guardarla se vinculan los leads que ya habían entrado con ese origen. Los orígenes sin
  pauta se listan en el panel con un botón para crearla.

El panel muestra por pauta y período: inversión, leads, costo por lead, contactados, en curso, ventas, conversión,
costo por cliente, cuotas vendidas y meses de recupero. El detalle de cada pauta suma leads y ventas por día, dónde
están hoy los leads (por etapa y estado), cierres por tipificación, resultados por agente y el botón "Ver las N
tarjetas" (listado filtrado). Métricas de cohorte: los leads que **ingresaron** en el período; atribución a la pauta
del primer ingreso (si vuelve a entrar por otra, cuenta como "repetido" de esa pauta).

## Campos personalizados

Configuración → **Campos personalizados**: cada campo tiene nombre, tipo (texto, texto largo, número, fecha, sí/no,
lista de opciones, email, teléfono, link), obligatorio o no, y si aplica a todos o a un embudo. Se elige si se muestra
en la tarjeta del tablero, como columna en la lista y si se puede filtrar por él. Aparecen en el alta, en la ficha
(editables), en la importación de Excel (columnas marcadas con ★, se sugieren solas si el título coincide), en la API
(la clave del campo como nombre del dato, validada según el tipo), en la exportación CSV y como variable `{clave}` en
mensajes, respuestas rápidas y plantillas. Permiso propio: `campos`.

### Obligatorios por etapa

En cada etapa del embudo (o desde la ficha del campo) se eligen los datos obligatorios para pasar a esa etapa:
campos fijos (email, DNI, fecha de atención, especialidad, localidad, provincia, fecha de nacimiento, teléfono
alternativo, valor de la cuota) o campos personalizados. Rigen **desde esa etapa en adelante** (incluida Venta);
cerrar como **No venta** no exige datos. Si falta algo, al mover la tarjeta (tablero, ficha, inbox o cierre) se abre
una ventanita para completarlo y el movimiento se hace solo. Los avances automáticos no se saltean los requisitos.
También aplica a los movimientos masivos desde la lista (los que no cumplen quedan sin mover y se avisa cuáles). La importación de Excel no los exige: la base del centro médico
entra igual.

## Sin duplicados

Un solo punto de entrada (`crm.services.ingresar_prospecto`) para Excel, API, WhatsApp, Anura y carga manual:
- El teléfono se normaliza (`011 15 2345-6789`, `+54 9 11…`, `11…`, `whatsapp:+549…` → `+5491123456789`) y es **único en la base**.
- Una sola oportunidad activa por persona y embudo (**constraint en la base**, a prueba de ingresos simultáneos).
- Si reingresa: se registra el reingreso en la ficha; si ya compró no se reabre; si se perdió, se abre una nueva (configurable).
- Al cargar a mano, avisa en vivo si el teléfono ya existe.

## Usuarios

Roles base (Administrador, Supervisor, Agente) + roles personalizados con permisos granulares.
Alta por invitación (le llega un email para crear su contraseña) o con contraseña temporal (obliga a cambiarla).
**Recupero de contraseña autónomo** ("¿Olvidaste tu contraseña?", link válido 2 h, no revela si el email existe).
Bloqueo tras 5 intentos fallidos. Ingreso con usuario o email. Cada usuario tiene embudos, líneas de WhatsApp e interno de Anura.
Botón "Disponible / No disponible" para vacaciones y redistribución de cartera desde Supervisión.

## Pensado para volumen

- Índices en todos los filtros del día a día + índice **trigram** para buscar por nombre/email.
- Tablero paginado por columna, polling liviano (un solo request de 3–5 ms para contadores y llamada activa).
- Celery en colas separadas: webhooks entrantes nunca esperan detrás de una importación o un envío masivo.
- Bloqueos de fila en asignación, cambios de etapa y discador (sin carreras entre usuarios simultáneos).
- Medido con **40.000 oportunidades y 120.000 actividades**: tablero ~170 ms, reportes ~130 ms, supervisión ~50 ms,
  lista ~45 ms; 20 usuarios concurrentes navegando: mediana 120 ms.
- Purga automática de logs técnicos (90 días, configurable).

## Qué se sacó respecto de crmsupreg (a propósito)

Constructor visual de flujos, chatbot visual, bot de palabras clave, cotizaciones de obra social / grupo familiar,
campos personalizados con reglas condicionales y el motor genérico de reglas. Se reemplazaron por las
automatizaciones por etapa, que cubren exactamente lo que pide el circuito y son más simples de operar.
Los datos extra de cada importación se guardan igual (`datos_extra`) y se muestran en la ficha.

## Tests

```bash
cd app && DJANGO_SETTINGS_MODULE=config.settings.test python manage.py test apps
```
134 tests: normalización de teléfonos, deduplicación (1000 ingresos con repetidos), asignación, cierres con
tipificación, postergaciones, importación, webhooks de los 3 proveedores (firma de Meta, idempotencia),
ventana de 24 h, Anura (entrantes, idempotencia, click2call unificado, discador), automatizaciones, recupero de
contraseña, bloqueo de login, permisos, API, campos personalizados y obligatorios por etapa.

## Puntos a definir con Sistemas / Comercial (del documento)

Textos finales de los mensajes (las automatizaciones están cargadas pero desactivadas), cantidad de intentos
antes de "Sin respuesta" (hoy 5), horario de atención del equipo de ventas, franja horaria del discador, formato exacto del CallHook
y del dial de Anura, y si el embudo es exclusivo de Doctor Flex (se pueden crear más embudos).
# crm-ventas
# crm-saas
