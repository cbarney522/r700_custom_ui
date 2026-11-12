#!/bin/bash
# Build custom_ui_cap_<version>.upgx with the Impinj ETK's cap_gen.sh.
#   ./build.sh                      (ETK defaults to /opt/10.3.0_Octane_Embedded_Development_Tools)
#   ETK_DIR=/path/to/etk ./build.sh
#   SIGN_KEY_DIR=/path/to/keys ./build.sh   (optional: passes -k to cap_gen.sh)
set -euo pipefail
cd "$(dirname "$0")"
ETK_DIR="${ETK_DIR:-/opt/10.3.0_Octane_Embedded_Development_Tools}"   # FW 10.x needs ETK >= 10.3.0
CAP_GEN="$ETK_DIR/cap_gen.sh"
[ -x "$CAP_GEN" ] || { echo "cap_gen.sh not found at $CAP_GEN (set ETK_DIR)"; exit 1; }

VERSION="$(sed -n 's/^[[:space:]]*Version[[:space:]]*=[[:space:]]*//p' cap_description.in | tr -d '[:space:]')"
OUT="custom_ui_cap_${VERSION}.upgx"

# Sanity checks before packaging
python3 -m py_compile cap/app/server.py && rm -rf cap/app/__pycache__
python3 -c "import json,sys; [json.load(open(p)) for p in sys.argv[1:]]" cap/app/schema.json cap/input_manifest.json
bash -n cap/start
chmod +x cap/start cap/app/server.py

# ---- Bundled Python runtime -> cap/python (downloaded once, cached, checksum-verified)
. ./python-runtime.conf
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/custom_ui_cap"
mkdir -p "$CACHE"
TARBALL="$CACHE/$PY_ASSET"
if [ ! -f "$TARBALL" ]; then
  echo "Downloading Python runtime $PY_ASSET ..."
  curl -fSL --retry 3 -o "$TARBALL.part" \
    "https://github.com/astral-sh/python-build-standalone/releases/download/$PY_RELEASE/${PY_ASSET//+/%2B}"
  mv "$TARBALL.part" "$TARBALL"
fi
echo "$PY_SHA256  $TARBALL" | sha256sum -c --quiet - || { echo "Python runtime checksum mismatch; delete $TARBALL and retry"; exit 1; }
if [ ! -f cap/python/.asset ] || [ "$(cat cap/python/.asset)" != "$PY_ASSET" ]; then
  rm -rf cap/python && mkdir -p cap/python.tmp
  tar -xzf "$TARBALL" -C cap/python.tmp
  mv cap/python.tmp/python cap/python && rmdir cap/python.tmp
  # Prune what a headless web server never uses (~88 MB -> ~45 MB)
  L=cap/python/lib/python3.12
  rm -rf cap/python/include cap/python/share cap/python/lib/pkgconfig \
         cap/python/lib/tcl* cap/python/lib/tk* cap/python/lib/itcl* cap/python/lib/thread* \
         cap/python/lib/libtcl* \
         $L/idlelib $L/tkinter $L/turtledemo $L/turtle.py $L/ensurepip $L/lib2to3 \
         $L/test $L/unittest/test $L/site-packages/pip* $L/config-3.12-* \
         $L/lib-dynload/_tkinter*
  echo "$PY_ASSET" > cap/python/.asset
fi
python3 -c "import sys; h=open(sys.argv[1],'rb').read(20); sys.exit(0 if h[:4]==b'\x7fELF' and int.from_bytes(h[18:20],'little')==183 else 1)" \
  "$(readlink -f cap/python/bin/python3)" || { echo "cap/python is not an aarch64 Python"; exit 1; }
# Python needs libgcc_s.so.1. The reader very likely has it; as insurance, bundle the ETK
# toolchain's aarch64 copy next to libpython (found via Python's RPATH $ORIGIN/../lib).
if [ ! -f cap/python/lib/libgcc_s.so.1 ]; then
  GCC_S="$(find "$ETK_DIR" -name libgcc_s.so.1 -path '*aarch64*' 2>/dev/null | head -1)"
  if [ -n "$GCC_S" ]; then cp -L "$GCC_S" cap/python/lib/ && echo "Bundled libgcc_s from $GCC_S"
  else echo "Note: no aarch64 libgcc_s.so.1 in the ETK; relying on the reader's copy"; fi
fi
echo "Bundled Python: $(du -sh cap/python | cut -f1)"

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
