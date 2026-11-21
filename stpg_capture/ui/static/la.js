"use strict";

// ---------------------------------------------------------------------------
// The logic-analyzer tool's page (its own server, port and token; the STPG Runner never loads it).
// Bindings, Identify, capture plans, offline captures, marker patterns, the accessory config for an
// STPG run, and capture results with their waveform. Nothing runs on page load except reading lists.
// ---------------------------------------------------------------------------

const TOKEN = new URLSearchParams(location.search).get("token") || "";
const S = { state: null, analyzers: [], bundle: null, plan: null, capture: null };
const $ = (id) => document.getElementById(id);

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (key === "class") node.className = value;
    else if (value === true) node.setAttribute(key, "");
    else node.setAttribute(key, value);
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : String(child));
  }
  return node;
}

function setChildren(node, ...children) {
  // replaceChildren would print null/undefined/false as text: drop them (and flatten nested lists)
  node.replaceChildren(...children.flat(Infinity).filter((child) => child !== null && child !== undefined && child !== false));
}

async function api(name, body) {
  const response = await fetch(`/api/${name}`, {
    method: body === undefined ? "GET" : "POST",
    headers: { "X-LA-Token": TOKEN, "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({ error: `HTTP ${response.status}` }));
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

function setStatus(text, bad) {
  const node = $("status");
  node.textContent = text || "";
  node.className = bad ? "status bad" : "status";
}

async function busyWhile(button, work) {
  if (button) { button.disabled = true; button.classList.add("busy"); }
  setStatus("Working…");
  try {
    const result = await work();
    setStatus("");
    return result;
  } catch (error) {
    setStatus(error.message, true);
    return null;
  } finally {
    if (button) { button.disabled = false; button.classList.remove("busy"); }
  }
}

const badge = (text, kind) => el("span", { class: `badge ${kind || "muted"}` }, text);
function notice(kind, title, lines = []) {
  return el("div", { class: `notice ${kind || ""}` }, title ? el("strong", {}, title) : null,
    lines.length ? el("ul", {}, lines.map((line) => el("li", {}, line))) : null);
}
function kvList(rows) {
  return el("dl", { class: "kv" }, rows.filter(Boolean).map(([k, v]) => [el("dt", {}, k), el("dd", {}, v ?? "—")]));
}
function table(headers, rows) {
  return el("table", {}, el("thead", {}, el("tr", {}, headers.map((h) => el("th", {}, h)))),
    el("tbody", {}, rows.map((row) => el("tr", {}, row.map((cell) => el("td", {}, cell ?? "—"))))));
}
const copyable = (text) => el("span", { class: "path", title: "select to copy" }, text);
const hex4 = (v) => (v ?? 0).toString(16).padStart(4, "0");
function fmtTime(ps) {
  if (ps === null || ps === undefined) return "—";
  const a = Math.abs(ps);
  if (a >= 1e9) return `${(ps / 1e9).toFixed(3)} ms`;
  if (a >= 1e6) return `${(ps / 1e6).toFixed(3)} µs`;
  if (a >= 1e3) return `${(ps / 1e3).toFixed(1)} ns`;
  return `${ps} ps`;
}
const intOrNull = (text) => (String(text ?? "").trim() === "" ? null : Number(text));

// ---- path picker ------------------------------------------------------------
// Existing files and folders are chosen from a listing. A new output folder is a folder that exists plus a new
// name: nothing is created until the tool writes into it, and the writer still refuses a folder that exists by then.
function pickPath({ title, dir = false, start = "", create = false }) {
  const dialog = $("pickDialog");
  $("pickTitle").textContent = title || (create ? "Choose where the new folder goes" : dir ? "Choose a folder" : "Choose a file");
  $("pickHint").textContent = create ? "Open the folder that will hold it, then type the new folder's name. Nothing is created now."
    : dir ? "Open the folder and press Use this path." : "Pick the file, or type its path.";
  $("pickNewRow").hidden = !create;
  $("pickNewName").value = "";
  $("pickOk").textContent = create ? "Use this new folder" : "Use this path";
  const error = (text) => { $("pickError").textContent = text || ""; $("pickError").hidden = !text; };
  let here = null;
  const show = async (path) => {
    error("");
    try {
      const data = await api("ls", { path });
      here = data.path;
      $("pickHere").textContent = data.path;
      $("pickPath").value = path && !dir && !create ? path : data.path;
      $("pickUp").onclick = () => show(data.parent);
      $("pickHome").onclick = () => show(data.home);
      setChildren($("pickRoots"), ...(data.roots || []).map((root) =>
        el("button", { class: "btn small", type: "button", onclick: () => show(root) }, root)));
      setChildren($("pickList"), ...data.entries.filter((e) => e.dir || (!dir && !create)).map((e) =>
        el("li", { class: e.dir ? "dir" : "", onclick: () => (e.dir ? show(e.path) : ($("pickPath").value = e.path)) }, e.name)));
    } catch (problem) { error(problem.message); }  // no other folder is opened instead
  };
  $("pickGo").onclick = () => show($("pickPath").value.trim());
  $("pickPath").onkeydown = (event) => { if (event.key === "Enter") { event.preventDefault(); show($("pickPath").value.trim()); } };
  // Enter in the name box confirms (the form's first button is Cancel, which a plain Enter would press)
  $("pickNewName").onkeydown = (event) => { if (event.key === "Enter") { event.preventDefault(); $("pickOk").click(); } };
  let chosen = null;
  $("pickOk").onclick = async (event) => {
    if (!create) { chosen = $("pickPath").value.trim(); return; }
    event.preventDefault();  // checked first: the parent must exist and the name must be new and usable
    try {
      chosen = (await api("newfolder", { parent: here, name: $("pickNewName").value })).path;
      dialog.close("ok");
    } catch (problem) { error(problem.message); }
  };
  show(start);
  dialog.showModal();
  return new Promise((resolve) => {
    dialog.onclose = () => resolve(dialog.returnValue === "ok" && chosen ? chosen : null);
  });
}
document.querySelectorAll("[data-browse]").forEach((button) => button.addEventListener("click", async () => {
  const target = $(button.dataset.browse);
  const create = !!button.dataset.new;
  // a new folder starts in the folder that would hold it
  const start = create && target.value.trim() ? target.value.trim().replace(/[\\/][^\\/]*[\\/]?$/, "") : target.value;
  const chosen = await pickPath({ dir: !!button.dataset.dir, start, create });
  if (chosen) target.value = chosen;
}));

// ---- 1. analyzer bindings ------------------------------------------------------
async function loadAnalyzers() {
  S.state = await api("state");  // the settings folder may have changed state (new, empty, in use)
  renderSettingsFolder();
  const data = await api("analyzers");
  S.analyzers = data.items;
  renderBindings(data.folder);
  renderAnalyzerChoice();
}

function renderSettingsFolder() {
  const c = S.state.config;
  setChildren($("settingsFolder"),
    el("p", { class: "muted small" }, "Settings folder: ", copyable(c.path), ` (${c.source_text}). ${c.text[0].toUpperCase()}${c.text.slice(1)}.`),
    c.problem ? notice("bad", "Settings folder problem", [c.problem, "No other folder is used instead. Fix it, or restart with --config-dir and a folder you can use."]) : null,
    c.notes.length ? notice("warn", "Settings folder", c.notes) : null,
    el("p", { class: "muted small" }, c.state === "new" || c.state === "empty"
      ? "The STPG Runner must show the same folder. Have bindings from an earlier setup? Start with --config-dir and that folder, or set STPG_CONFIG_DIR. Nothing is moved or copied for you."
      : "The STPG Runner must show the same folder; if it does not, start both with the same --config-dir or STPG_CONFIG_DIR."));
}

function wiringView(item) {
  const rows = (item.wiring_rows || []).filter((row) => row.analyzer_gpio !== null);
  const missing = (item.wiring_rows || []).filter((row) => row.analyzer_gpio === null).map((row) => `GPIO${row.stpg_gpio}`);
  setChildren($("identifyBody"), el("h3", {}, `Wiring of ${item.name}`),
    table(["STPG GPIO", "STPG bit", "analyzer GPIO", "analyzer channel"], rows.map((row) => [row.stpg_gpio, row.stpg_bit, row.analyzer_gpio, `ch${row.analyzer_channel}`])),
    missing.length ? notice("warn", "Not observed with this wiring", [`${missing.join(", ")}: no analyzer input, so a capture cannot see them.`]) : null,
    notice("warn", "Before capturing", [
      "Connect the STPG and analyzer grounds.",
      "A pattern trigger also needs analyzer GPIO0 wired to analyzer GPIO1 (the firmware signals a match between them). Level and marker triggers do not use them.",
      "Those two analyzer pins carry no signal input: do not wire STPG outputs to them."]));
}

function limitsName(limits) {
  const match = Object.entries(S.state.limits).find(([, v]) => JSON.stringify(v) === JSON.stringify(limits));
  return match ? match[0] : null;
}

function renderBindings(folder) {
  const body = $("bindingsBody");
  const rows = S.analyzers.map((a) => {
    const c = a.config;
    return [el("strong", {}, a.name), c ? c.port : "—", c ? `${hex4(c.vid)}:${hex4(c.pid)}` : "—", c ? c.serial_number : a.error,
      c ? (limitsName(c.limits) || c.limits.id_line) : "—", c ? (typeof c.wiring === "string" ? c.wiring : "explicit map") : "—",
      c && c.marker_gpio !== undefined ? `GPIO${c.marker_gpio}` : "—",
      el("span", { class: "row" },
        el("button", { class: "btn small", type: "button", disabled: !c || (!S.state.device_io && !a.simulated),
          title: "Opens only this analyzer's port, reads its identity and closes it; evidence is saved first",
          onclick: (e) => identify(a.name, e.target) }, "Identify"),
        c ? el("button", { class: "btn small", type: "button", onclick: () => wiringView(a) }, "Wiring") : null,
        a.simulated ? null : el("button", { class: "btn small", type: "button", onclick: () => editBinding(a) }, "Edit"),
        a.simulated ? null : el("button", { class: "btn small danger", type: "button", onclick: async (e) => {
          if (!confirm(`Delete the analyzer binding ${a.name}?`)) return;
          await busyWhile(e.target, () => api("analyzer/delete", { name: a.name }));
          await loadAnalyzers();
        } }, "Delete"))];
  });
  setChildren(body, 
    rows.length ? table(["name", "port", "vid:pid", "serial", "firmware limits", "wiring", "default marker", ""], rows)
      : el("p", { class: "muted" }, `No analyzer binding yet (${folder} has none).`),
    el("div", { class: "actions" }, el("button", { class: "btn", type: "button", onclick: () => editBinding(null) }, "Add binding…")),
    el("div", { id: "bindingForm" }), el("div", { id: "identifyBody" }));
}

async function scanPorts(target, form) {
  setChildren(target, el("p", { class: "muted small" }, "Scanning (USB metadata only, no port is opened)…"));
  try {
    const data = await api("ports", {});
    const use = (p) => {
      form.elements.port.value = p.device;
      form.elements.vid.value = p.vid !== null && p.vid !== undefined ? `0x${hex4(p.vid)}` : "";
      form.elements.pid.value = p.pid !== null && p.pid !== undefined ? `0x${hex4(p.pid)}` : "";
      form.elements.serial_number.value = p.serial_number || "";
    };
    setChildren(target,
      data.error ? notice("warn", "Port listing", [data.error]) : null,
      data.real_scan_performed === false ? notice("warn", "No real port scan", ["Device access is disabled (--no-device-io), so USB metadata is not read."]) : null,
      data.ports.length ? table(["port", "vid:pid", "serial", "description", ""], data.ports.map((p) => [p.device,
        p.vid !== null && p.vid !== undefined ? `${hex4(p.vid)}:${hex4(p.pid)}` : "—", p.serial_number || "—", p.description || "—",
        el("button", { class: "btn small", type: "button", onclick: () => use(p) }, "Use")]))
        : el("p", { class: "muted" }, "No serial ports found."),
      el("p", { class: "muted small" }, "Nothing is selected automatically: pick the analyzer's port yourself, not the STPG or the GPIO controller."));
  } catch (error) { setChildren(target, notice("bad", "Scan failed", [error.message])); }
}

function editBinding(item, override) {
  const c = { ...(item ? item.config || {} : {}), ...(override || {}) };
  const presets = Object.keys(S.state.limits);
  const wiringValue = typeof c.wiring === "string" ? c.wiring : c.wiring ? "__keep__" : "same-gpio";
  const form = el("form", { class: "grid", onsubmit: (e) => e.preventDefault() },
    el("label", {}, "Name", el("input", { name: "name", value: item ? item.name : "", readonly: !!item, required: true })),
    el("label", {}, "Port (COM3, /dev/ttyACM1…)", el("input", { name: "port", value: c.port || "", spellcheck: "false" })),
    el("label", {}, "USB VID", el("input", { name: "vid", value: c.vid !== undefined ? `0x${hex4(c.vid)}` : "0x2e8a" })),
    el("label", {}, "USB PID", el("input", { name: "pid", value: c.pid !== undefined ? `0x${hex4(c.pid)}` : "" })),
    el("label", {}, "USB serial number", el("input", { name: "serial_number", value: c.serial_number || "", spellcheck: "false" })),
    el("label", {}, "Firmware limits: those of the analyzer firmware you installed (not told by its USB identity; Identify and each run check them exactly)",
      el("select", { name: "limits" }, presets.map((p) => el("option", { value: p, selected: limitsName(c.limits) === p },
        `${p} (${S.state.limits[p].max_frequency / 1e6} MHz, ${S.state.limits[p].blast_frequency / 1e6} MHz blast)`)),
        c.limits && !limitsName(c.limits) ? el("option", { value: "__keep__", selected: true }, `${override ? "as reported" : "keep"}: ${c.limits.id_line}, ${c.limits.max_frequency} Hz`) : null)),
    el("label", {}, "Wiring STPG → analyzer (after Save, Wiring shows the exact table)",
      el("select", { name: "wiring" }, S.state.wiring_presets.map((w) => el("option", { value: w, selected: wiringValue === w }, w)),
        wiringValue === "__keep__" ? el("option", { value: "__keep__", selected: true }, "keep the explicit map") : null)),
    el("details", { class: "advanced", open: c.marker_gpio !== undefined },
      el("summary", { class: "small" }, "Advanced (optional)"),
      el("label", {}, "Default marker GPIO: a spare STPG pin wired to the analyzer, used only when you plan a marker capture. Left empty, no pin is chosen for you",
        el("input", { name: "marker_gpio", type: "number", value: c.marker_gpio ?? "" }))),
    el("label", { class: "check" }, el("input", { type: "checkbox", name: "replace", checked: !!item }), " replace an existing binding with this name"));
  const save = el("button", { class: "btn primary", type: "button" }, "Save");
  const ports = el("div", { id: "scanPorts" });
  save.addEventListener("click", () => busyWhile(save, async () => {
    const f = new FormData(form);
    const num = (text) => { const t = String(text).trim(); const v = /^0x/i.test(t) ? parseInt(t, 16) : Number(t); if (!Number.isInteger(v)) throw new Error(`not an integer: ${t}`); return v; };
    const config = { port: f.get("port").trim(), vid: num(f.get("vid")), pid: num(f.get("pid")), serial_number: f.get("serial_number").trim(),
      limits: f.get("limits") === "__keep__" ? c.limits : S.state.limits[f.get("limits")],
      wiring: f.get("wiring") === "__keep__" ? c.wiring : f.get("wiring") };
    if (String(f.get("marker_gpio")).trim() !== "") config.marker_gpio = num(f.get("marker_gpio"));
    await api("analyzer/save", { name: f.get("name").trim(), config, replace: f.get("replace") === "on" });
    await loadAnalyzers();
  }));
  setChildren($("bindingForm"), el("h3", {}, item ? `Edit ${item.name}` : "New analyzer binding"),
    el("ol", { class: "small", id: "bindingSteps" },
      el("li", {}, "Plug in the analyzer, press Scan ports and press Use on the analyzer's own row. Nothing is selected for you; do not pick the STPG or the GPIO controller."),
      el("li", {}, "Choose the firmware limits of the analyzer firmware you installed, and the wiring."),
      el("li", {}, "Save. Then press Identify on the binding: it opens only this port, reads what the analyzer reports and closes it. If the report differs, you can review it and Save it yourself.")),
    el("div", { class: "actions" }, el("button", { class: "btn small", type: "button", id: "scanPortsButton", onclick: () => scanPorts(ports, form) }, "Scan ports")), ports, form,
    el("div", { class: "actions" }, save, el("button", { class: "btn", type: "button", onclick: () => setChildren($("bindingForm")) }, "Cancel")));
}

async function identify(name, button) {
  const result = await busyWhile(button, () => api("analyzer/identify", { name }));
  if (!result) return;
  const ok = result.status === "IDENTIFIED" && result.matches_binding;
  const reported = result.reported || result.analyzer || {};
  const item = S.analyzers.find((a) => a.name === name);
  const adopt = !result.matches_binding && result.reported && item && !item.simulated;
  setChildren($("identifyBody"), el("h3", {}, `Identify ${name}`),
    el("div", { class: "badges" }, badge(result.status, ok ? "ok" : "bad"), result.simulated ? badge("SIMULATED", "warn") : null,
      badge(result.matches_binding ? "matches this binding" : "does not match this binding", result.matches_binding ? "ok" : "bad")),
    kvList([["reported", reported.id_line ? `${reported.id_line} · ${reported.max_frequency} Hz · ${reported.channels} channels · buffer ${reported.buffer_bytes} B` : "—"],
      ["evidence saved", result.result_file ? copyable(result.result_file) : (result.result_persisted ? "yes" : "not persisted")]]),
    result.errors && result.errors.length ? notice("bad", "Errors", result.errors) : null,
    adopt ? el("div", { class: "actions" }, el("button", { class: "btn", type: "button", id: "useReported", onclick: () => editBinding(item, { limits: result.reported }) }, "Use the reported values in this binding…")) : null,
    el("p", { class: "muted small" }, "Only the identity request was sent: no capture request and no cancel byte. USB open/close effects are not measured."));
}

function renderAnalyzerChoice() {
  const select = $("planAnalyzer");
  const keep = select.value;
  select.replaceChildren(el("option", { value: "" }, "choose…"),
    ...S.analyzers.filter((a) => a.config).map((a) => el("option", { value: a.name, selected: a.name === keep }, a.simulated ? `${a.name} (demo model)` : a.name)));
}

// ---- 2. bundle -------------------------------------------------------------------
$("bundleOpen").addEventListener("click", (e) => busyWhile(e.target, async () => {
  await openBundle($("bundlePath").value.trim(), $("bundleSha").value.trim());
}));

async function openBundle(path, sha) {
  S.bundle = await api("bundle", { bundle: path, manifest_sha256: sha || null });
  S.plan = null;
  $("bundlePath").value = S.bundle.bundle_dir;
  $("bundleSha").value = S.bundle.manifest_sha256;
  const B = S.bundle;
  const m = B.marker;
  setChildren($("bundleBody"), 
    kvList([["bundle", copyable(B.bundle_dir)], ["manifest", copyable(B.manifest_sha256)], ["vectors", B.total_vectors.toLocaleString()]]),
    table(["segment", "global vectors", "signals"], B.segments.map((s) => [s.index, `${s.global_start}–${s.global_end}`,
      s.signals.map((x) => x.name).join(", ")])),
    m ? notice(m.status === "EQUIVALENT_TIMING" ? "info" : "bad", `Marker pattern: ${m.status}`, [
      m.error || `marker on GPIO${m.gpio}; its derivation record is found by this bundle's hash and re-verified against the source bundle before each run.`]) : null);
  renderPlan();
}

// ---- 3. capture plan ---------------------------------------------------------------
function planRequest() {
  const f = new FormData($("planForm"));
  const n = intOrNull(f.get("address"));
  if (n === null) throw new Error("give the vector ID or the STIL line");
  const address = f.get("address_kind") === "vector" ? { vector: n } : { line: n };
  const occurrence = intOrNull(f.get("occurrence"));
  if (occurrence !== null) address.occurrence = occurrence;
  const signals = String(f.get("signals") || "").split(",").map((s) => s.trim()).filter(Boolean);
  return { address, before_vectors: intOrNull(f.get("before")) ?? 16, after_vectors: intOrNull(f.get("after")) ?? 64,
    strategy: f.get("strategy"), signals: signals.length ? signals : null, sample_rate_hz: intOrNull(f.get("sample_rate")),
    pretrigger_policy: f.get("pretrigger") };
}

function needBundle() {
  if (!S.bundle) throw new Error("open a bundle first (card 2)");
  if (!$("planAnalyzer").value) throw new Error("choose an analyzer binding");
  return { bundle: S.bundle.bundle_dir, manifest_sha256: S.bundle.manifest_sha256, analyzer: $("planAnalyzer").value };
}

$("planRun").addEventListener("click", (e) => busyWhile(e.target, async () => {
  S.plan = await api("plan", { ...needBundle(), request: planRequest() });
  renderPlan();
}));
$("planLoad").addEventListener("click", async (e) => {
  const base = needBundleSafe();
  if (!base) return;
  const path = await pickPath({ title: "Capture plan JSON (from stpg-capture plan or Save plan…)" });
  if (!path) return;
  await busyWhile(e.target, async () => { S.plan = await api("plan/load", { ...base, path }); renderPlan(); });
});
function needBundleSafe() { try { return needBundle(); } catch (error) { setStatus(error.message, true); return null; } }
$("planSave").addEventListener("click", (e) => {
  const path = prompt("Save the plan as a new .json file:", `${S.state.results_dir}/capture-plan-${Date.now()}.json`);
  if (path) busyWhile(e.target, async () => { const r = await api("plan/save", { plan_id: S.plan.plan_id, path }); setStatus(`saved ${r.path}`); });
});

function renderPlan() {
  const body = $("planBody");
  const planned = !!(S.plan && S.plan.plan.status === "PLANNED");
  for (const id of ["planSave"]) $(id).disabled = !S.plan;
  for (const id of ["runWrite", "mockRun", "importRun"]) $(id).disabled = !planned;
  $("markerRun").disabled = !S.plan;
  if (!S.plan) { setChildren(body, ); renderMarkerChoice(); return; }
  const p = S.plan.plan;
  const t = p.trigger || {};
  setChildren(body, 
    el("div", { class: "badges" }, badge(p.status, planned ? "ok" : "bad"), t.type ? badge(`${t.type} trigger`, "info") : null,
      t.strategy ? badge(`strategy ${t.strategy}`, "muted") : null, badge(`segment ${p.bundle.segment}`, "muted")),
    p.reasons && p.reasons.length ? notice("bad", "Not plannable", p.reasons) : null,
    planned ? kvList([["trigger", t.condition], ["target", `vector ${p.target.vector} (local ${p.target.local_vector} of segment ${p.bundle.segment})`],
      ["window", `vectors ${p.window.first_vector}–${p.window.last_vector}`],
      ["sampling", `${(p.sample_rate_hz / 1e6).toFixed(3)} MHz · ${p.samples.pre} before + ${p.samples.post} after = ${p.samples.total} samples`]]) : null,
    planned ? table(["signal", "analyzer channel"], p.channels.map((c) => [c.signal, `ch${c.channel}`])) : null,
    t.type === "pattern" ? notice("warn", "Pattern trigger", ["The analyzer firmware signals a pattern match from its GPIO0 to its GPIO1: connect GPIO0 and GPIO1 of the analyzer Pico, or plan with Strategy \"level trigger\"."]) : null,
    p.assumptions && p.assumptions.length ? notice("info", "Assumptions", p.assumptions) : null,
    p.notes && p.notes.length ? notice("", "Notes", p.notes) : null);
  if (planned && $("markerVector").value === "") $("markerVector").value = p.target.vector;
  renderMarkerChoice();
}

// ---- 4. accessory config for an STPG run ------------------------------------------------
$("runWrite").addEventListener("click", (e) => busyWhile(e.target, async () => {
  const mode = $("runMode").value;
  if (!mode) throw new Error("choose the mode explicitly: device or simulated");
  const r = await api("config", { plan_id: S.plan.plan_id, mode, device_binding: $("runDevice").value.trim() || null,
    directory: $("runFolder").value.trim() || null });
  setChildren($("runBody"), 
    el("div", { class: "badges" }, badge("written", "ok"), badge(r.mode, r.mode === "simulated" ? "warn" : "info"),
      r.checked_by_core_sdk ? badge("core identity check passed", "ok") : badge("core identity check not run", "muted")),
    kvList([["accessory config", copyable(r.config)], ["segment", r.segment],
      ["open limit", `${r.open_timeout_s ?? r.timeout_s} s`], ["arm/complete limit", `${r.timeout_s} s`],
      ["stdin EOF grace", `${r.close_timeout_s ?? 1} s`], ["trigger", r.trigger]]),
    notice("info", "Next, in the STPG Runner", [
      "External accessory → Accessory config: this external.json → Check → tick “use with the next run” → Run….",
      "The run's results folder then holds stpg/ (the STPG record), accessory/session.json (the Runner's record of this process) and accessory/capture/ (this tool's capture). Open the run folder in card 7."]),
    notice("", "Notes", r.notes));
}));

// ---- 5. offline captures ----------------------------------------------------------------
$("mockRun").addEventListener("click", (e) => busyWhile(e.target, async () =>
  showCapture(await api("mock", { plan_id: S.plan.plan_id, offset_ps: intOrNull($("mockOffset").value) || 0 }))));
$("importRun").addEventListener("click", (e) => busyWhile(e.target, async () =>
  showCapture(await api("import", { plan_id: S.plan.plan_id, path: $("importPath").value.trim(),
    sample_rate_hz: intOrNull($("importRate").value), trigger_index: intOrNull($("importTrigger").value),
    channels: $("importChannels").value.trim() || null }))));

// ---- 6. marker pattern --------------------------------------------------------------------
function renderMarkerChoice() {
  const select = $("markerGpio");
  const P = S.plan;
  const candidates = P ? P.marker_candidates || [] : [];
  select.replaceChildren(el("option", { value: "" }, P && P.marker_default !== null && P.marker_default !== undefined
    ? `binding default (GPIO${P.marker_default})` : "choose a free wired pin…"),
  ...candidates.map((c) => el("option", { value: c.gpio }, `GPIO${c.gpio} → analyzer ch${c.analyzer_channel}`)));
  if (P && P.has_marker_signal) select.replaceChildren(el("option", { value: "" }, "this bundle already drives a marker"));
}
$("markerRun").addEventListener("click", (e) => busyWhile(e.target, async () => {
  const base = needBundle();
  const gpio = intOrNull($("markerGpio").value);
  const r = await api("marker", { ...base, project: $("markerProject").value.trim(), vector: intOrNull($("markerVector").value),
    marker_gpio: gpio, spare_confirmed: $("markerSpare").checked, directory: $("markerFolder").value.trim() || null });
  const ok = r.verification.status === "EQUIVALENT_TIMING";
  setChildren($("markerBody"), 
    el("div", { class: "badges" }, badge(r.verification.status, ok ? "ok" : "bad"), badge(`marker GPIO${r.marker_gpio}`, "info")),
    kvList([["derived project", copyable(r.project_path)], ["bundle", copyable(r.bundle_dir)], ["manifest", copyable(r.manifest_sha256)],
      ["derivation record", copyable(r.derivation_file)]]),
    notice("warn", "Before running it", ["The STPG drives the marker pin: confirm on the bench that it is spare and wired only to the analyzer.",
      "The STPG Runner names this pin in its run confirmation (a pin with a special role). Plan the capture on the derived bundle with Strategy “marker”."]),
    el("div", { class: "actions" }, el("button", { class: "btn", type: "button", onclick: (ev) => busyWhile(ev.target, () => openBundle(r.bundle_dir, r.manifest_sha256)) }, "Open the derived bundle")));
}));

// ---- 7. capture results ---------------------------------------------------------------------
$("captureOpen").addEventListener("click", (e) => busyWhile(e.target, async () =>
  showCapture(await api("capture", { path: $("capturePath").value.trim() }))));
$("resultsRefresh").addEventListener("click", (e) => busyWhile(e.target, async () => {
  const data = await api("results");
  setChildren($("resultsList"), data.items.length ? table(["folder", "kind", "outcome", "mode", "alignment", ""], data.items.map((i) => [
    i.name, i.kind || "—", i.outcome || i.error, i.mode || "—", i.alignment || "—",
    i.kind === "capture" ? el("button", { class: "btn small", type: "button", onclick: (ev) => busyWhile(ev.target, async () => showCapture(await api("capture", { path: i.path }))) }, "Open") : null]))
    : el("p", { class: "muted" }, `Nothing in ${data.root} yet.`));
}));

function captureResultView(view) {
  const r = view.result;
  const box = el("div", {});
  const outcomeKind = { CAPTURED: "info", MOCK: "warn", IMPORTED: "info", NO_DATA: "bad", NO_RUN: "bad", HOST_STOPPED: "warn", ANALYZER_ERROR: "bad", REJECT: "bad", CANCELLED: "warn", CLEANUP_ERROR: "bad", PERSIST_ERROR: "bad", PENDING: "bad" }[r.outcome] || "muted";
  const timing = r.timing || { adopted: false, reasons: ["no adoption record"] };
  const al = r.alignment || {};
  const alignKind = { ALIGNED: al.confidence === "low" ? "warn" : "ok", UNALIGNED: "bad", NOT_ALIGNABLE: "warn" }[r.alignment_status] || "muted";
  box.append(el("div", { class: "badges" }, el("strong", {}, "Capture"), badge(r.outcome, outcomeKind), badge(`alignment: ${r.alignment_status}${al.confidence ? ` (${al.confidence})` : ""}`, alignKind),
    timing.adopted ? badge("timing adopted", "ok") : badge("timing not adopted", "bad"),
    r.mode === "mock" || r.mode === "simulated" ? badge("not a measurement", "warn") : null, badge("no DUT verdict", "muted")));
  if (!timing.adopted) box.append(notice("bad", "Timing not adopted", timing.reasons));
  if (r.arming) {
    box.append(kvList([["armed", r.arming.armed_segment !== null && r.arming.armed_segment !== undefined
      ? `immediately before segment ${r.arming.armed_segment}'s RUN (requested by the STPG run)` : `not armed (target segment ${r.arming.target_segment})`]]));
  }
  if (r.outcome === "HOST_STOPPED") box.append(notice("warn", "The run stopped after the analyzer was armed", ["The STPG run ended without asking this tool to collect (cancel, a device error or another accessory's failure). The target RUN may or may not have been sent. No cancel byte was sent: the analyzer may still be armed until its own timeout; use Reset LA if it must be stopped now."]));
  if (r.partial) box.append(kvList([["partial bytes kept", `${r.partial.file} · ${r.partial.size.toLocaleString()} of ${(r.partial.expected || 0).toLocaleString()} bytes`]]));
  if (r.result_persisted === false) box.append(notice("bad", "The final capture record was not persisted; capture.json on disk is an earlier checkpoint."));
  if (al.status) {
    box.append(kvList([
      ["requested", view.plan_summary && view.plan_summary.window ? `vectors ${view.plan_summary.window.first_vector}–${view.plan_summary.window.last_vector}` : "—"],
      ["captured", al.actual && al.actual.first_vector !== null && al.actual.first_vector !== undefined ? `vectors ${al.actual.first_vector}–${al.actual.last_vector} · ${fmtTime(al.actual.start_ps)} – ${fmtTime(al.actual.end_ps)}` : "—"],
      ["coverage", al.coverage || "—"],
      ["trigger error", al.offset_ps !== null && al.offset_ps !== undefined ? `${fmtTime(al.offset_ps)} vs plan` : "—"],
      ["drift", al.drift_ppm !== null && al.drift_ppm !== undefined ? `${al.drift_ppm} ppm` : "—"],
      ["residual", al.rms_ps !== null && al.rms_ps !== undefined ? `${fmtTime(al.rms_ps)} rms` : "—"],
      ["edges", al.edges ? `${al.edges.matched_expected ?? 0} of ${al.edges.expected_in_capture} expected tester transitions matched` : "—"],
    ]));
    if (al.notes && al.notes.length) box.append(notice(al.status === "ALIGNED" ? "" : "bad", null, al.notes));
    if (al.levels && al.levels.length) box.append(table(["signal", "LA ch", "level agreement (tester-driven)"], al.levels.map((l) => [l.signal, l.channel, l.total ? `${l.agree}/${l.total} (${(100 * l.agree / l.total).toFixed(1)} %)` : "—"])));
    if (al.strobe_observations && al.strobe_observations.length) {
      box.append(table(["signal", "LA ch", "H strobes seen high", "L strobes seen low"], al.strobe_observations.map((s) => [s.signal, s.channel, `${s.H_seen_high}/${s.H_total}`, `${s.L_seen_low}/${s.L_total}`])));
      box.append(el("p", { class: "muted small" }, al.strobe_note || ""));
    }
  }
  if (r.errors && r.errors.length) box.append(notice("bad", "Errors", r.errors));
  const session = (r.analyzer && r.analyzer.session) || {};
  if (r.outcome === "NO_DATA" && session.armed && session.capture_bytes_received === 0) {
    // NO_DATA follows a sent target RUN; with not one byte back, the analyzer never fired (not a transfer stall)
    const trig = view.plan_summary && view.plan_summary.trigger;
    box.append(notice("warn", "Trigger never seen", [
      `The analyzer was armed before the target RUN and the RUN was sent, but it did not see ${trig ? trig.condition : "the trigger condition"} and sent no samples.`,
      ...(trig && trig.type === "pattern" ? ['This is a pattern trigger: the analyzer firmware signals it from its GPIO0 to its GPIO1, so it fires only when GPIO0 and GPIO1 of the analyzer Pico are connected. A plan made with Strategy "level trigger" does not use them, so it tells this cause apart from the wiring below.'] : []),
      "Also check the wires from the STPG GPIOs to the analyzer channels listed in the plan, the common ground, and that the STPG section shows the run completed."]));
  }
  box.append(kvList([["folder", copyable(view.directory)]]));
  return box;
}

async function showCapture(view) {
  S.capture = view;
  const body = $("captureBody");
  setChildren(body, 
    view.run ? kvList([["STPG run (the Runner's record)", `${view.run.outcome} · comparison ${view.run.comparison || "—"}`], ["run record", copyable(view.run.path)]]) : null,
    captureResultView(view), el("div", { id: "waveBox" }));
  $("capturePath").value = view.directory;
  if (view.waveform) await drawWaveform(view.capture_id);
  body.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function drawWaveform(captureId) {
  const w = await api("waveform", { capture_id: captureId });
  const lanes = w.lanes;
  const css = getComputedStyle(document.documentElement);
  const colors = { exp: css.getPropertyValue("--wave-exp").trim(), cap: css.getPropertyValue("--wave-cap").trim(),
    ink: css.getPropertyValue("--ink").trim(), line: css.getPropertyValue("--line").trim() };
  const width = Math.max(600, $("captureBody").clientWidth - 4);
  const laneH = 34, labelW = 120, top = 18;
  const canvas = el("canvas", { class: "wave", id: "waveCanvas" });
  const ratio = window.devicePixelRatio || 1;
  canvas.width = width * ratio; canvas.height = (top + lanes.length * laneH + 8) * ratio;
  canvas.style.height = `${top + lanes.length * laneH + 8}px`;
  const g = canvas.getContext("2d");
  g.scale(ratio, ratio);
  const span = Math.max(1, w.end_ps - w.start_ps);
  const x = (t) => labelW + ((Math.min(Math.max(t, w.start_ps), w.end_ps) - w.start_ps) / span) * (width - labelW - 8);
  g.font = "11px system-ui, sans-serif";
  g.fillStyle = colors.ink;
  g.fillText(fmtTime(0), labelW, 12);
  g.fillText(`+${fmtTime(span)}`, width - 80, 12);
  lanes.forEach((lane, i) => {
    const y0 = top + i * laneH;
    g.fillStyle = colors.ink;
    g.fillText(`${lane.signal} · ch${lane.channel}`, 4, y0 + 20);
    g.strokeStyle = colors.line; g.lineWidth = 1;
    g.beginPath(); g.moveTo(labelW, y0 + laneH - 1); g.lineTo(width - 8, y0 + laneH - 1); g.stroke();
    const draw = (segments, color, high, low, isExpected) => {
      g.strokeStyle = color; g.lineWidth = isExpected ? 1 : 2;
      g.beginPath();
      let last = null;
      for (const [a, b, level] of segments) {
        if (b < w.start_ps || a > w.end_ps) continue;
        const state = String(level);
        const yy = state === "1" ? high : state === "0" ? low : (high + low) / 2;
        if (last !== null) g.lineTo(x(a), yy); else g.moveTo(x(a), yy);
        g.lineTo(x(b), yy);
        last = yy;
      }
      g.stroke();
    };
    draw(lane.expected, colors.exp, y0 + 4, y0 + 14, true);
    draw(lane.captured, colors.cap, y0 + 17, y0 + 29, false);
  });
  setChildren($("waveBox"), el("h3", {}, "Waveform: expected tester levels and captured levels (pattern time)"),
    el("div", { class: "legend" }, el("span", {}, el("i", { style: `background:${colors.exp}` }), "expected (STPG drive; mid line = not driven)"),
      el("span", {}, el("i", { style: `background:${colors.cap}` }), w.aligned ? "captured (mapped by the fitted offset)" : "captured (not aligned: no offset fitted)")),
    !w.adopted ? notice("warn", "Diagnostic only", ["This capture's timing is not adopted: the drawing shows the fit as it is, without a timing verdict."]) : null,
    w.mode === "mock" || w.mode === "simulated" ? notice("warn", "Not a measurement", ["Synthesized from the expected levels."]) : null,
    canvas);
}

// ---- start ----------------------------------------------------------------------------------
(async function start() {
  try {
    S.state = await api("state");
  } catch (error) { setStatus(`cannot reach this tool's server: ${error.message}`, true); return; }
  const parts = ["A separate program from the STPG Runner (its own process and page)."];
  if (S.state.demo) parts.push("Demo: the analyzer named \"simulated\" is a host-only model — results prove nothing about a board.");
  if (!S.state.device_io) parts.push("Device access is disabled (--no-device-io): Identify is off.");
  $("banner").textContent = parts.join(" ");
  await loadAnalyzers().catch((error) => setStatus(error.message, true));
  renderPlan();
})();
