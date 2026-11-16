#!/bin/sh
set -eu

case "${CONTEXT_GATEWAY_PUBLIC_PASSWORD}" in
  *[!A-Za-z0-9_-]*|'') echo "public database password must be non-empty URL-safe text" >&2; exit 1 ;;
esac

psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --set ON_ERROR_STOP=1 \
  --set public_password="$CONTEXT_GATEWAY_PUBLIC_PASSWORD" <<'SQL'
CREATE ROLE context_gateway_public LOGIN PASSWORD :'public_password';
GRANT CONNECT ON DATABASE context_gateway TO context_gateway_public;
GRANT USAGE ON SCHEMA public TO context_gateway_public;
SQL
