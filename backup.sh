#!/usr/bin/env bash
# Copy the database and encrypted PDFs to backups/<timestamp>/.
# The encryption key (storage/fernet.key) is copied too: in a real pilot, store the
# key backup somewhere different from the data backup (e.g. a USB kept by the director).
set -euo pipefail
cd "$(dirname "$0")"

dest="backups/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$dest"

if command -v sqlite3 >/dev/null 2>&1; then
  sqlite3 app.db ".backup '$dest/app.db'"   # consistent copy even while the app runs
else
  cp app.db "$dest/app.db"
fi
cp -R storage "$dest/storage"

echo "Backup listo en $dest"
