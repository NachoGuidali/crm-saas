# Guía de la demo — menú por menú

Para la reunión de presentación. Primero, cómo prepararla y un recorrido sugerido; después, **cada opción del menú**:
qué es, qué mostrar y para quién sirve.

---

## Antes de la reunión

1. **Datos de demo** (solo en un servidor de prueba, nunca en el que va a usar Roisa en serio):
   `docker compose exec web python manage.py datos_demo` → 300 prospectos con historia, equipo de ventas,
   línea de WhatsApp simulada y Anura en modo demo.
   Usuarios: agentes `laura` / `martin` / `sofia` / `diego`, supervisora `carla`, contraseña `demo1234`.
2. **Dos ventanas**: una como **admin o supervisora** (todo) y otra, en ventana privada, como **agente** (`laura`):
   así se ve que cada vendedora ve solo lo suyo.
3. **Telefonía**: si vas a llamar en vivo, tené el Anura web abierto con el interno de prueba y Auto answer activado.
   Si no, el **modo demo** simula las llamadas.
4. **WhatsApp**: con la línea demo, si escribís desde la ficha el "cliente" contesta solo a los pocos segundos.

## Recorrido sugerido (30–40 min)

| # | Momento | Qué mostrar | Mensaje |
|---|---|---|---|
| 1 | El lead entra | Cargar uno a mano (Oportunidades → Nueva) o por API; mostrar que se asigna solo y le llega el aviso a la vendedora | "Ningún lead queda sin dueño" |
| 2 | Duplicados | Cargar el mismo teléfono escrito distinto (`11 15…` vs `+54 9 11…`): no se duplica, se registra el reingreso | "Una persona, un historial" |
| 3 | El día de la vendedora | Entrar como `laura` → **Mi día** → Siguiente → ficha | "Sabe a quién llamar ahora" |
| 4 | Gestión en la ficha | Llamar (aviso flotante), WhatsApp desde la ficha eligiendo el número, nota con @mención, mover de etapa (pide datos obligatorios), cerrar con tipificación | "Todo desde una pantalla" |
| 5 | Tablero | Arrastrar tarjetas, cerrar como Venta / No venta, columnas de cierre | "El embudo a la vista" |
| 6 | Automatizaciones | Mostrar una en pasos: "Si no responde en 2 días → WhatsApp de seguimiento"; secuencia Bienvenida → Recordatorio | "El seguimiento se hace solo" |
| 7 | Supervisión | Lista con filtros (etapa + fechas) → seleccionar todos → reasignar / repartir / difusión | "Control del equipo en un clic" |
| 8 | Números | Dashboard (conversión por etapa, pipeline por vendedora, actividad, telefonía), SLA, Mis números | "Qué pasa y quién lo hace" |
| 9 | Marketing | Análisis de pautas (costo por lead y por venta), Calidad de envíos, Difusiones | "Cuánto cuesta cada venta" |
| 10 | Cierre | Roles y permisos, historial con quién cambió qué, seguridad | "Ordenado y auditable" |

---

## Barra superior (siempre visible)

- **Buscador**: por nombre, teléfono, email o DNI, desde cualquier pantalla.
- **Disponible / No disponible**: la vendedora se saca de la rueda de asignación (vacaciones, licencia).
- **Campana**: notificaciones (lead asignado, mención, llamada perdida, tarea vencida, SLA, venta). Suena.
- **Contadores**: tareas de hoy y WhatsApp sin leer.
- **Aviso de llamada flotante**: aparece al llamar o cuando entra una llamada; muestra con quién, el reloj y "Ir a Anura".
- **Mi perfil**: datos, contraseña, notificaciones. "¿Olvidaste tu contraseña?" en el login: se recupera sola por email.

## La ficha de la oportunidad (el corazón)

- **Encabezado**: nombre, teléfono, etapa (stepper clickeable), embudo, origen y pauta, vendedora.
- **Botones**: Llamar · WhatsApp · Email · SMS · Intento sin éxito (no atiende / ocupado / buzón) · Pausar / postergar ·
  Agendar tarea · Reasignar · Cerrar (Venta / No venta con tipificación).
- **Datos del contacto**: editables; campos personalizados; "Ingresó N veces"; intentos; valor de la cuota.
- **Pestaña Actividad**: todo lo que pasó, con quién y cuándo — llamadas con grabación, mensajes, cambios de etapa,
  **cambios de datos con valor anterior → nuevo**, tareas, asignaciones. Filtros por tipo. Notas con @menciones.
- **Pestaña WhatsApp**: el chat embebido; si la vendedora tiene varias líneas, elige **desde qué número escribir**.
- **Tareas pendientes**, **llamadas**, **otras oportunidades** de la misma persona.

---

## Trabajo diario

### Mi día
- **Qué es**: la cola de trabajo de la vendedora, priorizada: 1) tareas vencidas y de hoy, 2) leads nuevos sin
  gestionar, 3) leads que se enfriaron. Dentro de cada grupo, primero los de **mayor puntaje**.
- **Mostrar**: botón **"Siguiente"** → abre el próximo lead a trabajar.
- **Para**: vendedoras. "No pierde tiempo pensando a quién llamar".

### Mis números
- **Qué es**: el panel personal: leads recibidos, **sin contactar todavía**, contactabilidad, ventas, conversión,
  llamadas y minutos, tareas vencidas, SLA, conversión por etapa, su pipeline, su actividad, **tiempo conectada,
  tiempo de habla y after call work**.
- **Mostrar**: como `laura`; y como supervisora, eligiendo otra vendedora del selector.
- **Para**: vendedoras (autogestión) y supervisión (seguimiento individual).

### Tablero
- **Qué es**: kanban por embudo. Columnas por etapa, arrastrar y soltar, contador por columna, "Ver más".
  Columnas **Venta / No venta** muestran los cerrados de los últimos 30 días; soltar ahí pide la tipificación.
- **Tarjeta**: nombre, teléfono, botones llamar y WhatsApp, días en la etapa, intentos, puntaje ★, marcas (nuevo,
  pausado, tareas vencidas, no contactar), campos personalizados elegidos.
- **Filtros**: vendedora, origen, pausados, etc.
- **Mostrar**: arrastrar a una etapa con dato obligatorio → aparece la ventanita para completarlo.

### Oportunidades
- **Qué es**: la lista con **filtros combinables**: embudo, etapas, estados, vendedoras (o sin asignar), etiquetas,
  rango de fechas (ingreso, última actividad, cambio de etapa, próximo contacto, cierre) con atajos, origen, pauta,
  tipificación, intentos, **veces que ingresó**, **puntaje**, **SLA vencido**, campos personalizados, búsqueda.
  Chips con los filtros activos. Orden por columnas. Exportar a CSV.
- **Acciones masivas** (algunas o **todas las que cumplen los filtros**): reasignar, repartir entre varias, mover de
  etapa, cerrar / postergar, pausar, reactivar, etiquetar, cargar al discador, **enviar difusión**, eliminar.
- **Mostrar**: "todos los de *Recontactar* de septiembre → repartir entre Laura y Martín". Es lo que pidieron.

### Contactos
- **Qué es**: las personas (no las oportunidades): búsqueda, etiquetas, "no contactar", oportunidades activas de cada una.
  Ficha del contacto con todo su historial y botón para abrirle una oportunidad en otro embudo.

### Tareas
- **Qué es**: tareas propias (o de todo el equipo con permiso): llamar, WhatsApp, email, seguimiento, reactivación.
  Prioridad, vencimiento, completar con resultado, posponer, cancelar. Avisan al vencer.

### WhatsApp
- **Qué es**: bandeja **multinúmero** (todas las líneas, oficiales y por QR, juntas). Tres columnas: chats, conversación
  y **la tarjeta del CRM al costado** (mover de etapa, tipificar, agendar sin salir).
- Bandejas: míos / sin asignar (tomar) / todos. Filtro por línea y no leídos. Adjuntos, audios, estados
  enviado / entregado / leído. **Respuestas rápidas** con `/`. Plantillas aprobadas fuera de las 24 h.
- **Mostrar**: chat de un lead que vino de un anuncio → la tarjeta ya tiene la pauta.

### Llamadas
- **Qué es**: el registro de todas las llamadas: entrantes y salientes, estado, duración, vendedora, **grabación** para
  escuchar, ficha vinculada.

### Discador
- **Qué es**: campañas de llamado **progresivo**: lista de leads (cargada desde Oportunidades), vendedoras, horario,
  intentos máximos, minutos entre reintentos, pausa entre llamadas. La vendedora se "conecta" y el CRM le va pasando
  la próxima llamada sola, con la ficha abierta. Pausa, desconectarse, reintentos automáticos, avance de la campaña.

## Gestión

### Dashboard
- **Indicadores** del período (hoy, 7 días, mes, rango) y embudo: **leads únicos** (nuevos + reingresos),
  contactabilidad, ventas y valor, conversión, tiempo a contacto, llamadas y minutos; en curso, sin asignar,
  estancados, tareas vencidas.
- **Proyección**: ventas del mes, cuántas serían al ritmo actual, y **ventas esperadas del pipeline**.
- **SLA**: fuera de SLA ahora, % contactados a tiempo, promedio a la primera gestión.
- **Gráficos**: ingresos y ventas por día; **conversión por etapa** (cuántos llegan a cada etapa y % de paso);
  **pipeline vendedora × etapa**; motivos de venta y de no venta; origen de los ingresos.
- **Tablas**: ranking de vendedoras (CSV); **actividad por vendedora** (WhatsApp, plantillas, automáticos, emails, SMS,
  llamadas, minutos, intentos, notas, cambios de etapa, tareas — CSV); **telefonía por vendedora** (tiempo
  conectada, habla, promedio, after call work, ocupación).
- Se **actualiza solo** cada minuto.

### Difusiones
- **Qué es**: envíos masivos por **email o WhatsApp** a un grupo armado con los filtros de Oportunidades.
  Borrador → elegir plantilla → aviso de cuántos se omiten → **enviar ahora o programar** → sale de a tandas (cuida
  el número y el servidor de correo) → se puede frenar.
- **Resultados** por difusión: enviados, abiertos / clics o entregados / leídos / respondidos, **bajas**, omitidos;
  y destinatario por destinatario.
- Los emails llevan **link de baja**; quien se da de baja no recibe más emails masivos.

### Calidad de envíos
- **Qué es**: qué mensajes funcionan. Por **automatización** y por **plantilla**: enviados, % entregados, % leídos,
  **% que respondió** (24/48/72 h o una semana) y % cuya tarjeta **avanzó de etapa**. Emails: abiertos y clics.
- **Mostrar**: "la bienvenida A responde 35 %, la B 12 % → nos quedamos con A".

### Análisis de pautas
- **Qué es**: costo real de cada campaña. Marketing crea la pauta y carga la inversión; los leads se vinculan solos
  por el origen (formulario, utm, Excel), y en WhatsApp por **ID del anuncio**, título o **palabras clave** del primer
  mensaje.
- **Por pauta**: inversión, leads, **costo por lead**, contactados, ventas, conversión, **costo por venta**, cuotas
  vendidas y meses de recupero. Detalle: día a día, dónde están hoy los leads, motivos de cierre, resultados por
  vendedora. Orígenes sin pauta para crearla con un clic.

### Supervisión
- **Qué es**: el equipo **en vivo**: cada vendedora (disponible, en llamada, llamadas de hoy, chats), leads sin
  asignar (asignarlos ya), chats sin asignar, **estancados**, **fuera de SLA**.
- **Redistribuir la cartera** de una vendedora (vacaciones) entre el resto. Se actualiza sola.

### Importar base
- **Qué es**: subir Excel / CSV: detecta las columnas solo (incluidos campos personalizados), elegís embudo, etapa,
  vendedora o reparto, fuente y pauta; procesa en segundo plano con barra de progreso y detalle de errores; unifica
  repetidos. Planilla modelo para descargar.

## Configuración

### Embudos y tipificaciones
- **Embudo**: agentes y supervisores, **reparto** (rotativo o menor carga, **entre todos o solo conectados**, qué
  hacer si no hay nadie), **horario de atención**, **SLA de primer contacto** (avisar o reasignar), tarea al asignar,
  máximo de intentos, avisos de inactividad y estancados, reingreso de perdidos.
- **Reglas por origen**: "los de la pauta Instagram a Laura", "los referidos sin asignar".
- **Etapas**: ordenar, color, tipo, contacto efectivo, **datos obligatorios para entrar a la etapa**.
- **Tipificaciones**: motivos de venta y no venta por categoría, nota obligatoria, acciones (postergar, no contactar,
  dato erróneo).

### Campos personalizados
- Datos propios del negocio (texto, número, fecha, sí/no, lista, email, teléfono, link), para todos o un embudo;
  visibles en tarjeta, lista, filtros, importación, API y variables de mensajes; obligatorios por etapa.

### Puntaje de leads
- Reglas que suman o restan puntos (respondió, pauta, canal, origen, campo, email, etapa, reingresos, intentos sin
  contacto, días sin actividad). El puntaje ordena Mi día y se ve en tarjeta y lista.

### Automatizaciones
- Formulario en **4 pasos** con resumen en una frase:
  1. **Cuándo**: al entrar a la etapa · si no responde en X · cuando responde · sin actividad X · **después de otra
     automatización** (secuencias).
  2. **Cuánto esperar**.
  3. **Qué hacer**: WhatsApp (plantilla o texto), **email (plantilla de email)**, SMS, tarea, aviso a la vendedora o
     a supervisión, **mover de etapa / cerrar**, **pasar a otro embudo** (crear, mover o volver).
  4. **Condiciones**: solo si sigue en la etapa, solo en horario.
- **Ejecuciones**: registro de cada una (ejecutada, omitida y por qué, error).
- Ejemplos: bienvenida al instante → recordatorio al día 3 → último aviso al día 7; "7 días sin movimiento → cerrar
  como Sin respuesta"; "Venta → abrir en embudo Clientes"; "En verificación → embudo Verificaciones → vuelve".

### Plantillas de email
- Asunto y texto con variables; se usan en la ficha, automatizaciones y difusiones.

### Configuración de email
- Servidor de correo (Gmail, Brevo, SES, Mailgun), remitente, emails por minuto y **"Enviar prueba"**.

### Plantillas WA
- Plantillas de WhatsApp con variables; envío a aprobación de Meta y sincronización del estado; Twilio por ContentSid.
  **Respuestas rápidas** para el `/` del chat.

### Líneas WhatsApp
- Cada número: proveedor (**Meta oficial, Twilio o QR**), embudo para números nuevos, quiénes la usan, ritmo
  anti-bloqueo. Conexión por **QR** desde la pantalla (se renueva sola); webhook y verify token para copiar; estado.

### Telefonía Anura
- Token de Click2Dial (verificar), **internos y alias** por usuario, **rutas** (número o cola → embudo), plantilla de
  eventos para copiar, opciones (tarea por perdida, grabaciones, solo llamadas propias), modo demo y simulador de
  entrante.

### Usuarios
- Alta por invitación (email) o contraseña temporal, rol, embudos, líneas de WhatsApp, interno de Anura,
  disponibilidad, activar / desactivar.

### Roles y permisos
- Administrador, Supervisor, Agente y **roles a medida** eligiendo permiso por permiso (ver todo, supervisión,
  reportes, pautas, difusiones, reasignar, importar, exportar, eliminar, reabrir, discador, telefonía, plantillas,
  líneas, embudos, campos, automatizaciones, integraciones, usuarios).

### Integraciones API
- Claves para formularios, landings, n8n o sistemas propios (alta de lead, búsqueda, envío de WhatsApp), log de cada
  llamada, y **SMS (Twilio)** con su webhook de respuestas.

---

## Preguntas que suelen aparecer

- **¿Y si el mismo lead entra por WhatsApp y por el formulario?** Un solo contacto; el segundo ingreso queda como
  reingreso en la ficha y cuenta en "Ingresó N veces".
- **¿Se puede cortar la llamada desde el CRM?** Hoy se corta desde el softphone de Anura (Anura no lo permite por API);
  está planificado un teléfono dentro del CRM.
- **¿Cuántos números de WhatsApp?** Los que quieran, mezclando oficiales y por QR.
- **¿Se puede migrar lo que tienen hoy?** Sí, por Excel.
- **¿Cuánto volumen aguanta?** Probado con 10.000 leads y 20.000 automatizaciones programadas, y pantallas medidas con
  40.000 oportunidades.
- **¿Quién ve qué?** Cada vendedora solo lo suyo; supervisión todo; todo cambio queda registrado con autor.
