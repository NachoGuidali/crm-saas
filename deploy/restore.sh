#!/bin/sh
# Restaura un backup hecho con deploy/backup.sh. Uso:
#   deploy/restore.sh db-20260113-0415.dump[.enc] [media-20260113-0415.tgz[.enc]]
# Si los archivos terminan en .enc, definí BACKUP_PASSPHRASE (la misma del backup).
#
# OJO: pisa la base y los archivos actuales. Probalo en un server de staging, no en producción.
# Prueba mensual de restauración (A3): corré esto contra una base vacía y verificá que la app levanta.
set -eu
cd "$(dirname "$0")/.."
DB_FILE=${1:?Falta el archivo de base (db-*.dump o .enc)}
MEDIA_FILE=${2:-}

descifrar() {  # archivo (.enc o no) → stdout
    case "$1" in
        *.enc) openssl enc -d -aes-256-cbc -pbkdf2 -pass env:BACKUP_PASSPHRASE -in "$1" ;;
        *) cat "$1" ;;
    esac
}

echo ">> Restaurando base desde $DB_FILE"
descifrar "$DB_FILE" | docker compose exec -T db sh -c \
    'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner'

if [ -n "$MEDIA_FILE" ]; then
    echo ">> Restaurando archivos desde $MEDIA_FILE"
    descifrar "$MEDIA_FILE" | docker compose exec -T web tar xzf - -C /app
fi
echo ">> Listo. Revisá que la app levante y que un cliente pueda entrar."
