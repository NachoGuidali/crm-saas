#!/bin/sh
# Backup diario de la base (todos los schemas/clientes) y los archivos. Cron sugerido (crontab -e):
#   15 4 * * * /opt/crm-ventas/deploy/backup.sh >> /var/log/crm-backup.log 2>&1
#
# Cifrado (A3): si BACKUP_PASSPHRASE está definida, los archivos se cifran con AES-256 (openssl). Sin passphrase
# quedan en claro (solo aceptable si el disco ya está cifrado y nunca salen del server).
# Copia afuera (A3): si RCLONE_DESTINO está definido, se sube con rclone (configurá antes `rclone config`).
# Restaurar: deploy/restore.sh (y probarlo de verdad una vez por mes: un backup sin restaurar no existe).
set -eu
cd "$(dirname "$0")/.."
DESTINO=${DESTINO:-/opt/backups/crm-ventas}
DIAS=${DIAS:-14}
FECHA=$(date +%Y%m%d-%H%M)
mkdir -p "$DESTINO"

cifrar() {  # cifra stdin → archivo $1 (.enc si hay passphrase), o lo deja en claro
    if [ -n "${BACKUP_PASSPHRASE:-}" ]; then
        openssl enc -aes-256-cbc -pbkdf2 -salt -pass env:BACKUP_PASSPHRASE -out "$1.enc"
        echo "$1.enc"
    else
        cat > "$1"
        echo "$1"
    fi
}

DB=$(docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' | cifrar "$DESTINO/db-$FECHA.dump")
MEDIA=$(docker compose exec -T web tar czf - -C /app media | cifrar "$DESTINO/media-$FECHA.tgz")

# Copia afuera del server (otra región / proveedor). Imprescindible: si se pierde el server, se pierde el backup local.
if [ -n "${RCLONE_DESTINO:-}" ]; then
    rclone copy "$DB" "$RCLONE_DESTINO" && rclone copy "$MEDIA" "$RCLONE_DESTINO"
    echo "$(date) copia remota OK: $RCLONE_DESTINO"
fi

find "$DESTINO" -type f -mtime +"$DIAS" -delete
echo "$(date) backup OK: $DB ; $MEDIA"
