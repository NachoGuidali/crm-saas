# Modo SaaS (multi-cliente)

Una sola instalación atiende a todos los clientes. Cada cliente tiene **su propio schema de Postgres** con
todas las tablas del CRM (usuarios, embudos, contactos, líneas, llamadas…) y entra por **su subdominio**.
Los datos de un cliente nunca se mezclan con los de otro.

```
midominio.com              → panel de la plataforma (alta de clientes, planes, suspensión) + "Ingresar"
acme.midominio.com         → CRM de Acme          (schema c_acme)
clinicasur.midominio.com   → CRM de Clínica Sur   (schema c_clinicasur)
```

Stack: [django-tenants](https://django-tenants.readthedocs.io/) (schema por cliente, elegido por el subdominio)
y [tenant-schemas-celery](https://github.com/maciej-gol/tenant-schemas-celery) (las tareas corren en el schema
del cliente que las encoló).

## Qué queda aislado y cómo

| Pieza | Cómo se separa |
|---|---|
| Tablas del CRM | Un schema por cliente (`TENANT_APPS`). El schema `public` tiene solo clientes, dominios, planes y los superusuarios del panel (`SHARED_APPS`). |
| Usuarios y login | Cada cliente tiene su tabla de usuarios: el usuario `admin` de Acme y el de Clínica Sur son personas distintas con contraseñas distintas. |
| Sesiones | Cookies atadas a cada subdominio (no se define `SESSION_COOKIE_DOMAIN`): la sesión de un cliente no abre el CRM de otro. |
| Caché / Redis | La clave lleva el schema (`django_tenants.cache.make_key`). El ritmo anti-bloqueo de WhatsApp (Redis directo) también. |
| Archivos | `media/<schema>/…` (`core.storage.ArchivosPorCliente`). `/media/…` se sirve desde la carpeta del cliente del subdominio. |
| Celery | Cada tarea viaja con el schema; beat encola cada tarea periódica una vez por cliente con acceso (`core.beat`). |
| Webhooks y links | Salen con el subdominio del cliente (`core.tenancy.url_sitio()`): `https://acme.midominio.com/whatsapp/webhook/…` |
| API | Solo claves por integración (de cada cliente). Se eliminó la clave global `CRM_API_KEY`. |
| Credenciales de integraciones | Tokens y contraseñas (WhatsApp Meta/Twilio/Evolution, API y SIP de Anura, SMTP, SMS) cifrados en la base con `core.cifrado.CampoCifrado` (clave derivada de `SECRET_KEY`). |
| Auditoría | Bitácora append-only por schema (`users.RegistroAuditoria`): ingresos, usuarios, credenciales, exports y borrados de cada cliente en su schema; acciones del panel en el público. Pantalla *Auditoría* (admin del cliente / panel). |
| Evolution (servidor compartido) | Instancias con prefijo `crm-<schema>-`; el formulario no deja usar el prefijo de otro cliente. La clave global solo viaja al servidor de la plataforma. |
| Valores únicos | Teléfono, nombres de embudo, plantillas, etc. son únicos **dentro** de cada cliente (dos clientes pueden tener el mismo prospecto). |

## Puesta en marcha

1. DNS: registro A para `midominio.com` y `*.midominio.com` → IP del servidor. Es **un solo registro comodín**
   para todos los clientes: dar de alta un cliente no requiere tocar el DNS. Los subdominios que ya tengan su
   propio registro (ej. `mail.midominio.com`) no se ven afectados: el comodín solo responde por los que no existen.
   Si el dominio se comparte con otros servicios, conviene un nivel propio: `*.crm.midominio.com`
   (`SAAS_DOMINIO=crm.midominio.com`).
2. Certificado comodín y nginx: ver `deploy/nginx-saas.conf`.
3. `.env` (ver `.env.example`): `SAAS_DOMINIO`, `ALLOWED_HOSTS=.midominio.com`,
   `CSRF_TRUSTED_ORIGINS=https://midominio.com,https://*.midominio.com`, `SITE_URL=https://midominio.com`,
   `ADMIN_USERNAME` / `ADMIN_PASSWORD` (superusuario del panel).
4. `docker compose up -d --build`. Al arrancar, `web` corre `migrate_schemas` (público + todos los clientes) y
   `setup_plataforma` (dominio principal + superusuario del panel).
5. Entrar a `https://midominio.com/panel/`, cargar los planes y dar de alta el primer cliente.

## Operación diaria

**Alta de cliente**: panel → *Nuevo cliente* (empresa, subdominio, plan, administrador). Se crea el schema,
se carga un embudo de ventas genérico (7 etapas, tipificaciones, 3 roles, respuestas rápidas) y el usuario
administrador. Si no se carga contraseña, se genera una temporal que se pide cambiar al primer ingreso. Las
credenciales se muestran **una sola vez**. Por consola:

```bash
docker compose exec web python manage.py crear_cliente acme --nombre "Acme SA" --email admin@acme.com --plan Básico
```

**Planes y límites**: cada plan define usuarios activos, líneas de WhatsApp y **mensajes de WhatsApp automáticos/difusiones por día** (0 = sin límite; las respuestas 1 a 1 de los agentes no cuentan ni se frenan). En la ficha
del cliente se puede pisar el límite del plan. El CRM no deja pasar el límite (crear o reactivar usuarios, crear líneas). El tope diario de WhatsApp cuenta solo los envíos automáticos y las difusiones (los mensajes 1 a 1
que escribe un agente no cuentan ni se bloquean). Al alcanzarlo, lo automático/difusiones se frena y lo
pendiente sigue al día siguiente (protege el número ante Meta y te deja cobrar el volumen por plan).

**Suspender a mano**: ficha del cliente → *Suspender*. Se corta pantalla, API y webhooks, y beat deja de correr
sus tareas. *Reactivar* lo vuelve a habilitar sin perder nada.

## Funciones extra (habilitar o cobrar algo a ciertos clientes)

El código es **uno solo** para todos los clientes: una función nueva o un arreglo le sirve a todos a la vez.
No hay "una versión distinta por cliente". Lo que sí se puede es **prender/apagar** una función por plan o por
cliente puntual (útil cuando un cliente pide algo extra y se lo cobrás aparte), sin tocar a los demás.

El mecanismo son flags:

- Registro central en `apps/clientes/models.py` → `FUNCIONES_EXTRA` (clave + nombre que se ve en el panel).
- `Plan.funciones_extra`: lo que trae el plan (para **todos** sus clientes).
- `Cliente.funciones_extra`: extras habilitadas **solo a ese cliente** (se **suman** a las del plan, no lo pisan).

**Para agregar una función que no todos tienen:**

1. Sumá una línea al registro: `FUNCIONES_EXTRA = [('reportes_avanzados', 'Reportes avanzados'), ...]`.
2. En el código, encerrá la parte opcional con el flag:
   - Python/vistas: `from core.tenancy import tiene_funcion` → `if tiene_funcion('reportes_avanzados'): ...`
   - Templates: `{% if 'reportes_avanzados' in funciones_cliente %}...{% endif %}`
3. Deploy. Después, desde el panel, tildala en el **plan** (para todos sus clientes) o en la **ficha del cliente**
   (para uno solo).

Con el registro vacío no hay funciones extra: el CRM funciona igual para todos. Fuera del modo multi-cliente
(schema público, comandos, tests) `tiene_funcion()` devuelve `False`, así que lo opcional queda apagado por
defecto. **Ojo:** no envuelvas una función que los clientes ya usan en un flag, porque se les apagaría hasta
tildarla; los flags son para funciones nuevas u opcionales.

## Cobros

Cada cliente tiene **Pagado hasta** (vencimiento) y un **precio** (el del plan o uno acordado). Cada pago corre el
vencimiento N meses. Por defecto el período arranca en el vencimiento anterior (se mantiene el ciclo aunque pague
unos días tarde); si vuelve después de una suspensión larga, arranca el día del pago.

**Cobro manual (transferencia / efectivo)**: ficha del cliente → *Registrar pago* (monto, meses, medio, nº de
operación). Si estaba suspendido se reactiva. Un pago cargado por error se anula y se recalcula el vencimiento.

**Revisión diaria** (Celery beat, 9:00, en el schema público — `clientes.revisar_cobros`):

| Situación | Qué pasa |
|---|---|
| Vence en `SAAS_DIAS_AVISO` días (5) | Email al contacto del cliente con `SAAS_DATOS_PAGO` (una vez por vencimiento) y cartel a sus administradores dentro del CRM |
| Vencido hace hasta `SAAS_DIAS_GRACIA` días (5) | Sigue entrando, con cartel rojo en el CRM |
| Vencido hace más | Se suspende (*por falta de pago*) y se le avisa por email. Se reactiva solo al registrar el pago |
| Prueba vencida sin pago | Igual que un vencido (cuenta desde *Prueba hasta*) |

Por cliente se puede apagar la *suspensión automática* (clientes de confianza). Con `SAAS_SUSPENDER_VENCIDOS=False`
no se suspende a nadie automáticamente (solo avisos). La lista del panel ordena primero vencidos y por vencer, con
filtros, ingreso mensual y lo cobrado en el mes; *Pagos* muestra todos los pagos del mes por medio de pago.

### Mercado Pago (opcional, por cliente)

1. En [developers.mercadopago.com](https://www.mercadopago.com.ar/developers/panel/app) crear una aplicación
   (producto: *Suscripciones* y *Checkout Pro*) y copiar el **Access Token de producción** → `MP_ACCESS_TOKEN`.
2. En la aplicación → *Webhooks*: URL `https://midominio.com/mercadopago/webhook/`, eventos **Pagos** y
   **Planes y suscripciones**. Copiar la **clave secreta** → `MP_WEBHOOK_SECRET`.
3. En la ficha del cliente:
   - **Débito automático**: *Generar link de suscripción* con el email de la cuenta de Mercado Pago del cliente
     (tiene que coincidir, si no MP rechaza). Se le manda el link, autoriza la tarjeta y MP le cobra el precio
     todos los meses. Cada cobro aprobado llega al webhook y se registra solo como pago.
   - **Link de pago único**: N meses con Mercado Pago, sin débito automático.

Los pagos de MP no reactivan a un cliente suspendido **a mano** (solo a los suspendidos por falta de pago). El
webhook valida la firma y nunca confía en el cuerpo: con el id consulta el pago a la API de MP. Si se cambia el
precio de un cliente con débito automático, generar una suscripción nueva (la anterior se cancela sola).

**Comandos dentro de un cliente**: los comandos del CRM se corren con `tenant_command`:

```bash
# Datos de demostración (300 prospectos, equipo, línea simulada) en un cliente de prueba
docker compose exec web python manage.py tenant_command datos_demo --schema=c_demo_comercial
# Shell dentro de un cliente
docker compose exec web python manage.py tenant_command shell --schema=c_acme
```

**Migraciones**: `migrate_schemas` aplica las migraciones al schema público y a cada cliente. Con muchos
clientes: `migrate_schemas --executor=multiprocessing`.

**Backup**: `deploy/backup.sh` (pg_dump de todos los schemas + archivos). Cifra con AES-256 si definís
`BACKUP_PASSPHRASE` y sube una copia afuera del server si definís `RCLONE_DESTINO` (A3). Restaurar con
`deploy/restore.sh` (un cliente solo: `pg_restore -n c_acme …`). **Probá la restauración una vez por mes**:
un backup que nunca restauraste no sabés si sirve.

**Baja definitiva**: borrar el cliente desde el shell **no** borra su schema (`auto_drop_schema = False`, a
propósito). Después de un backup: `DROP SCHEMA c_acme CASCADE;` y borrar `media/c_acme/`.

## Desarrollo local

Los navegadores resuelven `*.localhost` a 127.0.0.1, así que no hace falta tocar `/etc/hosts`:

```bash
SAAS_DOMINIO=localhost SITE_URL=http://localhost:8010   # en el .env
# panel:   http://localhost:8010/panel/
# cliente: http://acme.localhost:8010/
```

Tests: `python manage.py test` corre todo dentro de un cliente de prueba (`core/test_runner.py`), más
`apps/clientes/tests.py` con dos clientes para verificar el aislamiento.

## Pendiente (próximas etapas)

- **Facturación electrónica** (AFIP) de cada pago.
- **Alta autogestionada**: que el cliente se registre y pruebe X días sin intervención (hoy el alta es desde el panel).
- **Ingresar con el email**: hoy "Ingresar" pide el subdominio; se puede indexar los emails en el schema público.
- **Dominio propio del cliente** (`crm.acme.com`): es agregar otro `Dominio` al cliente + certificado.
- **Marca por cliente**: logo y colores propios (hoy se muestra el nombre de la empresa debajo del logo).
- **Email saliente por cliente**: hoy cada cliente puede cargar su SMTP en *Configuración de email*; si no, sale por el de la plataforma.
- **Alta asíncrona**: crear un cliente tarda ~10 s (migraciones del schema); con muchas migraciones conviene moverlo a Celery.
