#!/bin/bash

set -e

FILE="$1"

if [ -z "$FILE" ]; then
  echo "Usage: ./scripts/restore.sh backup.sql.gz"
  exit 1
fi

gunzip -c "$FILE" | \
docker compose exec -T postgres \
psql \
  -U "${POSTGRES_USER:-cse}" \
  "${POSTGRES_DB:-cse_scheduler}"

echo "Restore complete."
