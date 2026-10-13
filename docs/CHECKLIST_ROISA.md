# Checklist de requerimientos (Roisa) — estado y plan

Fuente: "CHECKLIST DE REQUERIMIENTOS CRM – BAMINDS.docx" (06/10/2026). Leyenda: ✅ cumple. **Estado al 06/10/2026: todos los puntos cubiertos.**

| # | Requisito | Estado | Cómo se resuelve |
|---|---|---|---|
| 1.1 | Unificar duplicados + historial completo | ✅ | Deduplicación por teléfono, historial con autor |
| 1.2 | Cuántas veces ingresó y cómo se clasificó | ✅ | Contador "Ingresó N veces" en ficha y lista, filtro |
| 1.3 | Filtrar por fecha, fuente, estado, vendedora | ✅ | Lista de oportunidades |
| 2.1 | Plantillas enviadas por vendedora | ✅ | Reporte "Actividad por vendedora" |
| 2.2 | Número y duración de llamadas por vendedora | ✅ | Ranking |
| 2.3 | Mensajes/correos manuales y automáticos por vendedora | ✅ | Reporte "Actividad por vendedora" |
| 3.1 | Si el cliente respondió a los estímulos | ✅ | "Respondió" por envío; aperturas/clics de email |
| 3.2 | Calidad de mensajes automáticos según respuesta | ✅ | % entregado / leído / respondido / avanzó por plantilla y automatización |
| 4.1 | Leads únicos en el período | ✅ | Únicos = nuevos + reingresos (contactos distintos) |
| 4.2 | Leads atendidos por vendedora | ✅ | Ranking |
| 4.3 | Leads no contactados dentro del SLA | ✅ | SLA por embudo + reporte |
| 5.1 | Conversión etapa a etapa | ✅ | Embudo de conversión (cohorte) |
| 5.2 | Pipeline por vendedora y etapa | ✅ | Matriz vendedora × etapa |
| 6.1 | Panel de la vendedora con sus leads | ✅ | Mi día, tablero, lista |
| 6.2 | Llamar desde el CRM | ✅ | Anura Click2Dial |
| 6.3 | Plantillas o correos desde el panel | ✅ | WhatsApp ✅; email manual desde la ficha |
| 6.4 | Notas y cambios de estado desde el panel | ✅ | |
| 7.1 | Automatizaciones por tiempo desde la toma (día 3, día 7) | ✅ | Demora en la etapa inicial |
| 7.2 | Cambiar clasificación si no hay acción en X tiempo | ✅ | "Sin actividad X días → mover / cerrar" |
| 7.3 | Automatizaciones por acción del cliente | ✅ | "Respondió" / "No respondió en X h" |
| 7.4 | SLA y reasignación si no hay contacto | ✅ | Reasignación automática + alerta |
| 8.1 | Formularios web / landings | ✅ | API |
| 8.2 | Correo, WhatsApp, SMS con trazabilidad | ✅ | WhatsApp ✅; email manual; SMS por Twilio |
| 8.3 | Telefonía con registro y duración | ✅ | Anura |
| 9.1 | Indicadores en tiempo real | ✅ | Refresco automático + "fuera de SLA" |
| 9.2 | Vistas por rol | ✅ | "Mis números" para la vendedora |
| 10.1 | Roles | ✅ | + roles a medida |
| 10.2 | Auditoría | ✅ | Historial con valor anterior → nuevo |
| 10.3 | Seguridad y privacidad | ✅ | (opcional: exportar/borrar datos de una persona) |
| 11.1 | Lead scoring | ✅ | Puntaje por reglas |
| 11.2 | Informes de marketing / campañas | ✅ | Análisis de pautas |
| 11.3 | Informes predictivos | ✅ | Proyección de ventas del mes |
| 11.4 | Pipelines visuales | ✅ | Tablero |
| 11.5 | Discador automático | ✅ | Progresivo |
| 34 | Tiempo de habla, logueo, after call work, cantidad de llamadas | ✅ | Sesiones de conexión + ACW |
| 35 | Discador integrado | ✅ | |

## Plan

1. ✅ **Reportes con datos existentes** (hecho 06/10): actividad por vendedora, embudo de conversión, matriz
   vendedora × etapa, leads únicos, contador de ingresos, "Mis números", refresco automático.
2. ✅ **SLA** (hecho 06/10): definición por embudo, reporte, reasignación automática, alerta.
3. ✅ **Automatizaciones por respuesta e inactividad** (hecho 06/10).
4. ✅ **Calidad de envíos** (hecho 06/10).
5. ✅ **Métricas de call center** (hecho 06/10): tiempo conectado, after call work.
6. ✅ **Email manual** y **SMS** (hecho 06/10).
7. ✅ **Lead scoring** y **proyección** (hecho 06/10).

## Notas
- **SLA:** arranca desactivado (0 min) en cada embudo; activarlo cuando Roisa defina el tiempo.
- **Tiempo conectado:** se mide desde que se instaló esta versión (no hay datos anteriores).
- **Emails:** las aperturas son aproximadas (bloqueo o precarga de imágenes); los clics son exactos. Las respuestas a
  los emails manuales llegan al email de la vendedora (reply-to), no se leen dentro del CRM.
- **SMS:** requiere una cuenta y un número de Twilio con SMS (Integraciones → SMS).
- **Lead scoring:** por reglas configurables; uno predictivo tiene sentido con meses de historia.
- **Proyección:** ritmo del mes + ventas esperadas del pipeline (conversión histórica por etapa, 90 días).
