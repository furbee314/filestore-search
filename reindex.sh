#!/bin/bash
# Reindex the file store. Run from cron or systemd timer.
#   * * * * * root /opt/filestore-search/reindex.sh >> /var/log/filestore-search-reindex.log 2>&1
set -euo pipefail
ROOT="${FILESTORE_SEARCH_ROOT:-/opt/filestore-search}"
cd "$ROOT"
/usr/bin/python3 -m cli refresh
