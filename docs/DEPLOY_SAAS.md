# Puesta en producción — SaaS multi-cliente (supregcrm.com)

Una sola instalación atiende a todos los clientes. El panel vive en `supregcrm.com` y cada cliente en su
subdominio (`acme.supregcrm.com`). Arquitectura y operación diaria: ver `SAAS.md`.

Requisitos del servidor: Docker + Docker Compose, nginx y certbot. VPS sugerido para empezar: 2 vCPU / 4 GB.

---

## 1. Dominio y DNS

1. Comprar **supregcrm.com**.
2. Dos registros A apuntando a la IP del servidor (**un solo comodín** cubre a todos los clientes):
   ```
   A   supregcrm.com       → IP_DEL_SERVIDOR
   A   *.supregcrm.com     → IP_DEL_SERVIDOR
   ```
   Dar de alta un cliente nuevo después **no** requiere tocar el DNS.

## 2. Certificado comodín (HTTPS)

El certificado tiene que ser comodín (`*.supregcrm.com`) y eso exige validación por DNS. Con el dominio en
**Cloudflare** (gratis cubre un nivel de comodín):
```bash
apt install certbot python3-certbot-dns-cloudflare
# /root/.cloudflare.ini con el token de API de Cloudflare (chmod 600)
certbot certonly --dns-cloudflare --dns-cloudflare-credentials /root/.cloudflare.ini \
    -d supregcrm.com -d '*.supregcrm.com'
```
Renovación automática: `certbot renew` ya queda en cron/systemd.

## 3. Proyecto y variables

```bash
git clone <repo> /opt/crm-saas      # o copiar la carpeta
cd /opt/crm-saas
cp .env.saas .env                   # ya viene con supregcrm.com y secretos generados
```
Revisar en `.env`: `ADMIN_PASSWORD` (anotarla), `SAAS_DATOS_PAGO` (tu alias/CBU) y, cuando los tengas,
el SMTP de email y las claves de Mercado Pago. **No cambiar `DJANGO_SECRET_KEY`** después (de ella se derivan
las claves que cifran las credenciales de integraciones).

## 4. nginx

```bash
cp deploy/nginx-saas.conf /etc/nginx/sites-available/crm-saas
ln -s /etc/nginx/sites-available/crm-saas /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
```
El bloque ya trae `server_name supregcrm.com *.supregcrm.com` y manda el `Host` al backend (imprescindible:
con el Host se resuelve de qué cliente es cada request). nginx escucha en 127.0.0.1:8000 (el `WEB_PORT`).

## 5. Levantar

```bash
docker compose up -d --build
```
Al arrancar, el servicio `web` corre solo:
- `migrate_schemas --noinput` (schema público + todos los clientes),
- `setup_plataforma` (dominio principal `supregcrm.com` + superusuario del panel con `ADMIN_*`).

En el **primer** arranque los `worker`/`beat` pueden reiniciarse una o dos veces hasta que `web` termina de
migrar (todavía no existen las tablas); se estabilizan solos. Verificar:
```bash
docker compose ps           # todo "running"
docker compose logs web | tail -30
```

## 6. Primer uso

1. Entrar a **https://supregcrm.com/panel/** con `admin` / el `ADMIN_PASSWORD` del `.env`. Cambiar la
   contraseña desde *Mi perfil*.
2. Cargar los **planes** (hay ejemplos cargables; definir precio, usuarios, líneas y WhatsApp/día).
3. **Nuevo cliente**: empresa, subdominio, plan y administrador. Se crea su schema aislado y su CRM queda en
   `https://<subdominio>.supregcrm.com`. Las credenciales del admin se muestran una sola vez.

## 7. Backups (apenas haya datos reales)

`deploy/backup.sh` hace pg_dump de todos los schemas + los archivos, **cifrado** con `BACKUP_PASSPHRASE`.
Para copia afuera del server, definir `RCLONE_DESTINO` (tras `rclone config`). Cron sugerido:
```bash
15 4 * * * /opt/crm-saas/deploy/backup.sh >> /var/log/crm-backup.log 2>&1
```
Restaurar: `deploy/restore.sh db-AAAA...dump.enc media-AAAA...tgz.enc`. **Probar la restauración una vez por mes.**

## 8. Afinar después del primer día

- **HSTS**: con el HTTPS andando estable, poner `SECURE_HSTS_SECONDS=31536000` en el `.env` y `docker compose up -d`.
- **Email**: cargar el SMTP (Gmail con contraseña de aplicación, o Brevo/SES) y pasar `EMAIL_BACKEND` a `smtp`.
  Sin esto, el recupero de contraseña y los avisos de vencimiento no salen (quedan en consola).
- **Mercado Pago** (opcional): `MP_ACCESS_TOKEN` + `MP_WEBHOOK_SECRET`, y en MP el webhook
  `https://supregcrm.com/mercadopago/webhook/`.
- **WhatsApp por QR (Evolution)**: solo si lo vas a ofrecer vos. Levantarlo con el perfil y darle su subdominio:
  ```bash
  docker compose --profile evolution up -d
  ```
  Requiere `evolution.supregcrm.com` en nginx (bloque aparte) y su propio certificado o el comodín.

## 9. Actualizaciones

```bash
cd /opt/crm-saas && git pull
docker compose up -d --build      # web vuelve a correr migrate_schemas en todos los clientes
```
Un deploy ahora afecta a **todos** los clientes a la vez: probar en staging antes si el cambio es grande.
