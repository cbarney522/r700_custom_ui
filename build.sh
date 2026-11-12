#!/bin/sh
# Build the CAP. Usage:  ETK_DIR=/opt/impinj/etk ./build.sh
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$HERE/dist"; STAGE="$OUT/stage"
rm -rf "$STAGE"; mkdir -p "$STAGE"
cp -a "$HERE/cust_app/." "$STAGE/"
find "$STAGE" -name '__pycache__' -prune -exec rm -rf {} +
rm -rf "$STAGE/data" "$STAGE/log"

# Fallback TLS cert, used only if the reader has no openssl for first-boot generation.
mkdir -p "$STAGE/app/tls"
openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 3650 \
  -keyout "$STAGE/app/tls/key.pem" -out "$STAGE/app/tls/cert.pem" -subj "/CN=r700-connector" 2>/dev/null
chmod 600 "$STAGE/app/tls/key.pem"

python3 -m py_compile "$STAGE/app/server.py" && rm -rf "$STAGE/app/__pycache__"
python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$STAGE/app/schema.json"

# Dev tarball (handy for bench testing on any Linux box)
tar -C "$STAGE" -czf "$OUT/r700-connector-cap.tgz" .

# >>> Package as .upgx with the ETK's CAP generator. Adjust the arguments to match
#     your ETK version's cap_gen usage (run it with -h).
if [ -n "${ETK_DIR:-}" ] && [ -x "$ETK_DIR/cap/cap_gen.sh" ]; then
  "$ETK_DIR/cap/cap_gen.sh" -d "$HERE/cap_description.in" -s "$STAGE" -o "$OUT/r700-connector.upgx"
  echo "Built $OUT/r700-connector.upgx"
else
  echo "ETK_DIR not set or cap_gen.sh not found — built $OUT/r700-connector-cap.tgz only"
fi
