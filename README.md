# R700 Connector: CAP with a web configuration UI

This is a CAP for the Impinj R700v2 (FW 10.x). It runs a small HTTPS web server on the reader. The server gives you a settings page with dropdowns, checkboxes and a list of endpoints, and it saves your choices to a JSON file on the reader.

```
cap/
  build.sh               packages cust_app/ → dist/*.tgz, and .upgx when ETK_DIR is set
  cap_description.in     CAP metadata (TEMPLATE: match your ETK's sample)
  cust_app/
    start / stop         boot entry point + supervisor loop (auto-restart)
    app.env              port / data-dir overrides
    app/server.py        Python 3 standard library only: HTTPS, Basic auth, REST API, validation
    app/schema.json      single source of truth: drives the UI AND server-side validation
    app/www/             index.html, app.js, style.css (no CDN; works on an air-gapped plant LAN)
```

![Endpoints tab](docs/ui-endpoints-screenshot.png)

## How it works
- **Browser → `https://<reader-ip>:8443/`** You log in with HTTP Basic auth. The first login is `admin` / `changeme`, and the page shows a banner until you change it.
- **The schema drives everything.** To add a dropdown or checkbox, add a field to `schema.json`. You don't have to change the UI or the server code. Supported field types: `select`, `multiselect` (checkbox chips), `checkbox`, `number`, `text`, `url`, `secret`, plus `list` sections for repeating items such as endpoints.
- **Saving (`PUT /api/config`)** The server checks every value against the schema and rejects unknown keys. It writes `config.json.tmp`, runs fsync, renames the file over `config.json` and fsyncs the directory, so a power cut never leaves a half-written file. It keeps the previous version as `config.json.bak`, and if `config.json` can't be read it falls back to the `.bak`. Each save increments `revision`. If two people edit at once, the second save gets a 409 conflict instead of silently overwriting the first.
- **Secrets** (endpoint tokens) stay on the reader. The browser only ever sees `********`. If a save sends back the mask, the server keeps the stored value. Secrets are matched to their endpoint by a hidden `_id`, so reordering or deleting endpoints can't attach a token to the wrong endpoint.
- **TLS** On first boot the server generates a self-signed cert for that reader, if `openssl` exists on the reader. If it doesn't, the server falls back to a cert that `build.sh` bundles into the CAP. That bundled cert is the same on every reader built from that CAP, so treat it as a stopgap.
- **Other hardening** A write from another site is refused (Origin check). Five wrong passwords lock that address out for 30 s. Passwords are stored as PBKDF2-SHA256 hashes. The page has a strict CSP.

## Build and deploy
```sh
cd cap
ETK_DIR=/path/to/impinj/etk ./build.sh     # → dist/r700-connector.upgx  (+ dist/r700-connector-cap.tgz)
```
Upload the `.upgx` in the reader's web UI (the same page you use for firmware upgrades), reboot, then open `https://<reader>:8443/`.

Bench test on any Linux box, without a reader:
```sh
python3 cap/cust_app/app/server.py --data-dir ./devdata --port 8443   # add --insecure-http to skip TLS
```

## ⚠ Check these against the R700 ETK developer guide (it's behind the Impinj support portal)
1. **`cap_description.in` fields and `cap_gen` arguments.** Both files are placeholders marked `>>>`.
2. **Entry point.** Check that the reader runs `start` in the CAP root at boot, or rename it to whatever the ETK expects.
3. **Persistent writable directory.** The default is `<CAP root>/data`. If the CAP image is mounted read-only, set `CAP_DATA_DIR` in `app.env` to the writable path the ETK documents. If the directory can't be written, the UI shows a red banner and refuses to save. It never silently writes somewhere that won't survive a reboot.
4. **Python 3 in the FW 10.x CAP runtime.** The server needs only the standard library, Python 3.7 or newer.
5. **Port 8443 reachable through the reader's firewall.** Pick a port that doesn't clash with the reader's own web UI (80/443) or the IoT Device Interface.

## Using the settings from the connector process
Your worker (the process that reads tags and posts to the APIs) reads the same file. It can poll the file's modification time, or `revision`, to pick up changes without a restart:
```python
import json, os
PATH = os.path.join(os.environ.get("CAP_DATA_DIR", "/cust_app/data"), "config.json")
_last = None
def settings_if_changed():
    global _last
    m = os.stat(PATH).st_mtime_ns
    if m != _last:
        _last = m
        with open(PATH) as f:
            return json.load(f)     # atomic rename means you never read a partial file
```

## Next steps
- Add a "Test endpoint" button that sends a sample payload from the reader and shows the HTTP status.
- Add a GPIO/LED status indicator and reader info (serial, FW, antenna hub) on a Status tab.
- Import settings from a file, so one exported config can be applied across a fleet.
