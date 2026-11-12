# rw_dir (read-write)
The only writable directory in this CAP (declared in ../input_manifest.json).
On the reader it is /cust/rw_dir. custom_ui_cap stores here:
  config.json, config.json.bak   settings saved from the web UI
  auth.json                      web UI login (PBKDF2 hash)
  tls/                           per-reader self-signed HTTPS cert
  log/ui.log                     server log
  diag.txt                       startup diagnostics (python3/openssl/etc. availability)
Everything else under /cust is read-only; modifying it disables the CAP.
