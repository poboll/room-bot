#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"
cd ..

if [ -n "${QIANGFANG_CA_BUNDLE:-}" ]; then
  export REQUESTS_CA_BUNDLE="$QIANGFANG_CA_BUNDLE"
  export SSL_CERT_FILE="$QIANGFANG_CA_BUNDLE"
fi

PYTHON_BIN="${QIANGFANG_PYTHON:-python3}"
exec "$PYTHON_BIN" app.py "$@"
