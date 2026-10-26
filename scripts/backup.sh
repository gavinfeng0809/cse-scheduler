#!/bin/bash

set -e

mkdir -p backups

DATE=$(date +"%Y%m%d_%H%M%S")

docker compose exec -T postgres \
  pg_dump \
  -U "${POSTGRES_USER:-cse}" \
  "${POSTGRES_DB:-cse_scheduler}" \
  > "backups/cse_${DATE}.sql"

gzip "backups/cse_${DATE}.sql"

echo "Backup complete:"
echo "backups/cse_${DATE}.sql.gz"
