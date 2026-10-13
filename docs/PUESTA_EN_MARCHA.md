# Puesta en marcha: crmmkt.supregsolutions.com

Paso a paso para levantar el CRM en el servidor cloud y conectar Anura con un interno de prueba.
Archivos que se usan: `.env.produccion`, `deploy/nginx-crmmkt.conf`, `deploy/backup.sh`.

---

## 0. Antes de empezar (5 min)

- [ ] **DNS:** registro `A` `crmmkt.supregsolutions.com` → IP pública del servidor (si usás Cloudflare, en "DNS only"
      hasta sacar el certificado). Verificar: `dig +short crmmkt.supregsolutions.com`.
- [ ] **Servidor:** Linux con Docker + plugin compose (`docker compose version`) y nginx. Mínimo 2 vCPU / 4 GB RAM /
      20 GB de disco. Puertos 80 y 443 abiertos.
- [ ] **Puerto interno libre:** el CRM escucha en `127.0.0.1:8010`. Si ya lo usa otra app: `ss -ltnp | grep 8010`;
      si está ocupado, cambiar `WEB_PORT` en el `.env` y en `deploy/nginx-crmmkt.conf`.

## 1. Subir el proyecto

Desde tu máquina (WSL), sin el entorno virtual ni los archivos locales:

```bash
cd /home/nacho/crm-venta-roisa
rsync -av --exclude .venv --exclude .env --exclude .env.dev --exclude 'app/media' --exclude 'app/logs/*.log' \
      --exclude '__pycache__' crm-ventas/ usuario@SERVIDOR:/opt/crm-ventas/
scp crm-ventas/.env.produccion usuario@SERVIDOR:/opt/crm-ventas/.env
```

`.env.produccion` ya tiene el dominio y claves generadas al azar (secret key, base de datos, admin inicial).
**Guardá una copia de ese archivo en un lugar seguro**: sin él no se accede a la base.

## 2. Levantar los contenedores

```bash
cd /opt/crm-ventas
chmod 600 .env
docker compose up -d --build
docker compose ps                       # web, worker, worker-masivos, beat, db y redis "running/healthy"
docker compose logs web | tail -20      # debe mostrar las migraciones y "Usuario administrador creado"
curl -I http://127.0.0.1:8010/usuarios/login/    # 200
```

El primer arranque crea las tablas, el embudo "Doctor Flex — Prospectos" con sus etapas y tipificaciones,
y el usuario `admin` con la contraseña `ADMIN_PASSWORD` del `.env`.

**¿Datos de ejemplo para mostrar?** (300 prospectos, equipo, pautas). Solo en una instalación de demo, nunca en la
que se va a usar en serio: `docker compose exec web python manage.py datos_demo`.

## 3. nginx + HTTPS

```bash
sudo cp deploy/nginx-crmmkt.conf /etc/nginx/sites-available/crmmkt.supregsolutions.com
sudo ln -s /etc/nginx/sites-available/crmmkt.supregsolutions.com /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d crmmkt.supregsolutions.com      # elegir "redirect" a HTTPS
```

Probar `https://crmmkt.supregsolutions.com` → pantalla de login. Entrar con `admin` y la contraseña del `.env`,
y **cambiarla** desde el perfil.

Cuando el HTTPS esté estable, se puede activar HSTS: `SECURE_HSTS_SECONDS=31536000` en `.env` y
`docker compose up -d web`.

## 4. Backups (2 min)

```bash
sudo mkdir -p /opt/backups/crm-ventas
crontab -e      # agregar:
15 4 * * * /opt/crm-ventas/deploy/backup.sh >> /var/log/crm-backup.log 2>&1
```

Guarda la base (`pg_dump`) y los adjuntos todos los días, conserva 14 días. Probarlo una vez a mano:
`./deploy/backup.sh`. Restaurar: `docker compose exec -T db pg_restore -U crm_ventas -d crm_ventas --clean < db-XXXX.dump`.

## 5. Email (recupero de contraseña)

Mientras no esté cargado, los mails de recupero se escriben en el log (`docker compose logs web`). Para Gmail:
cuenta con verificación en 2 pasos → *Contraseñas de aplicación* → crear una → en `.env`:

```
EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
EMAIL_HOST_USER=supregsolutions@gmail.com
EMAIL_HOST_PASSWORD=<contraseña de aplicación>
```

y `docker compose up -d` (recrea los contenedores con el `.env` nuevo).

---

## 6. Anura con el interno de prueba (cuenta de otro cliente)

### 6.1 Qué pedir / preparar en esa cuenta de Anura

| # | Qué | Para qué |
|---|---|---|
| 1 | **Un interno nuevo de prueba** (ej. `299 – Prueba CRM`) con usuario para entrar al **Anura web** | Es "tu" agente: desde ahí suena y cortás |
| 2 | Que ese interno tenga **terminal principal** (el softphone web alcanza) y que salga a celulares | Click2Dial hace sonar la terminal principal; sin ella da error "No Originate terminal" |
| 3 | El **token de Click2Dial** de la cuenta (*Integraciones → Click 2 Dial*) | El CRM lo usa para originar llamadas |
| 4 | Acceso con **rol de configuración** para crear los **Eventos** (o cargarlos vos con el admin) | Para que Anura avise al CRM de cada llamada |
| 5 | (Opcional) un **número entrante (DID) o cola que derive a ese interno** | Para probar que una llamada entrante crea la tarjeta |
| 6 | La **dirección del Anura web** (la URL con la que entran los agentes) | Botón "Ir a Anura" del aviso de llamada |

### 6.2 Cargar los Eventos en Anura

Dos pasos:

**1. Plantilla** — *Configuración → Eventos → Agregar* (una sola; petición + cuerpo):

| Campo | Valor |
|---|---|
| Protocolo / Puerto | HTTPS / 443 |
| Host | `crmmkt.supregsolutions.com` |
| Ruta | `api/integrations/anura/webhook` |
| Método / Content-Type | POST / JSON |
| Autorización | Bearer `<token del CRM>` (se copia en el CRM: *Telefonía Anura*) |
| Cuerpo | la plantilla que muestra el CRM en *Telefonía Anura* (clic para copiar) |

**2. Triggers** — *Configuración → Cuenta* → la cuenta del **interno de prueba** → **Modificar** → pestaña **Eventos**
→ **Agregar** tres veces: `START`, `TALK` y `END`, dirección `BOTH`, plantilla la del paso 1, Activo tildado.

**Cargar los triggers SOLO en la cuenta del interno de prueba** (y en la cola o DID de prueba, si hay). Así Anura no
manda al CRM las llamadas del resto de la empresa.

### 6.3 Configurar el CRM

*Configuración → Telefonía Anura*:

1. **Modo demostración: apagado**. **Integración activa: prendido**.
2. **Token de Click2Dial** → pegar → **Verificar token** (tiene que dar OK sin llamar a nadie).
3. **Dirección del Anura web** → la URL del punto 6.1.6.
4. **Procesar solo llamadas de internos o rutas del CRM: PRENDIDO.** Es el cinturón de seguridad por si algún
   evento quedó asignado a otras cuentas: el CRM descarta toda llamada que no sea de un interno asociado a un
   usuario, de una ruta cargada o de un "Llamar" del CRM. No se crean contactos con datos del otro cliente.
5. **Embudo para llamadas de números desconocidos**: Doctor Flex.
6. Si hay DID/cola de prueba: *Rutas* → agregar el DID (tal como lo manda Anura en `called`) o el queueId → embudo.

*Usuarios → tu usuario (o un agente de prueba)* → **Interno de Anura** = el interno de prueba (ej. `299`).
Si en los eventos Anura lo informa con otro nombre (usuario SIP, nombre de cuenta), cargarlo en **Alias**.

### 6.4 Pruebas (en este orden)

| # | Prueba | Qué tiene que pasar |
|---|---|---|
| 1 | Crear una oportunidad con **tu celular** → **Llamar** | Suena el Anura web → atendés → suena tu celular. En el CRM aparece el aviso de llamada con "Ir a Anura" |
| 2 | Hablar 20 s y cortar desde Anura | En la ficha queda la llamada **atendida, ~20 s**, y se pide tipificar |
| 3 | Llamar y **no atender** en el celular | Queda **no atendida** y suma un intento |
| 4 | Con Anura web **cerrado**, apretar Llamar | Mensaje claro: "Tu teléfono de Anura no está conectado" |
| 5 | (Si hay DID) llamar al DID desde un número que **no** está en el CRM | Se crea la tarjeta en Doctor Flex, asignada a quien atendió |
| 6 | Mismo, pero sin atender | Tarjeta + tarea "Devolver llamada" + aviso |
| 7 | Discador: campaña con 2–3 números propios | Llama de a uno cuando el agente queda libre |

**Si algo no llega:** mirar *Llamadas* en el CRM y `docker compose logs web worker | grep -i anura`. Si no aparece
nada, el evento no está asignado a la cuenta o la URL/token está mal (nginx: `/var/log/nginx/access.log`). Si el log
dice "Evento de Anura ignorado", el interno no coincide: ver qué identificador trae el evento y cargarlo como alias.

**Al terminar las pruebas en la cuenta del otro cliente:** borrar los triggers de la cuenta, la plantilla y revocar el token de Click2Dial.

---

## 7. Actualizar a una versión nueva

```bash
# desde tu máquina: el mismo rsync del paso 1 (sin el scp del .env)
cd /opt/crm-ventas && docker compose up -d --build     # migra solo al arrancar
```

## 8. WhatsApp por QR (Evolution) — opcional, cuando haga falta

```bash
cd /opt/crm-ventas
grep EVOLUTION .env              # EVOLUTION_API_KEY con valor; EVOLUTION_PORT libre (ej. 8081)
ss -ltnp | grep 8081             # tiene que estar libre
docker compose --profile evolution up -d
docker compose ps                # evolution-db-init "Exited (0)" y evolution-api "Up"
docker compose logs evolution-api --tail 30
```

- `evolution-db-init` crea sola la base `evolution` dentro del Postgres del CRM (antes había que crearla a mano y, si
  no existía, Evolution no arrancaba).
- El CRM habla con Evolution por la red interna (`EVOLUTION_API_URL=http://evolution-api:8080`): no hace falta
  publicarlo en internet ni un subdominio para que funcione el QR.
- Después: *Configuración → Líneas WhatsApp → Nueva* → proveedor **Evolution** → guardar → **Conectar / ver QR** →
  escanear desde el celular (WhatsApp → Dispositivos vinculados → Vincular un dispositivo).
- Los mensajes entrantes llegan al webhook `https://<dominio>/whatsapp/webhook/evolution/<clave>/`, que el CRM
  configura solo en Evolution al pedir el QR. Si no entran mensajes: `docker compose logs evolution-api | grep -i webhook`.
