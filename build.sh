#!/bin/bash
# Build custom_ui_cap_<version>.upgx with the Impinj ETK's cap_gen.sh.
#   ./build.sh                      (ETK defaults to ~/9.1.0_Octane_Embedded_Development_Tools)
#   ETK_DIR=/path/to/etk ./build.sh
#   SIGN_KEY_DIR=/path/to/keys ./build.sh   (optional: passes -k to cap_gen.sh)
set -euo pipefail
cd "$(dirname "$0")"
ETK_DIR="${ETK_DIR:-$HOME/9.1.0_Octane_Embedded_Development_Tools}"
CAP_GEN="$ETK_DIR/cap_gen.sh"
[ -x "$CAP_GEN" ] || { echo "cap_gen.sh not found at $CAP_GEN (set ETK_DIR)"; exit 1; }

VERSION="$(sed -n 's/^[[:space:]]*Version[[:space:]]*=[[:space:]]*//p' cap_description.in | tr -d '[:space:]')"
OUT="custom_ui_cap_${VERSION}.upgx"

# Sanity checks before packaging
python3 -m py_compile cap/app/server.py && rm -rf cap/app/__pycache__
python3 -c "import json,sys; [json.load(open(p)) for p in sys.argv[1:]]" cap/app/schema.json cap/input_manifest.json
bash -n cap/start
chmod +x cap/start cap/app/server.py

# Ship no runtime data: rw_dir starts empty except its README
find cap/rw_dir -mindepth 1 ! -name README.md -exec rm -rf {} +

# Fallback TLS cert, used only if the reader has no openssl (rebuilt each build)
mkdir -p cap/app/tls
openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 3650 \
  -keyout cap/app/tls/key.pem -out cap/app/tls/cert.pem -subj "/CN=custom_ui_cap" 2>/dev/null
chmod 600 cap/app/tls/key.pem

ARGS=(-d cap_description.in -o "$OUT")
[ -n "${SIGN_KEY_DIR:-}" ] && ARGS+=(-k "$SIGN_KEY_DIR")
"$CAP_GEN" "${ARGS[@]}"
ls -l "$OUT"
echo "Upload $OUT in the reader's web UI, then browse to https://<reader>:8443/"
