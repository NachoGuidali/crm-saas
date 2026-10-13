# Pedidos de la reunión de demo con Roisa (09/10/2026)

> **Actualizado 09/10:** hechos ID de grabación, resultado de gestión obligatorio + sin calificar, reproceso (acción reasignar), reingreso → prioridad/etiqueta, vencimiento de fichas, socios, gestión por pauta/base, filtros de tablero, tarea masiva, condiciones por campaña/etiqueta, campos de tipo archivo y API con etapa. Ver FUNCIONALIDADES §24. Devolución enviada como página: https://claude.ai/artifact/R9H2bF6Q3i5VceTmkD81qy. **Horario por vendedora: en standby** (se resuelve con "solo conectadas").

Participantes: Melina Perez, Maximiliano Delia, Martín Gerez. Fuente: resumen y transcripción de Read AI.
Leyenda: ✅ ya está · 🔧 cambio chico (horas) · 🧩 desarrollo (días) · ❓ depende de terceros / definir con ellos.

## Compromisos tomados en la reunión
| Pedido | Estado | Cómo |
|---|---|---|
| Devolver el checklist marcando disponible / a desarrollar | ✅ armado | `CHECKLIST_ROISA.md` (todo cubierto) → falta pasarlo a un documento para ellos |
| Mostrar el **ID de la grabación** de cada llamada | 🔧 | Mostrar el id de Anura (cdrid) junto a la grabación en la ficha y en Llamadas; buscable |
| Investigar **Instagram y Facebook Messenger** por canales oficiales de Meta | 🧩 ❓ | Sí se puede: Messenger Platform + Instagram Messaging API (página de Facebook + cuenta de Instagram profesional, app de Meta con permisos `pages_messaging` / `instagram_manage_messages` y revisión de Meta). Entran a la misma bandeja como un canal más. 3–5 días + aprobación de Meta |
| **No dejar cerrar la gestión sin tipificar** y ver masivamente las gestiones sin calificar | 🧩 | Al terminar una llamada (o del discador) se abre "Resultado de la gestión" obligatorio; aviso si intenta salir de la ficha sin cargarlo; estado "Sin calificar" + filtro y reporte por vendedora |
| Mejoras + versión para que prueben las operadoras | 🧩 | Demo limpio con usuarios reales de prueba |
| Reevaluar personalización y plazos y pasar estimación | — | Este documento |
| **Calidad de la línea de Meta en tiempo real** | 🔧 | La API ya devuelve `quality_rating` y límite de mensajes: mostrarlo en Líneas, guardar el historial y avisar a supervisión cuando baja (webhook `phone_number_quality_update`). Para líneas por QR no existe ese dato: alertar por % de envíos fallidos |

## Otros pedidos que surgieron
| Pedido | Estado | Cómo |
|---|---|---|
| **Reprocesamiento automático**: leads en ciertas etapas sin respuesta en X días → reasignar a otra vendedora, parejo | 🔧 | Acción de automatización nueva "Reasignar a otro agente" (excluye a la actual, usa el reparto del embudo) con el disparador "sin respuesta" o "sin actividad". Manual ya existe (Repartir) |
| **Horario de trabajo por vendedora** (turnos distintos) | 🔧 | Ya conversado: horario por usuario que combina con disponible / conectada; el SLA no corre fuera de turno |
| **Priorizar reingresos** | 🔧 | Disparador de automatización "Cuando el lead reingresa" (etiqueta, aviso, mover, puntaje). El puntaje por reingresos ya existe |
| **Granularidad por campaña / base**: llamadas, mensajes, mails y ventas de cada campaña o base importada | 🔧 | En Análisis de pautas sumar actividad por pauta (llamadas, minutos, WhatsApp, emails, por lead). Las bases importadas se asignan a una pauta |
| Filtro por **pauta / campaña en el tablero** | 🔧 | Agregar el filtro (en la lista ya está) |
| **Priorizar campañas** ("ahora llamen a tal campaña") | ✅/🔧 | Discador por campaña + puntaje por pauta. Opcional: "prioridad" en la pauta que ordene Mi día |
| **Email desde la casilla de cada asesor** | 🔧 ❓ | Con un proveedor de envío y el dominio verificado, cada email sale "De: Laura <laura@roisa…>". Alternativa: casilla propia por usuario (contraseña de aplicación) |
| Reportes **descargables** | ✅/🔧 | CSV ya está; sumar Excel. Reportes a medida según lo que pidan en la prueba |
| **Cortar llamadas desde el CRM** (16.000 llamadas/mes) | 🧩 ❓ | Teléfono web dentro del CRM (JsSIP, `PENDIENTE_SOFTPHONE.md`): pasa a **prioridad alta**. Depende de los datos de Anura |
| Que la ficha caiga en pantalla y la llamada se dispare sola | ✅ | Discador progresivo (abre la ficha y llama). Con Auto answer de Anura no hay que tocar nada |
| **Switch de línea de WhatsApp** (si levantan el baneo de Meta, pasar a la oficial) | ✅/🔧 | Multilínea: la línea de salida se elige por embudo y por ficha. Opcional: "línea de salida predeterminada" global de un clic |
| **Bot propio** (Evolution) → CRM con **contexto de la conversación** para el asesor | 🧩 ❓ | API para que el bot cree el lead y cargue la conversación (y el "pase a asesor"). Hay que ver cómo está hecho su bot |
| **Carrito abandonado** (Click2Buy de Doctor Flex) priorizado con los datos cargados | 🧩 ❓ | La web manda cada paso al CRM por API; se crea/actualiza el lead con esos datos, etiqueta "abandono" y prioridad. Necesita a su equipo web |
| **Link de pago Mercado Pago** desde la ficha | 🧩 | Botón "Enviar link de pago": genera el link (Checkout Pro) por WhatsApp/email y, al pagarse, marca la venta. 2–3 días |
| **Afimedes / portal de agentes**: que vuelva el estado de la auditoría post venta | 🧩 ❓ | Al pasar a Venta el CRM envía el lead; su sistema devuelve el estado por API (por ID del CRM o DNI). Alternativa: lectura de su base. Depende de su equipo |
| Conectar **Google Ads / Meta** (inversión y leads automáticos) | 🧩 ❓ | Inversión por API de Meta Marketing / Google Ads; leads de formularios de Meta (Lead Ads) directo al CRM |
| Llamadas de WhatsApp entrantes | ✗ | No disponible (WhatsApp no las expone a integraciones) |

## Propuesta de orden
1. **Semana 1 (cambios chicos):** ID de grabación · reasignación automática · horario por vendedora · disparador de reingreso · gestión sin calificar · filtro de pauta en tablero · actividad por campaña · calidad de línea Meta · Excel · email por asesor.
2. **En paralelo:** demo limpio con usuarios para las operadoras.
3. **Semana 2:** teléfono web (si Anura pasa los datos) · Mercado Pago · Instagram/Facebook (sujeto a aprobación de Meta).
4. **A definir con ellos:** bot, carrito abandonado, Afimedes, Google/Meta Ads.

---

# Segunda charla (con Martín, "Integración CRM y gestión de leads", 09/10)

| Pedido | Estado | Cómo |
|---|---|---|
| **Portal de Agentes** (auditoría médica + documentación → "Control OK / Aprobado") de vuelta al CRM | 🧩 ❓ | **Webhook de entrada** en el CRM para que el portal postee el estado (por ID del CRM o DNI) → mueve la tarjeta (ej. embudo "Afiliación": En auditoría → Aprobado / Rechazado). Alternativa: el CRM consulta el endpoint de reportes que están pidiendo al equipo del portal. Coordinar con ese equipo |
| **Insertar leads por API con un estado definido** | 🔧 | La API ya crea leads y elige embudo; sumar `etapa` (y opcional `agente`) en el alta |
| **Instagram**: separar socios (experiencia del socio) de interesados | 🧩 ❓ | Opción rápida: bot (n8n) en Instagram que pregunta y crea el lead en el CRM con su teléfono. Opción completa: Instagram dentro de la bandeja (API oficial de Meta) |
| **Socios / lista negra**: si el número ya es venta ganada, que no se asigne ni genere tareas | ✅/🔧 | Hoy ya no abre tarjeta nueva ni asigna (queda como reingreso en la venta). Falta: que un WhatsApp de un socio no caiga en "Sin asignar" de ventas sino a Postventa (pendiente postventa) y etiqueta "Socio" automática al ganar |
| Una ficha = un agente (no la pueden tomar dos) | ✅ | Ya funciona así; solo supervisión reasigna |
| **Vencimiento de la ficha**: X días para cerrar; si vence, vuelve a reparto. Desde cierta etapa (ej. preventa) queda siempre de la asesora | 🔧 | En el embudo: "La ficha vence a los N días sin cerrarse" + qué hacer (reasignar / devolver a cola / avisar) + "No vence desde la etapa…" |
| **Reproceso**: reinyectar al día siguiente los que no contestaron | ✅/🔧 | Automatización "si no responde en X → mover a Nuevo / crear tarea / reasignar a otra" (reasignar es la acción nueva de la semana 1) |
| Cambio de proveedor de WhatsApp | ✅ | El historial queda en la ficha; se sigue escribiendo desde la línea nueva |
| Acceso a la demo para Martín | — | Con el demo limpio para las operadoras |
