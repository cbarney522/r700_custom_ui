#!/usr/bin/env python3
"""
R700 Enterprise Connector — configuration web server (runs inside the CAP).

Standard library only. Serves a schema-driven settings UI over HTTPS with
HTTP Basic auth, and persists settings to a JSON file on the reader.

  GET  /                  -> UI (www/index.html)
  GET  /api/schema        -> schema.json (drives the UI and server-side validation)
  GET  /api/config        -> current settings (secrets masked)
  PUT  /api/config        -> validate + atomically save settings (optimistic concurrency via "revision")
  GET  /api/status        -> app / storage / auth status
  POST /api/password      -> change the UI password
  GET  /api/export        -> settings file download (secrets masked)

Files in DATA_DIR:
  config.json       current settings            config.json.bak   previous settings
  auth.json         PBKDF2 password hash        tls/cert.pem, tls/key.pem
"""

import argparse
import base64
import copy
import hashlib
import hmac
import http.server
import json
import logging
import os
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

APP_DIR = os.path.dirname(os.path.abspath(__file__))
WWW_DIR = os.path.join(APP_DIR, "www")
SCHEMA_PATH = os.path.join(APP_DIR, "schema.json")
BUNDLED_TLS_DIR = os.path.join(APP_DIR, "tls")

MASK = "********"
DEFAULT_USER = "admin"
DEFAULT_PASSWORD = "changeme"
PBKDF2_ITERS = 120_000
MAX_BODY = 256 * 1024

log = logging.getLogger("connector-ui")
_lock = threading.Lock()


# ---------------------------------------------------------------- schema / defaults

def load_schema():
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def default_config(schema):
    cfg = {"schema_version": schema["schema_version"], "revision": 0, "saved_at": None}
    for sec in schema["sections"]:
        if sec.get("type") == "list":
            cfg[sec["key"]] = []
        else:
            cfg[sec["key"]] = {f["key"]: copy.deepcopy(f["default"]) for f in sec["fields"]}
    return cfg


class ValidationError(Exception):
    pass


def _validate_field(f, value, path):
    t = f["type"]
    if t in ("text", "secret", "url"):
        if not isinstance(value, str):
            raise ValidationError(f"{path}: must be text")
        if len(value) > f.get("maxlength", 1024):
            raise ValidationError(f"{path}: too long")
        if f.get("required") and not value.strip():
            raise ValidationError(f"{path}: required")
        if t == "url":
            u = urlparse(value)
            if u.scheme not in ("http", "https") or not u.netloc:
                raise ValidationError(f"{path}: must be an http(s) URL")
        return value
    if t == "select":
        allowed = [o["value"] for o in f["options"]]
        if value not in allowed:
            raise ValidationError(f"{path}: must be one of {allowed}")
        return value
    if t == "multiselect":
        allowed = {o["value"] for o in f["options"]}
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValidationError(f"{path}: must be a list")
        bad = [v for v in value if v not in allowed]
        if bad:
            raise ValidationError(f"{path}: unknown option(s) {bad}")
        # de-duplicate, keep schema order
        return [o["value"] for o in f["options"] if o["value"] in value]
    if t == "checkbox":
        if not isinstance(value, bool):
            raise ValidationError(f"{path}: must be true/false")
        return value
    if t == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationError(f"{path}: must be a number")
        if "min" in f and value < f["min"] or "max" in f and value > f["max"]:
            raise ValidationError(f"{path}: must be between {f.get('min')} and {f.get('max')}")
        return value
    raise ValidationError(f"{path}: unsupported field type {t}")


def _validate_group(fields, data, path, old=None):
    if not isinstance(data, dict):
        raise ValidationError(f"{path}: must be an object")
    known = {f["key"] for f in fields}
    extra = set(data) - known
    if extra:
        raise ValidationError(f"{path}: unknown key(s) {sorted(extra)}")
    out = {}
    for f in fields:
        key = f["key"]
        value = data.get(key, copy.deepcopy(f["default"]))
        # A masked secret means "unchanged" — keep what is already stored.
        if f["type"] == "secret" and value == MASK:
            value = (old or {}).get(key, "")
        out[key] = _validate_field(f, value, f"{path}.{key}")
    return out


def validate_config(schema, incoming, current):
    if not isinstance(incoming, dict):
        raise ValidationError("body must be a JSON object")
    out = {"schema_version": schema["schema_version"]}
    for sec in schema["sections"]:
        key = sec["key"]
        if sec.get("type") == "list":
            items = incoming.get(key, [])
            if not isinstance(items, list):
                raise ValidationError(f"{key}: must be a list")
            if len(items) > sec.get("max_items", 16):
                raise ValidationError(f"{key}: at most {sec.get('max_items', 16)} items")
            old_items = current.get(key, [])
            validated = []
            for i, item in enumerate(items):
                # Secrets are matched to the stored item by its hidden _id, not list position,
                # so reordering or deleting endpoints can't cross-wire credentials.
                item = dict(item) if isinstance(item, dict) else item
                item_id = item.pop("_id", None) if isinstance(item, dict) else None
                old = next((o for o in old_items if o.get("_id") == item_id), None) if item_id else None
                v = _validate_group(sec["fields"], item, f"{key}[{i}]", old)
                v["_id"] = item_id if old else secrets.token_hex(6)
                validated.append(v)
            out[key] = validated
        else:
            out[key] = _validate_group(sec["fields"], incoming.get(key, {}), key, current.get(key))
    return out


def mask_secrets(schema, cfg):
    cfg = copy.deepcopy(cfg)
    for sec in schema["sections"]:
        secret_keys = [f["key"] for f in sec["fields"] if f["type"] == "secret"]
        groups = cfg.get(sec["key"], []) if sec.get("type") == "list" else [cfg.get(sec["key"], {})]
        for g in groups:
            for k in secret_keys:
                if g.get(k):
                    g[k] = MASK
    return cfg


# ---------------------------------------------------------------- storage

class Store:
    def __init__(self, data_dir, schema):
        self.dir = data_dir
        self.schema = schema
        self.cfg_path = os.path.join(data_dir, "config.json")
        self.auth_path = os.path.join(data_dir, "auth.json")
        self.writable = self._probe_writable()

    def _probe_writable(self):
        try:
            os.makedirs(self.dir, exist_ok=True)
            probe = os.path.join(self.dir, ".write_probe")
            with open(probe, "w") as f:
                f.write("ok")
            os.remove(probe)
            return True
        except OSError as e:
            log.error("DATA_DIR %s is not writable: %s — running read-only", self.dir, e)
            return False

    def _atomic_write(self, path, obj, mode=0o600):
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, sort_keys=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        # fsync the directory so the rename itself survives power loss
        try:
            dfd = os.open(self.dir, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass

    def load_config(self):
        for p in (self.cfg_path, self.cfg_path + ".bak"):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                if p.endswith(".bak"):
                    log.warning("config.json unreadable; loaded backup")
                return cfg
            except FileNotFoundError:
                continue
            except (OSError, ValueError) as e:
                log.error("could not read %s: %s", p, e)
        return default_config(self.schema)

    def save_config(self, incoming, expected_revision):
        with _lock:
            current = self.load_config()
            if expected_revision is not None and expected_revision != current.get("revision", 0):
                return None, "conflict"
            new = validate_config(self.schema, incoming, current)
            new["revision"] = current.get("revision", 0) + 1
            new["saved_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            if os.path.exists(self.cfg_path):
                shutil.copy2(self.cfg_path, self.cfg_path + ".bak")
            self._atomic_write(self.cfg_path, new)
            return new, None

    # --- auth
    def load_auth(self):
        try:
            with open(self.auth_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def ensure_auth(self):
        if self.load_auth() is None:
            self.set_password(DEFAULT_USER, DEFAULT_PASSWORD, must_change=True)
            log.warning("created default login %s / %s — change it in the UI", DEFAULT_USER, DEFAULT_PASSWORD)

    def set_password(self, user, password, must_change=False):
        salt = secrets.token_bytes(16)
        h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERS)
        rec = {"user": user, "salt": salt.hex(), "hash": h.hex(), "iterations": PBKDF2_ITERS,
               "must_change": must_change}
        if self.writable:
            self._atomic_write(self.auth_path, rec)
        else:
            self._mem_auth = rec

    def check(self, user, password):
        rec = self.load_auth() or getattr(self, "_mem_auth", None)
        if not rec:
            return False
        h = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(rec["salt"]), rec["iterations"])
        return hmac.compare_digest(user, rec["user"]) & hmac.compare_digest(h.hex(), rec["hash"])


# ---------------------------------------------------------------- TLS

def ensure_tls(data_dir):
    """Per-reader self-signed cert generated on first boot; bundled cert as fallback."""
    tls_dir = os.path.join(data_dir, "tls")
    cert, key = os.path.join(tls_dir, "cert.pem"), os.path.join(tls_dir, "key.pem")
    if os.path.exists(cert) and os.path.exists(key):
        return cert, key
    openssl = shutil.which("openssl")
    if openssl:
        try:
            os.makedirs(tls_dir, exist_ok=True)
            cn = socket.gethostname() or "r700"
            subprocess.run(
                [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256", "-days", "3650",
                 "-keyout", key, "-out", cert, "-subj", f"/CN={cn}"],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
            os.chmod(key, 0o600)
            log.info("generated self-signed TLS cert for CN=%s", cn)
            return cert, key
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("openssl cert generation failed: %s", e)
    b_cert, b_key = os.path.join(BUNDLED_TLS_DIR, "cert.pem"), os.path.join(BUNDLED_TLS_DIR, "key.pem")
    if os.path.exists(b_cert) and os.path.exists(b_key):
        log.warning("using the build-time bundled TLS cert (same key on every reader built from this CAP)")
        return b_cert, b_key
    return None, None


# ---------------------------------------------------------------- HTTP

CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".ico": "image/x-icon",
                 ".json": "application/json"}

_failed = {}  # ip -> (count, last_ts) for simple login throttling


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "R700ConnectorUI/1.0"
    store: Store = None
    schema: dict = None

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.client_address[0], fmt % args)

    # --- helpers
    def _send(self, code, body=b"", ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _auth_ok(self):
        ip = self.client_address[0]
        count, last = _failed.get(ip, (0, 0))
        if count >= 5 and time.time() - last < 30:
            self._send(429, {"error": "too many failed logins; wait 30 s"})
            return False
        hdr = self.headers.get("Authorization", "")
        if hdr.startswith("Basic "):
            try:
                user, _, pw = base64.b64decode(hdr[6:]).decode().partition(":")
                if self.store.check(user, pw):
                    _failed.pop(ip, None)
                    return True
            except (ValueError, UnicodeDecodeError):
                pass
            _failed[ip] = (count + 1, time.time())
        self._send(401, {"error": "authentication required"},
                   extra={"WWW-Authenticate": 'Basic realm="R700 Connector", charset="UTF-8"'})
        return False

    def _read_json(self):
        n = int(self.headers.get("Content-Length", "0"))
        if n <= 0 or n > MAX_BODY:
            raise ValidationError("missing or oversized body")
        try:
            return json.loads(self.rfile.read(n))
        except ValueError:
            raise ValidationError("body is not valid JSON")

    def _same_origin(self):
        # Basic-auth credentials are sent automatically by browsers, so block cross-site writes.
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc != self.headers.get("Host"):
            self._send(403, {"error": "cross-origin request refused"})
            return False
        return True

    # --- verbs
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self._auth_ok():
            return
        path = urlparse(self.path).path
        if path == "/api/schema":
            return self._send(200, self.schema)
        if path == "/api/config":
            return self._send(200, mask_secrets(self.schema, self.store.load_config()))
        if path == "/api/export":
            name = f"connector-config-{socket.gethostname()}.json"
            return self._send(200, json.dumps(mask_secrets(self.schema, self.store.load_config()), indent=2),
                              extra={"Content-Disposition": f'attachment; filename="{name}"'})
        if path == "/api/status":
            auth = self.store.load_auth() or getattr(self.store, "_mem_auth", {})
            return self._send(200, {
                "hostname": socket.gethostname(),
                "data_dir": self.store.dir,
                "writable": self.store.writable,
                "must_change_password": bool(auth.get("must_change")),
                "revision": self.store.load_config().get("revision", 0),
                "python": sys.version.split()[0],
            })
        return self._static(path)

    def _static(self, path):
        if path in ("", "/"):
            path = "/index.html"
        rel = os.path.normpath(path.lstrip("/"))
        full = os.path.join(WWW_DIR, rel)
        if not os.path.abspath(full).startswith(os.path.abspath(WWW_DIR) + os.sep) or not os.path.isfile(full):
            return self._send(404, {"error": "not found"})
        with open(full, "rb") as f:
            data = f.read()
        return self._send(200, data, CONTENT_TYPES.get(os.path.splitext(full)[1], "application/octet-stream"))

    def do_PUT(self):
        if not self._auth_ok() or not self._same_origin():
            return
        if urlparse(self.path).path != "/api/config":
            return self._send(404, {"error": "not found"})
        if not self.store.writable:
            return self._send(503, {"error": f"settings directory {self.store.dir} is not writable"})
        try:
            body = self._read_json()
            rev = body.pop("revision", None) if isinstance(body, dict) else None
            for k in ("schema_version", "saved_at"):
                body.pop(k, None) if isinstance(body, dict) else None
            saved, err = self.store.save_config(body, rev)
        except ValidationError as e:
            return self._send(400, {"error": str(e)})
        except OSError as e:
            log.exception("save failed")
            return self._send(500, {"error": f"write failed: {e}"})
        if err == "conflict":
            return self._send(409, {"error": "settings were changed by someone else; reload and try again"})
        log.info("settings saved, revision %s", saved["revision"])
        return self._send(200, mask_secrets(self.schema, saved))

    def do_POST(self):
        if not self._auth_ok() or not self._same_origin():
            return
        if urlparse(self.path).path != "/api/password":
            return self._send(404, {"error": "not found"})
        try:
            body = self._read_json()
            cur, new = body.get("current", ""), body.get("new", "")
            auth = self.store.load_auth() or getattr(self.store, "_mem_auth", {})
            if not self.store.check(auth.get("user", DEFAULT_USER), cur):
                return self._send(403, {"error": "current password is incorrect"})
            if not isinstance(new, str) or len(new) < 8 or new == DEFAULT_PASSWORD:
                return self._send(400, {"error": "new password must be at least 8 characters"})
            self.store.set_password(auth.get("user", DEFAULT_USER), new, must_change=False)
        except ValidationError as e:
            return self._send(400, {"error": str(e)})
        return self._send(200, {"ok": True})


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    ap = argparse.ArgumentParser(description="R700 Connector configuration UI")
    ap.add_argument("--port", type=int, default=int(os.environ.get("UI_PORT", "8443")))
    ap.add_argument("--bind", default=os.environ.get("UI_BIND", "0.0.0.0"))
    ap.add_argument("--data-dir", default=os.environ.get("CAP_DATA_DIR",
                                                           os.path.join(os.path.dirname(APP_DIR), "data")))
    ap.add_argument("--insecure-http", action="store_true", help="serve plain HTTP (bench use only)")
    ap.add_argument("--log-level", default=os.environ.get("UI_LOG_LEVEL", "INFO"))
    args = ap.parse_args()

    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    schema = load_schema()
    store = Store(args.data_dir, schema)
    store.ensure_auth()

    Handler.store, Handler.schema = store, schema
    httpd = Server((args.bind, args.port), Handler)

    scheme = "http"
    if not args.insecure_http:
        cert, key = ensure_tls(args.data_dir)
        if not cert:
            log.critical("no TLS certificate available (no openssl, no bundled cert). "
                         "Rebuild the CAP with build.sh or start with --insecure-http.")
            sys.exit(2)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(cert, key)
        # Handshake lazily in the per-request thread so one slow client can't stall accept().
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True, do_handshake_on_connect=False)
        scheme = "https"

    log.info("serving %s://%s:%d/  data_dir=%s writable=%s", scheme, args.bind, args.port, store.dir, store.writable)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
