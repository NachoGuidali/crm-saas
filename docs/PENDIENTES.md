# Pendientes

- **Softphone dentro del CRM (JsSIP):** ver [`PENDIENTE_SOFTPHONE.md`](PENDIENTE_SOFTPHONE.md). Esperando respuesta de Anura.

## Contactos que ya son clientes (postventa)

Hoy, cuando llama, escribe o reingresa alguien con una oportunidad en **Venta** (cerrada), no se abre otra tarjeta: la
llamada queda en su contacto y se registra un "reingreso" en la tarjeta de la venta. Pero si **nadie atiende**, no hay
responsable claro: el aviso de llamada perdida va a supervisores y la tarea "Devolver llamada" queda **sin asignar**.

A definir con el cliente quién se ocupa de los clientes:
- un usuario / rol **Postventa** (con permisos para ver clientes, sus llamadas y mensajes), o
- el **vendedor que registró la venta**, o
- configurable (por embudo o por línea).

Aplica a llamadas perdidas, WhatsApp de clientes y reingresos por formulario.
