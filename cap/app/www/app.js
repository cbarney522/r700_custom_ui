/* R700 Connector settings UI — renders entirely from /api/schema. No external dependencies. */
(() => {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const el = (tag, attrs = {}, ...kids) => {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") n.className = v;
      else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else if (v === true) n.setAttribute(k, "");
      else if (v !== false && v != null) n.setAttribute(k, v);
    }
    for (const k of kids.flat()) if (k != null) n.append(k.nodeType ? k : document.createTextNode(k));
    return n;
  };

  let schema, saved, cfg, activeTab;

  async function api(method, path, body) {
    const res = await fetch(path, {
      method, credentials: "same-origin",
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
    let data = null;
    try { data = await res.json(); } catch (_) { /* empty */ }
    if (!res.ok) throw Object.assign(new Error((data && data.error) || res.statusText), { status: res.status });
    return data;
  }

  const clone = (o) => JSON.parse(JSON.stringify(o));
  const dirty = () => JSON.stringify(cfg) !== JSON.stringify(saved);
  function refreshDirty() {
    const d = dirty();
    $("#saveBtn").disabled = !d;
    $("#revertBtn").disabled = !d;
    document.title = (d ? "• " : "") + "R700 Connector Settings";
  }

  function toast(msg, kind = "ok") {
    const t = $("#toast");
    t.textContent = msg;
    t.className = "toast show " + kind;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => (t.className = "toast"), 3500);
  }

  function banner(html, kind = "warn") {
    $("#banners").append(el("div", { class: "banner " + kind }, html));
  }

  function defaults(fields) {
    const o = {};
    for (const f of fields) o[f.key] = clone(f.default);
    return o;
  }

  // ---------- field renderers
  function renderField(f, obj) {
    const id = "f_" + Math.random().toString(36).slice(2, 9);
    const set = (v) => { obj[f.key] = v; refreshDirty(); };
    const help = f.help ? el("div", { class: "help" }, f.help) : null;
    let input;

    switch (f.type) {
      case "select":
        input = el("select", { id, onchange: (e) => set(e.target.value) },
          f.options.map((o) => el("option", { value: o.value, selected: obj[f.key] === o.value }, o.label)));
        break;
      case "checkbox":
        return el("div", { class: "field check" },
          el("label", { for: id },
            el("input", { type: "checkbox", id, checked: !!obj[f.key], onchange: (e) => set(e.target.checked) }),
            el("span", {}, f.label)), help);
      case "multiselect": {
        const cur = new Set(obj[f.key] || []);
        const boxes = f.options.map((o) =>
          el("label", { class: "chip" },
            el("input", {
              type: "checkbox", value: o.value, checked: cur.has(o.value),
              onchange: (e) => {
                e.target.checked ? cur.add(o.value) : cur.delete(o.value);
                set(f.options.map((x) => x.value).filter((v) => cur.has(v)));
              },
            }),
            el("span", {}, o.label)));
        return el("fieldset", { class: "field" }, el("legend", {}, f.label), el("div", { class: "chips" }, boxes), help);
      }
      case "number":
        input = el("input", {
          type: "number", id, value: obj[f.key], min: f.min, max: f.max, step: f.step || "any",
          oninput: (e) => { const v = e.target.valueAsNumber; if (!Number.isNaN(v)) set(v); },
        });
        break;
      case "secret":
        input = el("input", {
          type: "password", id, value: obj[f.key] || "", maxlength: f.maxlength,
          placeholder: obj[f.key] ? "" : "not set", autocomplete: "new-password",
          onfocus: (e) => { if (e.target.value === "********") e.target.select(); },
          oninput: (e) => set(e.target.value),
        });
        break;
      default: // text, url
        input = el("input", {
          type: f.type === "url" ? "url" : "text", id, value: obj[f.key] ?? "", maxlength: f.maxlength,
          required: !!f.required, spellcheck: "false", oninput: (e) => set(e.target.value),
        });
    }
    return el("div", { class: "field" }, el("label", { for: id }, f.label), input, help);
  }

  function renderSection(sec) {
    if (sec.type !== "list") {
      return el("section", { class: "card" }, sec.fields.map((f) => renderField(f, cfg[sec.key])));
    }
    const items = cfg[sec.key];
    const wrap = el("div", { class: "list" });
    items.forEach((item, i) => {
      const title = item.name || `${sec.item_label} ${i + 1}`;
      wrap.append(el("section", { class: "card item" + (item.enabled === false ? " off" : "") },
        el("div", { class: "item-head" },
          el("h3", {}, title),
          el("button", {
            class: "btn ghost small", type: "button",
            onclick: () => { if (confirm(`Remove "${title}"?`)) { items.splice(i, 1); render(); refreshDirty(); } },
          }, "Remove")),
        el("div", { class: "grid" }, sec.fields.map((f) => renderField(f, item)))));
    });
    const full = items.length >= (sec.max_items || 16);
    wrap.append(el("button", {
      class: "btn add", type: "button", disabled: full,
      onclick: () => { items.push(defaults(sec.fields)); render(); refreshDirty(); },
    }, full ? `Maximum of ${sec.max_items} reached` : `+ Add ${sec.item_label.toLowerCase()}`));
    return wrap;
  }

  function render() {
    const tabs = $("#tabs");
    tabs.replaceChildren(...schema.sections.map((s) =>
      el("button", {
        type: "button", role: "tab", class: "tab" + (s.key === activeTab ? " active" : ""),
        "aria-selected": s.key === activeTab ? "true" : "false",
        onclick: () => { activeTab = s.key; render(); },
      }, s.label, s.type === "list" ? el("span", { class: "count" }, String(cfg[s.key].length)) : null)));
    const sec = schema.sections.find((s) => s.key === activeTab);
    $("#form").replaceChildren(el("h2", {}, sec.label), renderSection(sec));
  }

  function showMeta(c) {
    $("#rev").textContent = c.revision ?? 0;
    $("#saved").textContent = c.saved_at ? "saved " + new Date(c.saved_at).toLocaleString() : "never saved";
  }

  // ---------- actions
  async function save() {
    const btn = $("#saveBtn");
    btn.disabled = true;
    btn.textContent = "Saving…";
    try {
      const out = await api("PUT", "/api/config", cfg);
      saved = out; cfg = clone(out);
      showMeta(out); render(); toast("Settings saved to reader");
    } catch (e) {
      toast(e.status === 409 ? "Someone else saved first — reload to see their changes" : "Not saved: " + e.message, "err");
    } finally {
      btn.textContent = "Save";
      refreshDirty();
    }
  }

  function setupPassword() {
    const dlg = $("#pwDialog");
    $("#pwBtn").onclick = () => { $("#pwForm").reset(); $("#pwErr").textContent = ""; dlg.showModal(); };
    $("#pwSave").onclick = async (ev) => {
      ev.preventDefault();
      const cur = $("#pwCur").value, n1 = $("#pwNew").value, n2 = $("#pwNew2").value;
      if (n1.length < 8) return ($("#pwErr").textContent = "At least 8 characters.");
      if (n1 !== n2) return ($("#pwErr").textContent = "New passwords don't match.");
      try {
        await api("POST", "/api/password", { current: cur, new: n1 });
        dlg.close();
        toast("Password changed — your browser will ask you to sign in again");
        setTimeout(() => location.reload(), 1500);
      } catch (e) { $("#pwErr").textContent = e.message; }
    };
  }

  async function init() {
    try {
      const [s, c, st] = await Promise.all([api("GET", "/api/schema"), api("GET", "/api/config"), api("GET", "/api/status")]);
      schema = s; saved = c; cfg = clone(c);
      for (const sec of schema.sections) {
        if (sec.type === "list") cfg[sec.key] = cfg[sec.key] || [];
        else cfg[sec.key] = Object.assign(defaults(sec.fields), cfg[sec.key] || {});
      }
      saved = clone(cfg);
      activeTab = schema.sections[0].key;
      $("#title").textContent = schema.title;
      $("#host").textContent = st.hostname;
      showMeta(c);
      if (st.must_change_password) banner("You're using the default password. Change it before this reader goes into service.");
      if (!st.writable) banner(`Settings folder ${st.data_dir} isn't writable, so changes can't be saved.`, "err");
      render(); refreshDirty();
    } catch (e) {
      banner("Couldn't load settings: " + e.message, "err");
    }
    $("#saveBtn").onclick = save;
    $("#revertBtn").onclick = () => { cfg = clone(saved); render(); refreshDirty(); };
    setupPassword();
    window.addEventListener("beforeunload", (e) => { if (cfg && dirty()) { e.preventDefault(); e.returnValue = ""; } });
    document.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "s") { e.preventDefault(); if (dirty()) save(); }
    });
  }

  init();
})();
