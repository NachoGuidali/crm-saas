# Supreg CRM — Funcionalidades e integraciones

Documento de referencia con **todo lo que hace el sistema**: módulos, reglas de negocio, integraciones y cómo
funciona cada una. Para configurar paso a paso cada integración, ver las guías específicas:

| Guía | Para qué |
|---|---|
| [`INTEGRACION_ANURA.md`](INTEGRACION_ANURA.md) | Telefonía Anura: Click2Dial, eventos, errores, checklist |
| [`INTEGRACION_WHATSAPP.md`](INTEGRACION_WHATSAPP.md) | WhatsApp por Evolution (QR), Meta Cloud API y Twilio |
| [`PUESTA_EN_MARCHA.md`](PUESTA_EN_MARCHA.md) | Instalar en un servidor, HTTPS, backups, actualizar |

---

## Índice

1. [Resumen](#1-resumen)
2. [Contactos y oportunidades](#2-contactos-y-oportunidades)
3. [Ingreso de leads y deduplicación](#3-ingreso-de-leads-y-deduplicación)
4. [Embudos, etapas y tipificaciones](#4-embudos-etapas-y-tipificaciones)
5. [Asignación automática](#5-asignación-automática)
6. [Trabajo diario del vendedor](#6-trabajo-diario-del-vendedor)
7. [Historial y auditoría](#7-historial-y-auditoría)
8. [Campos personalizados](#8-campos-personalizados)
9. [Listados, filtros y acciones masivas](#9-listados-filtros-y-acciones-masivas)
10. [Importación y exportación](#10-importación-y-exportación)
11. [WhatsApp multinúmero](#11-whatsapp-multinúmero)
12. [Telefonía (Anura)](#12-telefonía-anura)
13. [Discador progresivo](#13-discador-progresivo)
14. [Automatizaciones](#14-automatizaciones)
15. [Análisis de pautas](#15-análisis-de-pautas)
16. [Reportes y supervisión](#16-reportes-y-supervisión)
17. [Usuarios, roles y permisos](#17-usuarios-roles-y-permisos)
18. [API para integraciones externas](#18-api-para-integraciones-externas)
19. [Email](#19-email)
20. [Tecnología, rendimiento y seguridad](#20-tecnología-rendimiento-y-seguridad)
21. [Mapa de pantallas](#21-mapa-de-pantallas)

---

## 1. Resumen

CRM de ventas para equipos comerciales que venden por **teléfono y WhatsApp**. Junta en un solo sistema:

- el **ingreso** de leads desde cualquier canal, sin duplicados;
- el **reparto** automático entre vendedores;
- la **gestión** en un tablero por etapas, con WhatsApp y llamadas desde la misma ficha;
- el **seguimiento** automático (mensajes, tareas, reactivaciones);
- la **medición**: conversión por etapa y vendedor, y costo real de cada venta por pauta publicitaria.

Todo se configura desde la pantalla, sin programar: embudos, etapas, motivos de cierre, reglas de reparto,
mensajes automáticos, campos propios, líneas de WhatsApp, telefonía y permisos.

---

## 2. Contactos y oportunidades

El sistema separa a **la persona** de **cada intento de venta**:

- **Contacto:** nombre, teléfono (único en todo el sistema), teléfono alternativo, email, DNI, fecha de nacimiento,
  localidad, provincia, fecha y especialidad de atención, etiquetas, marca **"No contactar"** y campos personalizados.
- **Oportunidad:** la tarjeta del tablero. Pertenece a un contacto y a un embudo; tiene etapa, estado, vendedor,
  valor (cuota mensual), origen, pauta, intentos de contacto, próxima fecha de contacto y motivo de cierre.

**Estados de una oportunidad:** abierta, pausada, ganada (venta), perdida (no venta).

**Reglas:**
- Una persona tiene **una sola oportunidad activa por embudo** (lo garantiza la base de datos, aun con ingresos
  simultáneos). Puede tener oportunidades en distintos embudos y un historial de oportunidades cerradas.
- "No contactar" bloquea mensajes automáticos, envíos masivos y el discador para esa persona.

**Ficha de la oportunidad:** datos del contacto (editables), stepper de etapas, valor, pauta, tareas, llamadas con
grabación, chat de WhatsApp embebido, historial completo, otras oportunidades de la misma persona y botones para
llamar, escribir, mover, pausar, registrar intento, agendar tarea, reasignar y cerrar.

---

## 3. Ingreso de leads y deduplicación

### Canales de ingreso

| Canal | Cómo entra |
|---|---|
| Formularios web / landings | API `POST /api/v1/leads/` (sección 18) |
| Integraciones (n8n, sistemas externos) | La misma API |
| WhatsApp | Un número nuevo que escribe a una línea crea contacto y oportunidad en el embudo de esa línea |
| Anuncios "clic para WhatsApp" de Meta | Igual que WhatsApp, y el lead queda atribuido al anuncio (pauta) |
| Llamada entrante | Un número nuevo que llama crea contacto y oportunidad (sección 12) |
| Llamada saliente desde el softphone | Opcional: si el vendedor llama a un número que no está, se crea la tarjeta |
| Excel / CSV | Importación con mapeo de columnas (sección 10) |
| Carga manual | Formulario "Nueva oportunidad", con aviso en vivo si el teléfono ya existe |

### Deduplicación

Todos los canales pasan por **un único punto de entrada**, que aplica las mismas reglas:

1. **Normaliza el teléfono** al formato internacional argentino. `011 15 2345-6789`, `+54 9 11 2345 6789`,
   `11 2345 6789` y `whatsapp:+5491123456789` terminan todos en `+5491123456789`.
2. Busca el contacto por teléfono (y por email/DNI cuando corresponde). Si existe, **completa los datos que falten**
   sin pisar los que ya tiene.
3. Decide qué pasa con la oportunidad:
   - **Ya tiene una activa en ese embudo:** no se duplica. Se registra un **reingreso** en la ficha (con el canal y
     la pauta por la que volvió).
   - **Ya compró:** no se reabre; se registra el reingreso.
   - **Se había perdido:** se abre una nueva o solo se registra, según la configuración del embudo.
   - **Pidió no ser contactado:** se registra el intento de reingreso y no se abre nada.
   - **Es nuevo:** se crea la oportunidad en la etapa inicial, se asigna y se disparan las automatizaciones.

---

## 4. Embudos, etapas y tipificaciones

- **Varios embudos** (por producto, campaña o área), cada uno con su color, agentes habilitados y supervisores.
- **Etapas** ordenables, con color y tipo: normal, ganado (venta) o perdido (no venta). Una etapa puede marcar
  "contacto efectivo" (cuenta para la métrica de contactabilidad).
- **Tipificaciones** (motivos de cierre) de venta y de no venta, agrupadas por categoría, con:
  - nota obligatoria opcional (ej. "condición especial");
  - acciones especiales: **postergar** (pausa y reactiva en una fecha), **no contactar** (bloquea al contacto),
    **dato erróneo**.
- **Cierre obligatorio con tipificación:** no se puede pasar a Venta ni a No venta sin elegir el motivo, desde
  cualquier pantalla (tablero, ficha, inbox, listado).
- **Datos obligatorios por etapa:** en cada etapa se eligen los datos que tienen que estar completos para llegar ahí
  (campos fijos o personalizados). Rigen desde esa etapa en adelante; cerrar como No venta no exige datos. Si falta
  algo, se abre una ventanita para completarlo y el movimiento se hace solo.
- **Avance automático:** la primera gestión (intento, llamada atendida o mensaje) pasa la tarjeta de la etapa
  inicial a la siguiente.
- **Máximo de intentos:** al llegar al número configurado, el sistema sugiere cerrar como "Sin respuesta".
- **Pausar / postergar:** con motivo y fecha; la tarjeta se reactiva sola en la etapa donde estaba.
- **Reabrir** oportunidades cerradas (permiso aparte).
- **Reingreso de perdidos:** por embudo, abrir una oportunidad nueva o solo registrar el reingreso.
- **Avisos del embudo:** recordatorio al vendedor por inactividad (N días), alerta a supervisión por prospectos
  estancados (N días) y aviso de ventas a supervisores.

---

## 5. Asignación automática

Se configura por embudo (*Configuración → Embudos*).

### Regla general

| Opción | Valores |
|---|---|
| Regla | **Rotativa** (round robin), **al que tiene menos prospectos abiertos**, o **manual** |
| Repartir entre | **Todos los agentes habilitados** o **solo los conectados** en ese momento |
| Si no hay nadie conectado | Esperar y asignarlo apenas alguien se conecte, o asignarlo igual entre todos |
| Horario de atención | Días y horario; fuera de horario el lead queda en cola y se asigna al abrir (o se asigna igual) |
| Tarea al asignar | Opcional: crea "Contactar a …" para el vendedor, con vencimiento en 2 h |

- **"Conectado"** = tiene el CRM abierto (en cualquier pestaña). Se desconecta a los 4 minutos de cerrarlo o al salir.
  Cuando alguien se conecta, se le reparten en el momento los leads que estaban esperando.
- **"Disponible"** es aparte: el vendedor (o un administrador) lo apaga en vacaciones o licencia, y no recibe nada.
- Dos ingresos simultáneos nunca le dan el mismo turno al mismo vendedor (bloqueo en la base).

### Reglas por origen (excepciones)

Se evalúan en orden antes de la regla general; gana la primera que coincide.

- **Condiciones** (se combinan todas las cargadas): el **origen contiene** un texto (sin distinguir mayúsculas,
  acentos ni guiones), **pauta**, y/o **canal** de ingreso.
- **Acción:**
  - **Asignar solo a estos agentes** (uno o varios; pueden no estar en el embudo), repartiendo entre todos, solo
    entre los conectados o como el embudo; y qué hacer si ninguno puede recibir (esperar, asignar igual entre
    ellos o usar la regla general).
  - **No asignar:** queda sin dueño para repartir a mano.
- En la ficha queda registrado qué regla asignó (o dejó sin asignar) cada lead.

---

## 6. Trabajo diario del vendedor

- **Tablero kanban** por embudo: columnas por etapa, arrastrar y soltar, contadores, carga paginada por columna,
  campos personalizados visibles en las tarjetas, botón de llamar en cada tarjeta.
- **Mi día:** la cola de trabajo priorizada: tareas vencidas y de hoy → leads nuevos sin gestionar → leads que se
  enfriaron. Botón "Siguiente" para ir al próximo.
- **Tareas:** llamar, WhatsApp, email, seguimiento, reactivación u otra; prioridad, vencimiento, asignación a otro usuario;
  completar (con resultado), posponer o cancelar. Aviso cuando vencen.
- **Registro de intentos:** canal (llamada, WhatsApp…) y resultado (sin respuesta, ocupado, buzón, número erróneo,
  contactado). Suma al contador de intentos.
- **Notas** con **@menciones** a otros usuarios (les llega una notificación).
- **Notificaciones internas:** asignaciones, menciones, llamadas perdidas, tareas vencidas, avisos de supervisión.
  Campana con contador y sonido.
- **Barra superior** con contadores en vivo: notificaciones, tareas de hoy, WhatsApp sin leer y aviso de llamada.
- **Buscador** global por nombre, teléfono, email o DNI.

---

## 7. Historial y auditoría

Cada ficha tiene un historial (pestaña **Actividad**) con **todo lo que pasa con la tarjeta**, quién lo hizo y
cuándo. Lo que hace el sistema solo figura como "automático".

| Tipo | Qué registra |
|---|---|
| Cambio de datos | Cada campo modificado (contacto, campos personalizados, etiquetas, valor, pauta) con **valor anterior → nuevo** |
| Etapas y cierres | Cambios de etapa, tipificación, nota de cierre, reaperturas |
| Asignaciones | Asignación automática (y por qué regla), reasignaciones, leads dejados sin asignar |
| Llamadas | Entrantes y salientes, estado, duración, agente y grabación |
| Intentos | Canal y resultado de cada intento |
| Mensajes | Inicio de chats de WhatsApp (y desde qué línea), mensajes y emails automáticos |
| Tareas | Agendadas (para quién y cuándo vence), completadas, pospuestas y canceladas |
| Pausas | Pausas, postergaciones y reactivaciones |
| Reingresos | Cada vez que la persona volvió a entrar, por qué canal y pauta |
| Sistema | Ingreso, carga al discador, etiquetas agregadas en masa |

Filtros por tipo en la ficha: Todo, Notas, Llamadas, Mensajes, Etapas, Cambios de datos, Asignaciones, Tareas y
Sistema. Además, cada oportunidad guarda el **historial de etapas** (con tiempos) y las **ejecuciones de
automatizaciones**.

---

## 8. Campos personalizados

*Configuración → Campos personalizados*. Cada campo tiene:

- **Tipo:** texto, texto largo, número, fecha, sí/no, lista de opciones, email, teléfono o link.
- **Alcance:** todos los embudos o uno.
- Obligatorio o no; visible en la tarjeta del tablero; columna en la lista; filtrable.

Aparecen en el alta, en la ficha (editables), en la importación de Excel (se sugieren solos si el título de la
columna coincide), en la API (validados según el tipo), en la exportación, en los filtros y como variable
`{clave}` en mensajes, respuestas rápidas y plantillas. Se pueden hacer obligatorios por etapa (sección 4).

Los datos extra que llegan en importaciones y que no son campos definidos se guardan igual y se muestran en la ficha.

---

## 9. Listados, filtros y acciones masivas

**Lista de oportunidades** con filtros combinables:

- embudo, etapas (varias), estados, vendedores (incluido "sin asignar"), etiquetas;
- **rango de fechas** sobre: ingreso, última actividad, cambio de etapa, próximo contacto o cierre; con atajos
  (hoy, 7 días, mes…);
- origen / canal, pauta, texto de origen, tipificación, cantidad de intentos;
- campos personalizados filtrables;
- búsqueda de texto.

Los filtros activos se muestran como chips que se quitan con un clic.

**Acciones masivas** sobre las seleccionadas o sobre **todas las que cumplen el filtro** (hasta 20.000):

| Acción | Detalle |
|---|---|
| Reasignar | A un vendedor o automático (según las reglas) |
| Repartir | En partes iguales entre los vendedores elegidos |
| Mover de etapa | Respeta los datos obligatorios; informa las que no se pudieron mover |
| Cerrar / postergar | Con tipificación, nota y fecha de recontacto |
| Pausar / reactivar | Con motivo y fecha |
| Agregar etiqueta | Queda registrado en el historial |
| Cargar al discador | En una campaña (sección 13) |
| Eliminar | Con permiso aparte |

Hasta 300 se aplican en el momento; más, en segundo plano, con aviso al terminar. Cada tarjeta pasa por las mismas
reglas que en la ficha (historial, automatizaciones, obligatorios).

**Contactos:** listado con búsqueda, filtro por etiqueta y "no contactar", oportunidades activas de cada uno.

---

## 10. Importación y exportación

**Importar base (Excel .xlsx o CSV):**

1. Se sube el archivo; el sistema **detecta las columnas** y sugiere a qué campo va cada una.
2. Se ajusta el mapeo (campos fijos, campos personalizados ★, pauta, valor) y se elige embudo, etapa, vendedor o
   reparto automático, fuente y pauta para toda la base.
3. Se procesa en segundo plano con barra de progreso: creados, actualizados, duplicados y errores fila por fila.

Aplica la misma deduplicación que el resto de los canales. Hay una **planilla modelo** para descargar. La
importación no exige los datos obligatorios por etapa, para que una base incompleta entre igual.

**Exportar:** el listado filtrado a CSV (con campos personalizados) y el ranking de vendedores. Requiere permiso.

---

## 11. WhatsApp multinúmero

### Modelo

- **Línea** = un número de WhatsApp, con su proveedor, credenciales, **embudo para números nuevos** y **agentes
  que la usan** (vacío = todos).
- **Conversación** = línea + teléfono. La misma persona escribiendo a dos líneas son dos chats, pero **un solo
  contacto**.
- Varias líneas y proveedores conviven en la misma bandeja.

### Proveedores

| Proveedor | Tipo | Cómo se conecta | Cuándo conviene |
|---|---|---|---|
| **Meta Cloud API** | Oficial | Token, Phone Number ID, WABA ID, verify token y app secret; webhook por línea | Volumen alto, plantillas aprobadas, sin riesgo de bloqueo |
| **Twilio** | Oficial (BSP) | Account SID, token y número; plantillas por Content SID | Si ya usan Twilio o quieren un intermediario |
| **Evolution API** | No oficial (QR) | Se escanea un QR desde la pantalla de la línea | Números existentes del celular, sin trámite con Meta |
| **Demo** | Simulado | Sin credenciales | Capacitación y presentaciones |

Cada línea tiene su **webhook secreto** (`/whatsapp/webhook/<proveedor>/<clave>/`). En Meta se valida la firma
(`X-Hub-Signature-256`) y el handshake de suscripción. En Evolution el CRM crea la instancia, configura el webhook
y muestra el QR.

### Bandeja (inbox)

- Tres columnas: lista de chats, conversación y la **tarjeta del CRM** para mover de etapa, tipificar o agendar sin
  salir.
- Bandejas: míos, sin asignar (para tomar), todos (con permiso); filtros por línea, estado y no leídos.
- Tomar chat, marcar resuelto, reabrir, marcar no leído, archivar.
- Estados de cada mensaje: enviado, entregado, leído, fallido.
- Adjuntos (imagen, video, audio, documento, hasta 16 MB); los recibidos se descargan y se guardan protegidos.
- **Respuestas rápidas** con `/atajo` y variables (`{primer_nombre}`, `{agente}`, campos personalizados…).

### Desde la ficha

- Chat embebido en la pestaña WhatsApp.
- **Elegir desde qué número escribir** si el vendedor tiene más de una línea, aunque ya exista un chat en otra. Una
  línea sin chat muestra el chat vacío y el primer mensaje lo inicia. Los chats de líneas que el vendedor no tiene
  habilitadas se ven en solo lectura.

### Reglas de las líneas oficiales

- **Ventana de 24 h:** fuera de las 24 h desde el último mensaje del cliente, solo se puede enviar una **plantilla
  aprobada**. El CRM lo avisa y bloquea el texto libre.
- **Plantillas:** se crean en el CRM (con variables), se envían a aprobación y se **sincroniza el estado** con Meta.
  Pueden limitarse a una línea.

### Envíos automáticos y anti-bloqueo

- Cada línea tiene un **mínimo de segundos entre envíos automáticos**. El reparto del ritmo se hace en Redis, así
  los envíos masivos se espacian sin frenar al resto del sistema.
- Los mensajes entrantes se procesan en una cola propia: nunca esperan detrás de un envío masivo.
- Idempotencia: cada mensaje se guarda una sola vez aunque el proveedor reenvíe el webhook.

### Atribución de anuncios

Los chats abiertos desde un anuncio de Meta ("clic para WhatsApp") traen el anuncio en el mensaje; el lead queda
vinculado a esa pauta (sección 15).

---

## 12. Telefonía (Anura)

Integración por **API y eventos** con Anura (telefonía en la nube). La central sigue siendo Anura; el CRM origina
las llamadas, recibe los avisos y registra todo. No usa troncal SIP.

### Cómo funciona una llamada saliente

1. El vendedor toca **Llamar** (ficha, tablero, lista, inbox o Mi día).
2. El CRM crea el registro de la llamada y llama a la API **Click2Dial** de Anura con el interno del vendedor, el
   número y su propio id (`custom1 = crm-<id>`).
3. Anura hace sonar la **terminal principal** del vendedor (softphone). Con **Auto answer** activado en la cuenta,
   atiende sola y enseguida suena el cliente.
4. Anura avisa cada paso con eventos (**START**, **TALK**, **END**); el aviso flotante del CRM pasa de "Discando" a
   "En curso" con reloj, y al terminar muestra "Atendida · duración".
5. Al terminar: se guarda duración, estado y grabación, se suma el intento y la tarjeta avanza si estaba en la
   etapa inicial.

### Llamadas entrantes

- **Número que ya existe:** la llamada se suma a su ficha; el aviso muestra el nombre mientras suena (o al
  atender, si entra por cola).
- **Número nuevo:** se crea contacto y tarjeta en el embudo que corresponde al número marcado o a la cola (rutas),
  o en el embudo por defecto. La tarjeta queda **de quien atendió**.
- **Nadie atiende:** la tarjeta se asigna por la regla del embudo, con aviso de llamada perdida y tarea "Devolver
  llamada" (30 min, prioridad alta).

### Configuración (*Telefonía Anura*)

| Pieza | Detalle |
|---|---|
| Token de Click2Dial | Lo genera un administrador de Anura; botón **Verificar token** (no llama a nadie) |
| Interno por usuario | Interno + **alias** (otros identificadores con los que Anura nombra al mismo teléfono) |
| Rutas | Número marcado (DID) o cola → embudo (y opcionalmente un vendedor fijo) |
| Eventos | Plantilla JSON lista para copiar, token del CRM (Bearer, header o en la URL) |
| Opciones | Crear tarjeta en salientes a números nuevos; tarea por llamada perdida; descargar grabaciones; **procesar solo llamadas de internos o rutas del CRM** (para cuentas compartidas) |
| Modo demostración | Simula llamadas y eventos, sin Anura |
| Botón "Ir a Anura" | Abre o enfoca la pestaña del softphone para cortar |

### Robustez

- **Idempotencia** por id de llamada: un evento repetido no duplica nada.
- La llamada del CRM se reconoce siempre por `custom1`, aunque el id de Anura cambie.
- Si Anura no avisa nada, el vendedor puede **descartar** el aviso a los 20 s y se cierra solo a los 3 min, como
  "sin confirmación de Anura" (no cuenta como intento).
- Llamadas que quedaron abiertas se cierran solas a las 2 h.
- **Grabaciones:** se descargan del link del evento de fin, con reintentos, y quedan en la ficha.
- Opcional: API de tenant de Anura (CDRs de respaldo y números bloqueados), si Anura la habilita.

### Límites de Anura

- **No se puede cortar por API:** se corta desde el softphone (o cuando corta el cliente).
- El Web Phone embebible de Anura solo hace salientes; se evaluó y se dejó para más adelante.

---

## 13. Discador progresivo

- **Campañas** con: embudo, vendedores, horario y días, máximo de intentos por contacto, minutos entre reintentos y
  pausa del vendedor entre llamadas (para tipificar).
- **Carga de contactos** desde la campaña o con la acción masiva "Cargar al discador" del listado filtrado. No carga
  repetidos ni contactos "no contactar"; respeta números bloqueados.
- El vendedor toca **Conectarme al discador**; cuando está libre, el CRM le origina la próxima llamada (Click2Dial) y
  le abre la ficha. **Pausa** y **Desconectarme** en cualquier momento.
- Reintentos automáticos a los que no atendieron; la campaña se finaliza sola cuando no queda nadie.
- Estados por contacto: pendiente, en curso, contactado, reintentar, no contesta (agotó los intentos), descartado.
  Botón para volver a encolar a los "no contesta".
- Es **progresivo** (una llamada por vendedor libre). El predictivo requeriría troncal SIP.

---

## 14. Automatizaciones

Se configuran **por etapa**, con un disparador:

| Disparador | Ejemplo |
|---|---|
| Al entrar a la etapa (con demora opcional) | Bienvenida al instante, seguimiento al día 3 y al día 7 |
| Si el cliente no responde en X tiempo | "Si no respondió en 2 días → mandar recordatorio" |
| Cuando el cliente responde (WhatsApp o llamada atendida) | "Respondió → pasar a En gestión y avisar a la vendedora" |
| Si no hay actividad durante X tiempo | "7 días sin movimiento → cerrar como Sin respuesta" |

| Acción | Detalle |
|---|---|
| Enviar WhatsApp | Texto libre (Evolution) o plantilla aprobada (Meta/Twilio), por la línea del chat o la del embudo |
| Enviar email | Asunto y cuerpo con variables |
| Crear tarea | Para el vendedor, con vencimiento en N horas |
| Notificar al vendedor | Notificación interna |
| Notificar a supervisión | A los supervisores del embudo |
| Mover a otra etapa / cerrar | A otra etapa del embudo, o a Venta / No venta con su tipificación |
| Pasar a otro embudo | **Crear** una oportunidad nueva en otro embudo (la actual queda como está; ej. Venta → embudo Clientes), **mover** la misma tarjeta a otro embudo (ej. "En verificación" → embudo Verificaciones) o **volver** al embudo del que vino (a la etapa donde estaba o a la siguiente). Se elige embudo y etapa de destino y a quién se asigna: mismo agente, regla del embudo destino o un usuario fijo |

Opciones de cada automatización:

- **Demora** (0 = inmediato; ej. 1440 = al día siguiente).
- **Solo si sigue en esa etapa** al momento de ejecutar (no manda un seguimiento si ya avanzó).
- **Solo en horario de atención** (si cae de madrugada, se posterga a la apertura).
- Nunca se ejecuta sobre contactos "no contactar" ni oportunidades pausadas o cerradas.
- Variables: nombre, primer nombre, vendedor, embudo, campos personalizados.
- Registro de cada ejecución (programada, ejecutada, omitida con motivo, fallida) en la ficha y en
  *Automatizaciones → Ejecuciones*.

**Procesos automáticos del sistema** (corren solos):

| Proceso | Frecuencia |
|---|---|
| Reparto de leads en espera (fuera de horario o sin conectados) | Cada 2 min, y al conectarse alguien |
| Ejecutar automatizaciones programadas (con demora) | Cada 1 min |
| Reactivar oportunidades pausadas cuya fecha llegó | Cada 5 min |
| Aviso de tareas vencidas | Cada 10 min |
| Recordatorio por inactividad y alerta de estancados | Cada hora |
| Discador | Cada 10 s |
| Cierre de llamadas sin eventos / colgadas | Cada 2 min |
| CDRs de respaldo de Anura (si la API de tenant está activa) | Cada 5 min |
| Estado de conexión de las líneas de WhatsApp | Cada 5 min |
| Sincronizar estado de plantillas con Meta | Cada hora |
| Purga de logs técnicos | Diario, 3:30 (90 días por defecto) |

---

## 15. Análisis de pautas

Para Marketing (permiso `pautas`): **cuánto cuesta cada lead y cada venta, por campaña**.

- **Pautas:** nombre, plataforma, embudo, fechas y los **otros textos con los que puede llegar** (utm, nombre del
  anuncio…). Un mismo texto no puede apuntar a dos pautas.
- **Inversión:** se carga por fecha (monto y nota).
- **Vinculación automática:** cada lead guarda el origen tal cual llegó y se vincula a la pauta que coincide, sin
  distinguir mayúsculas, acentos, guiones ni guiones bajos:
  - API: `origen` libre, o `pauta`, `utm_campaign`, `campaign`, `ad_name`;
  - Excel: columna de pauta, o una pauta para toda la base;
  - carga manual: selector;
  - **WhatsApp** (cada pauta elige cómo, todo opcional), en este orden:
    1. **ID del anuncio** de Meta ("clic para WhatsApp": llega solo en el primer mensaje, con Meta Cloud API,
       Twilio o Evolution);
    2. **título del anuncio**, si coincide con el nombre o con "También llega como";
    3. **palabras clave en el primer mensaje** (un código como `#IG-SEP` o una frase del mensaje precargado de un link
       `wa.me/…?text=…`), para links comunes en bio, historias o posteos. No distingue mayúsculas, acentos ni
       puntuación.
- **Pauta creada después:** al guardarla se vinculan los leads que ya habían entrado con ese origen. Los orígenes que
  no coinciden con ninguna pauta se listan con un botón para crearla.

**Panel por período (y embudo):** inversión, leads, repetidos, costo por lead, contactados, en curso, ventas,
perdidas, conversión, **costo por venta**, cuotas vendidas y meses de recupero de la inversión.

**Detalle de cada pauta:** leads y ventas por día (gráfico), dónde están hoy los leads (por etapa y estado), cierres
por tipificación, resultados por vendedor y el link a las tarjetas filtradas.

**Criterios:** métricas de **cohorte** (los leads que ingresaron en el período, aunque la venta se cierre después) y
atribución al **primer ingreso** (si vuelve por otra pauta, cuenta como "repetido" de esa pauta).

---

## 16. Reportes y supervisión

**Dashboard de reportes** (por embudo, vendedor y período: hoy, 7 días, mes o rango):

- ingresos, ventas, perdidas, conversión, contactabilidad, porcentaje gestionado;
- tiempo promedio hasta el primer contacto;
- en curso, pausadas, sin asignar, estancados, tareas vencidas;
- valor vendido;
- llamadas, llamadas perdidas y minutos hablados;
- embudo por etapa, serie diaria de ingresos y ventas;
- motivos de venta y de no venta por categoría, postergados;
- ingresos por origen;
- **ranking de vendedores** (asignados, contactados, ventas, perdidas, abiertas, conversión, llamadas, minutos),
  exportable.

**Supervisión** (en vivo):

- cada vendedor: disponible o no, **en llamada** ahora, llamadas de hoy, chats de WhatsApp;
- leads sin asignar, chats sin asignar, prospectos estancados;
- **asignar la cola** de leads sin vendedor y **redistribuir la cartera** abierta de un vendedor entre el resto
  (por ejemplo, si se va de vacaciones).

---

## 17. Usuarios, roles y permisos

- **Roles base:** Administrador (todo), Supervisor y Agente (ve solo lo suyo).
- **Roles personalizados** que definen exactamente sus permisos.
- **Permisos disponibles:**

| Grupo | Permisos |
|---|---|
| Visibilidad | Ver todo · Supervisión · Reportes |
| Marketing | Pautas |
| Gestión comercial | Reasignar · Importar · Exportar · Eliminar · Reabrir cerradas |
| Telefonía | Discador · Configurar telefonía |
| WhatsApp | Plantillas · Líneas |
| Configuración | Embudos · Campos · Automatizaciones · Integraciones |
| Administración | Usuarios |

- Por usuario: embudos donde trabaja, líneas de WhatsApp que usa, interno y alias de Anura, disponibilidad.
- **Alta** por invitación (le llega un email para crear su contraseña) o con contraseña temporal (obliga a
  cambiarla al entrar).
- **Recupero de contraseña autónomo** ("¿Olvidaste tu contraseña?"): link por email válido 2 h, sin revelar si el
  email existe.
- Ingreso con usuario o email; **bloqueo por 15 min tras 5 intentos fallidos** (por usuario e IP).
- Cada vendedor ve solo sus oportunidades, contactos y chats, salvo con "Ver todo".

---

## 18. API para integraciones externas

Para landings, formularios, n8n, Zapier o sistemas propios.

**Autenticación:** header `X-API-Key: <clave>`. Las claves se crean en *Integraciones* (se muestran una sola vez y
se guardan solo como hash), cada una con su **embudo por defecto** y **fuente**. Límite: 120 solicitudes por minuto por
clave. Cada llamada queda en un log (request, respuesta, IP).

### `POST /api/v1/leads/` — alta de lead

```json
{
  "nombre": "Ana Pérez",
  "telefono": "11 5555-1234",
  "email": "ana@mail.com",
  "embudo": "doctor-flex",
  "origen": "Pauta Instagram",
  "utm_campaign": "septiembre",
  "valor": 18500,
  "nota": "Pidió que la llamen a la tarde",
  "obra_social": "OSDE"
}
```

- Campos del contacto: `nombre`, `telefono`, `email`, `dni`, `fecha_atencion`, `especialidad_atencion`,
  `localidad`, `provincia`, `fecha_nacimiento`, `telefono_alternativo`, y cualquier **campo personalizado** por su
  clave (validado según el tipo).
- `embudo`: id o slug (si no, el de la clave).
- `origen`: canal del sistema (`web`, `api`…) o texto libre de pauta. También `pauta`, `utm_campaign`,
  `campaign`, `ad_name`.
- Aplica deduplicación, asignación y automatizaciones.
- Respuesta: contacto, oportunidad, si fue creada o no (y el motivo: ya activa, ya cliente…), etapa, vendedor asignado
  y pauta vinculada.

### `GET /api/v1/leads/buscar/?telefono=…` (o `email`, `dni`)

Devuelve el contacto y sus oportunidades (embudo, etapa, estado, vendedor), o 404.

### `POST /api/v1/whatsapp/enviar/`

`telefono`, `mensaje` o `plantilla` (por nombre), y `linea` opcional. Envía por la línea indicada o la que
corresponde al contacto, respetando la ventana de 24 h y el ritmo anti-bloqueo.

### Webhooks que recibe el CRM

| Origen | URL |
|---|---|
| WhatsApp (Meta, Twilio, Evolution) | `/whatsapp/webhook/<proveedor>/<clave-de-la-línea>/` |
| Anura (eventos de llamadas) | `/api/integrations/anura/webhook` (token por Bearer, header o `?token=`) |

---

## 19. Email

- Por SMTP (ej. Gmail con contraseña de aplicación).
- Se usa para: invitaciones y recupero de contraseña, y emails automáticos a los prospectos (automatizaciones).
- Sin SMTP configurado, los emails se escriben en el log.

---

## 20. Tecnología, rendimiento y seguridad

**Stack:** Python / Django 5, PostgreSQL 15, Redis 7, Celery (colas separadas para entrantes, salientes y
masivos, más tareas programadas), Gunicorn, Docker Compose. Se instala en cualquier servidor Linux con Docker, detrás
de nginx con HTTPS.

**Rendimiento** (medido con 40.000 oportunidades y 120.000 actividades): tablero ~170 ms, reportes ~130 ms,
supervisión ~50 ms, lista ~45 ms; 20 usuarios simultáneos navegando, mediana 120 ms.

- Índices en todos los filtros de uso diario y búsqueda por similitud de texto (trigram) en nombres y emails.
- Un único pedido liviano cada 4–15 s actualiza contadores, llamada en curso y presencia.
- Bloqueos de fila en asignación, cambios de etapa y discador: sin carreras entre usuarios simultáneos.
- Los webhooks responden al instante y se procesan en segundo plano.

**Seguridad:**

- HTTPS, cookies seguras, protección CSRF, cabeceras de seguridad.
- Webhooks con clave secreta por línea / token; firma de Meta validada.
- API keys guardadas como hash, con límite de solicitudes y log.
- Archivos (adjuntos, grabaciones) servidos solo a usuarios autenticados.
- Bloqueo de login por intentos, recupero por email, contraseñas temporales obligatorias de cambiar.
- Permisos granulares y visibilidad por vendedor.
- Backups diarios de la base y los archivos (script incluido).

**Calidad:** más de 100 tests automáticos (teléfonos, deduplicación con ingresos simultáneos, asignación, reglas,
cierres, importación, webhooks de los tres proveedores, ventana de 24 h, Anura, discador, automatizaciones,
permisos, API, campos personalizados, historial de cambios).

---

## 21. Mapa de pantallas

| Menú | Pantalla | Para qué |
|---|---|---|
| Trabajo | Mi día | Cola priorizada del vendedor |
| | Tablero | Kanban por embudo |
| | Oportunidades | Lista con filtros y acciones masivas |
| | Contactos | Personas y sus oportunidades |
| | Tareas | Tareas propias (o de todos) |
| | WhatsApp | Bandeja multinúmero |
| | Llamadas | Registro de llamadas con grabaciones |
| | Discador | Campañas de discado |
| Análisis | Dashboard | Reportes, métricas y ranking |
| | Supervisión | Estado del equipo en vivo |
| | Análisis de pautas | Costo por lead y por venta |
| Datos | Importar base | Excel / CSV |
| Configuración | Embudos y tipificaciones | Etapas, motivos, reglas de asignación, obligatorios |
| | Campos personalizados | Datos propios del negocio |
| | Automatizaciones | Acciones por etapa y ejecuciones |
| | Plantillas WA / Respuestas rápidas | Mensajes |
| | Líneas WhatsApp | Números, proveedores, QR |
| | Telefonía Anura | Token, internos, rutas, eventos |
| | Usuarios / Roles y permisos | Accesos y permisos |
| | Integraciones API | API keys y logs |

---

## 22. Agregado en octubre 2026 (checklist de requerimientos)

Detalle punto por punto en [`CHECKLIST_ROISA.md`](CHECKLIST_ROISA.md).

| Función | Dónde | Qué hace |
|---|---|---|
| Leads únicos | Dashboard | Personas distintas del período: nuevas + las que reingresaron |
| Ingresó N veces | Ficha, lista (×N), filtro | Primer ingreso + reingresos de cada oportunidad |
| Conversión por etapa | Dashboard, Mis números | De la cohorte del período, cuántos llegaron a cada etapa y % de paso |
| Pipeline vendedora × etapa | Dashboard, Mis números | Lo que cada vendedora tiene en curso en cada etapa |
| Actividad por vendedora | Dashboard (CSV) | WhatsApp y plantillas, automáticos, emails, SMS, llamadas, minutos, intentos, notas, etapas, tareas |
| Mis números | Menú | Panel personal de la vendedora (el supervisor elige a cualquiera) |
| Refresco automático | Dashboard, Supervisión, Mis números | Cada minuto |
| SLA de primer contacto | Configuración del embudo | Minutos máximos; avisar o reasignar (con tope); reporte, filtro y alerta |
| Disparadores de automatización | Automatizaciones | Al entrar · si no responde en X · cuando responde · sin actividad X |
| Mover de etapa / cerrar | Automatizaciones | Acción automática, con tipificación si cierra |
| Calidad de envíos | Menú | Entregados, leídos, respondidos y avance por automatización y plantilla; aperturas y clics de emails |
| Call center | Dashboard, Mis números | Tiempo conectado, habla, promedio, after call work, ocupación |
| Email manual | Ficha | Con seguimiento de apertura y clics; respuestas al email de la vendedora |
| SMS | Ficha, automatizaciones, Integraciones | Twilio: envío, respuestas al CRM (dispara "cuando responde") |
| Puntaje de leads | Configuración | Reglas que suman/restan; se ve en tarjeta y lista, ordena Mi día |
| Proyección | Dashboard | Ritmo del mes y ventas esperadas del pipeline |

## 23. Email y difusiones

| Función | Dónde | Qué hace |
|---|---|---|
| Plantillas de email | Configuración → Plantillas de email | Asunto y texto con variables; se usan en la ficha, automatizaciones y difusiones |
| Configuración de email | Configuración → Configuración de email | Servidor SMTP (Gmail, Brevo, SES, Mailgun), remitente, emails por minuto, botón de prueba. Apagado = usa el `.env` |
| Email desde la ficha | Botón "Email" | Con o sin plantilla; respuestas al email de la vendedora; seguimiento de apertura y clics |
| Email automático | Automatizaciones → "Enviar email" | Plantilla de email o asunto y texto |
| Secuencias | Automatizaciones → Cuándo: "Después de otra automatización" | Ej.: Bienvenida → 3 días → Recordatorio → 4 días → Último aviso |
| Difusiones | Oportunidades (filtrar y seleccionar) → acción "Enviar difusión"; menú Difusiones | Por email o WhatsApp, ahora o programada, de a tandas; resultados por difusión (enviados, abiertos/clics o leídos/respondidos, bajas, omitidos) |
| Baja de suscripción | Link al pie de difusiones y emails automáticos (+ encabezado List-Unsubscribe) | Marca al contacto "no recibir emails": no recibe más difusiones ni emails automáticos (WhatsApp y llamadas siguen) |

Permiso nuevo: **Difusiones** (incluido en Supervisor y en el rol Comercial / Marketing).

## 24. Gestión de call center y cartera (pedidos de la reunión del 09/10/2026)

| Función | Dónde | Cómo funciona |
|---|---|---|
| Resultado de la gestión obligatorio | Al cortar cada llamada atendida (cualquier pantalla) | Ventana que no se puede cerrar sin elegir el resultado; algunos piden fecha de rellamado (crea la tarea) o mueven la tarjeta. El discador no pasa la próxima llamada hasta calificar. Opciones en Embudo → Resultados de gestión; se desactiva por embudo |
| Llamadas sin calificar | Supervisión (KPI y columna por agente), Llamadas → filtro Resultado, Oportunidades → "Con llamadas sin calificar", Reportes → Resultado de las llamadas | Para ver en masa qué gestiones quedaron sin tipificar |
| ID de grabación | Llamadas, ficha → Llamadas, exportación CSV | Es el ID de Anura (cdrid); el buscador de Llamadas lo acepta |
| Exportar llamadas | Llamadas → CSV | Respeta los filtros; incluye resultado, nota e ID de grabación |
| Reproceso (reasignar) | Automatizaciones → Qué hacer: "Reasignar a otro vendedor" | Nunca a la misma vendedora; reparto parejo (menos carga) o según el embudo; opcional volver a una etapa (ej. Nuevo). Con "Si no responde en 2 días" también toma la base que ya estaba en la etapa |
| Varias etapas o todas | Automatizaciones → Cuándo | "También en estas etapas" o "En todas las etapas abiertas" |
| Cuando el lead reingresa | Automatizaciones → Cuándo | Se dispara cuando la persona vuelve a entrar y no se duplicó (máx. 1 vez por hora) |
| Prioridad 🔥 | Acción de automatización, botón en la ficha, acción masiva | Primero en Mi día y en el tablero, sube en el discador, filtro "Prioridad". Se quita al calificar la llamada, escribirle o moverla de etapa |
| Etiqueta automática | Acción de automatización "Poner una etiqueta"; Embudo → Etiqueta al cerrar como venta | Ej.: "Socio" al ganar |
| Condiciones por campaña y etiqueta | Automatizaciones → Condiciones → "Solo para ciertas campañas o etiquetas" | Solo pautas X, solo con etiqueta Y, nunca con etiqueta Z |
| Vencimiento de la ficha | Embudo → Vencimiento de la ficha | N días con la misma vendedora sin cerrarse → reasignar (parejo), dejar sin asignar o solo avisar. "No vence desde la etapa…" (ej. Preventa). Fuera de horario no actúa |
| Clientes (socios) | Embudo → Clientes (socios) | Si el número ya tiene una venta (en cualquier embudo) no se abre tarjeta ni se reparte; su WhatsApp y sus llamadas perdidas van al vendedor de la venta o a un usuario de postventa. Marcar "Abrir tarjeta aunque ya sea cliente" en embudos de upgrade |
| Tarea masiva | Oportunidades → seleccionar → "Crear tarea" | Para el vendedor de cada una (o para mí); queda en Mi día |
| Campos de tipo archivo | Configuración → Campos (tipo Archivo); ficha → Documentos; clip 📎 en los adjuntos de WhatsApp | Varios archivos por campo (ej. recibos de sueldo); pueden ser obligatorios para pasar de etapa (se suben en la ventanita) |
| Gestión por pauta / base | Análisis de pautas → Gestión por pauta; Importaciones → detalle de la base | Llamadas, atendidas, minutos, llamadas por lead, WhatsApp y emails de cada campaña o base |
| Filtros del tablero | Tablero | Pauta, base importada y prioridad |
| API con etapa y vendedor | `POST /api/v1/leads/` | `etapa` (nombre o id) y `agente` (usuario o email); si el lead ya existe lo mueve de etapa (`movida`) |
