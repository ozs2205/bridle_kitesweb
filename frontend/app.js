"use strict";

// Backend base URL. Override with ?api=http://host:port (e.g. when not served on localhost:8000).
const API = new URLSearchParams(location.search).get("api") || window.location.origin;
const VM = "single-cycle";
const ABI_NAMES = [
  "zero", "ra", "sp", "gp", "tp", "t0", "t1", "t2",
  "s0/fp", "s1", "a0", "a1", "a2", "a3", "a4", "a5",
  "a6", "a7", "s2", "s3", "s4", "s5", "s6", "s7",
  "s8", "s9", "s10", "s11", "t3", "t4", "t5", "t6"
];
const DEFAULT_PROGRAM = [
  "# Print 42, then exit with code 0",
  "li   a0, 42       # value to print",
  "li   a7, 1        # SYSCALL_PRINT_INT",
  "ecall",
  "",
  "li   a0, 0        # exit code",
  "li   a7, 10       # SYSCALL_EXIT",
  "ecall",
  ""
].join("\n");

// memQuery: last successful "<addr>:<rows>" so memory is refreshed along with registers.
const app = { sessionId: null, busy: false, prevRegs: null, memQuery: null };
const $ = (id) => document.getElementById(id);

/* ------------------------------ console ------------------------------ */
function log(level, message) {
  const colors = { info: "text-slate-300", ok: "text-emerald-400", warn: "text-amber-300", error: "text-rose-400", out: "text-sky-300" };
  const line = document.createElement("div");
  line.className = "whitespace-pre-wrap break-words " + (colors[level] || colors.info);
  const time = new Date().toLocaleTimeString([], { hour12: false });
  line.textContent = "[" + time + "] " + message;   // textContent: program output is never parsed as HTML
  const body = $("consoleBody");
  body.appendChild(line);
  body.scrollTop = body.scrollHeight;
}

/* ------------------------------- API --------------------------------- */
class ApiError extends Error {
  constructor(status, code, message, details) {
    super(message);
    this.status = status; this.code = code; this.details = details;
  }
}

async function api(method, path, body) {
  let res;
  try {
    res = await fetch(API + path, {
      method,
      headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined
    });
  } catch (err) {
    throw new ApiError(0, "NETWORK", "Cannot reach Bridle at " + API + " (is the server running? is CORS allowed?)");
  }
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (e) { /* non-JSON body */ }
  if (!res.ok) {
    const e = data && data.error ? data.error : { code: "HTTP_" + res.status, message: text.slice(0, 300) || res.statusText };
    if (e.code === "SESSION_NOT_FOUND" && method !== "DELETE") forgetSession();   // expired/reaped server-side
    throw new ApiError(res.status, e.code, e.message, e.details);
  }
  return data;
}

function logApiError(err, context) {
  if (!(err instanceof ApiError)) { log("error", context + ": " + err); return; }
  log("error", context + ": [" + err.code + "] " + err.message);
  if (err.details && Array.isArray(err.details.errors)) {
    err.details.errors.forEach((p) => log("error", "  " + (p.line != null ? "line " + p.line + ": " : "") + p.message));
  }
  if (err.details && Array.isArray(err.details.problems)) {
    err.details.problems.forEach((p) => log("error", "  " + p.field + ": " + p.message));
  }
  if (err.code === "SESSION_NOT_FOUND") log("warn", "Session expired; a new one is created on the next Execute.");
}

/* ------------------------------ busy state ---------------------------- */
function setBusy(flag) {
  app.busy = flag;
  document.querySelectorAll(".action-btn").forEach((b) => { b.disabled = flag; });
  $("busyLabel").classList.toggle("hidden", !flag);
}

async function guarded(fn) {
  if (app.busy) return;
  setBusy(true);
  try { await fn(); } finally { setBusy(false); }
}

/* ------------------------------ sessions ------------------------------ */
function showSession() {
  $("sessionId").textContent = app.sessionId || "none";
}

function forgetSession() {
  app.sessionId = null;
  app.prevRegs = null;
  app.memQuery = null;
  showSession();
}

async function createSession() {
  if (app.sessionId) {
    const old = app.sessionId;
    try { await api("DELETE", "/sessions/" + old); log("info", "Closed previous session " + old); }
    catch (err) { /* it may already have expired: ignore */ }
  }
  forgetSession();
  const data = await api("POST", "/sessions");
  app.sessionId = data.session_id;
  showSession();
  renderRegisters(null);
  renderStats(null);
  renderMemory(null, true);
  log("ok", "New session " + app.sessionId);
}

async function ensureSession() {
  if (!app.sessionId) await createSession();
}

/* ------------------------------ rendering ----------------------------- */
function fmtStat(v, digits) {
  if (v === null || v === undefined) return "-";
  return typeof v === "number" && digits !== undefined && !Number.isInteger(v) ? v.toFixed(digits) : String(v);
}

function renderStats(data) {
  const s = (data && data.stats) || {};
  const items = [
    ["PC", data ? data.pc : "-"],
    ["Cycles", fmtStat(s.cycles)],
    ["Instructions", fmtStat(s.instructions_retired)],
    ["CPI", fmtStat(s.cpi, 3)],
    ["IPC", fmtStat(s.ipc, 3)],
    ["Stall cycles", fmtStat(s.stall_cycles)],
    ["Branch mispredicts", fmtStat(s.branch_mispredictions)]
  ];
  const bar = $("statsBar");
  bar.textContent = "";
  items.forEach(([label, value]) => {
    const card = document.createElement("div");
    card.className = "rounded bg-slate-800 px-2 py-1";
    const l = document.createElement("div");
    l.className = "text-slate-400 text-xs";
    l.textContent = label;
    const v = document.createElement("div");
    v.className = "mono text-slate-100 break-all";
    v.textContent = value;
    card.appendChild(l); card.appendChild(v);
    bar.appendChild(card);
  });
}

function signedDecimal(hex) {
  try { return BigInt.asIntN(64, BigInt(hex)).toString(); } catch (e) { return "-"; }
}

/* The backend returns register names exactly as the REPL prints them (x5, t0, ...). Accept either. */
function regValue(regs, i) {
  const names = ["x" + i].concat(ABI_NAMES[i].split("/"), [String(i)]);
  for (const n of names) if (regs[n] !== undefined) return regs[n];
  const keys = Object.keys(regs);
  return keys.length >= 32 && !keys.some((k) => /^x\d+$/.test(k)) ? regs[keys[i]] : undefined;   // positional fallback
}

function renderRegisters(data) {
  const regs = data && data.registers ? data.registers : null;
  const prev = app.prevRegs;
  const next = {};
  const body = $("regBody");
  body.textContent = "";
  for (let i = 0; i < 32; i++) {
    const key = "x" + i;
    const raw = regs ? regValue(regs, i) : undefined;
    const hex = raw === undefined ? null : String(raw);
    if (hex !== null) next[key] = hex;
    const changed = hex !== null && prev && prev[key] !== undefined && prev[key] !== hex;
    const tr = document.createElement("tr");
    tr.className = "border-t border-slate-800" + (changed ? " row-changed" : "");
    [
      [key, "px-3 py-1 text-slate-300"],
      [ABI_NAMES[i], "px-3 py-1 text-slate-500"],
      [hex === null ? "-" : hex, "px-3 py-1 text-slate-100"],
      [hex === null ? "-" : signedDecimal(hex), "px-3 py-1 text-right text-slate-400"]
    ].forEach(([text, cls]) => {
      const td = document.createElement("td");
      td.className = cls; td.textContent = text;
      tr.appendChild(td);
    });
    body.appendChild(tr);
  }
  if (regs) app.prevRegs = next;
}

/* The memory dump layout is not fixed by the API: handle {addr: value} maps and [{address, value}] lists. */
function pairFromObject(obj) {
  const keys = Object.keys(obj);
  if (!keys.every((k) => ["string", "number"].includes(typeof obj[k]))) return null;
  const a = keys.find((k) => /addr/i.test(k));
  const v = keys.find((k) => k !== a && /val|data|hex|bytes/i.test(k));
  return a && v ? [String(obj[a]), String(obj[v])] : null;
}

function collectMemoryRows(node, out) {
  if (Array.isArray(node)) {
    node.forEach((item) => {
      const pair = item && typeof item === "object" && !Array.isArray(item) ? pairFromObject(item) : null;
      if (pair) out.push(pair); else collectMemoryRows(item, out);
    });
  } else if (node && typeof node === "object") {
    Object.keys(node).forEach((k) => {
      const v = node[k];
      if (typeof v === "string" || typeof v === "number") out.push([k, String(v)]);
      else collectMemoryRows(v, out);
    });
  }
  return out;
}

function renderMemory(memory, reset) {
  const body = $("memBody");
  body.textContent = "";
  $("memRaw").textContent = memory === null || memory === undefined ? "" : JSON.stringify(memory, null, 2);
  const rows = collectMemoryRows(memory, []);
  const parsed = rows.map((r) => { try { return BigInt(/^0x/i.test(r[0]) ? r[0] : "0x" + r[0]); } catch (e) { return null; } });
  if (rows.length && parsed.every((p) => p !== null)) {
    const order = rows.map((_, i) => i).sort((x, y) => (parsed[x] < parsed[y] ? -1 : parsed[x] > parsed[y] ? 1 : 0));
    const sorted = order.map((i) => rows[i]);
    rows.length = 0; sorted.forEach((r) => rows.push(r));
  }
  if (rows.length === 0) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 2; td.className = "px-3 py-3 text-slate-500";
    td.textContent = reset ? "No memory fetched yet." : "No memory rows returned.";
    tr.appendChild(td); body.appendChild(tr);
    return;
  }
  rows.forEach(([addr, value]) => {
    const tr = document.createElement("tr");
    tr.className = "border-t border-slate-800";
    [[addr, "px-3 py-1 text-slate-300"], [value, "px-3 py-1 text-slate-100"]].forEach(([text, cls]) => {
      const td = document.createElement("td");
      td.className = cls; td.textContent = text;
      tr.appendChild(td);
    });
    body.appendChild(tr);
  });
}

/* --------------------------- state / actions -------------------------- */
async function refreshState() {
  const query = app.memQuery ? "?memory=" + encodeURIComponent(app.memQuery) : "";
  const data = await api("GET", "/sessions/" + app.sessionId + "/state" + query);
  renderStats(data);
  renderRegisters(data);
  if (app.memQuery) {
    if (data.memory_error) log("error", "Memory: " + data.memory_error);
    else renderMemory(data.memory);
  }
  return data;
}

async function loadAndRun(source) {
  await ensureSession();
  const id = app.sessionId;

  log("info", "Loading program (vm: " + VM + ")...");
  await api("POST", "/sessions/" + id + "/load", { source: source, vm: VM });
  log("ok", "Program loaded.");

  log("info", "Executing (mode: run)...");
  const r = await api("POST", "/sessions/" + id + "/execute", { mode: "run" });
  (r.program_output || []).forEach((line) => log("out", "program output: " + line));
  if (r.exit_code !== null && r.exit_code !== undefined) log("info", "Exited with exit code " + r.exit_code);
  if (r.status === "error" && r.error) {
    log("error", "Run failed: " + r.error.message + (r.error.line ? " (line " + r.error.line + ")" : ""));
  } else {
    log("ok", "Run status: " + r.status);
  }
  await refreshState();
  log("info", "State refreshed.");
}

async function doExecute() {
  const source = $("editor").value;
  if (!source.trim()) { log("warn", "The editor is empty."); return; }
  try {
    try {
      await loadAndRun(source);
    } catch (err) {
      // The server may have reaped an idle session: transparently start a fresh one once.
      if (!(err instanceof ApiError && err.code === "SESSION_NOT_FOUND")) throw err;
      log("warn", "Session expired; starting a new one.");
      await loadAndRun(source);
    }
  } catch (err) {
    logApiError(err, "Execute");
    if (err instanceof ApiError && (err.code === "TIMEOUT" || err.code === "REPL_CRASHED" || err.code === "OUTPUT_LIMIT")) {
      log("warn", "The simulator was restarted by the server; press Execute to load the program again.");
    }
  }
}

async function doReset() {
  if (!app.sessionId) { log("warn", "No session yet; press Execute or New Session first."); return; }
  try {
    await api("POST", "/sessions/" + app.sessionId + "/reset");
    log("ok", "Simulator reset (editor content kept).");
    await refreshState();
    log("info", "State refreshed.");
  } catch (err) {
    logApiError(err, "Reset");
  }
}

async function doFetchMemory() {
  if (!app.sessionId) { log("warn", "No session yet; press Execute first."); return; }
  const addr = $("memAddr").value.trim();
  const rows = parseInt($("memRows").value, 10);
  if (!/^(0[xX])?[0-9a-fA-F]{1,16}$/.test(addr)) { log("error", "Address must be hexadecimal, e.g. 0x1000."); return; }
  if (!Number.isInteger(rows) || rows < 1 || rows > 1024) { log("error", "Rows must be between 1 and 1024."); return; }
  try {
    const spec = addr + ":" + rows;
    const data = await api("GET", "/sessions/" + app.sessionId + "/state?memory=" + encodeURIComponent(spec));
    renderStats(data);
    renderRegisters(data);
    if (data.memory_error) { log("error", "Memory: " + data.memory_error); return; }
    if (data.memory === null || data.memory === undefined) log("warn", "No memory data returned.");
    else log("ok", "Fetched " + rows + " row(s) from " + addr + ".");
    app.memQuery = spec;
    renderMemory(data.memory);
  } catch (err) {
    logApiError(err, "Fetch Memory");
  }
}

function saveCode() {
  const blob = new Blob([$("editor").value], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = "program.s";
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  log("info", "Saved editor content as program.s");
}

function loadFile(file) {
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => { $("editor").value = String(reader.result); log("info", "Loaded " + file.name + " (" + file.size + " bytes)"); };
  reader.onerror = () => log("error", "Could not read " + file.name);
  reader.readAsText(file);
}

/* -------------------------------- tabs -------------------------------- */
function showTab(which) {
  const regs = which === "regs";
  $("panelRegs").classList.toggle("hidden", !regs);
  $("panelMem").classList.toggle("hidden", regs);
  $("tabRegs").className = "px-4 py-2 border-b-2 font-medium " + (regs ? "tab-active" : "tab-idle");
  $("tabMem").className = "px-4 py-2 border-b-2 font-medium " + (regs ? "tab-idle" : "tab-active");
}

/* ------------------------------- wiring ------------------------------- */
$("editor").value = DEFAULT_PROGRAM;
$("editor").addEventListener("keydown", (e) => {
  if (e.key === "Tab") {            // insert spaces instead of leaving the editor
    e.preventDefault();
    const t = e.target, s = t.selectionStart, end = t.selectionEnd;
    t.value = t.value.slice(0, s) + "    " + t.value.slice(end);
    t.selectionStart = t.selectionEnd = s + 4;
  }
});
document.addEventListener("keydown", (e) => {   // Ctrl/Cmd+Enter = Execute
  if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); guarded(doExecute); }
});
$("btnNewSession").addEventListener("click", () => guarded(async () => {
  try { await createSession(); } catch (err) { logApiError(err, "New Session"); }
}));
$("btnExecute").addEventListener("click", () => guarded(doExecute));
$("btnReset").addEventListener("click", () => guarded(doReset));
$("btnFetchMem").addEventListener("click", () => guarded(doFetchMemory));
$("btnSave").addEventListener("click", saveCode);
$("fileInput").addEventListener("change", (e) => { loadFile(e.target.files[0]); e.target.value = ""; });
$("tabRegs").addEventListener("click", () => showTab("regs"));
$("tabMem").addEventListener("click", () => showTab("mem"));
$("btnClearConsole").addEventListener("click", () => { $("consoleBody").textContent = ""; });
$("btnToggleConsole").addEventListener("click", () => {
  const hidden = $("consoleBody").classList.toggle("hidden");
  $("consoleArrow").innerHTML = hidden ? "&#9656;" : "&#9662;";
});
// Free the server-side simulator process when the tab closes.
window.addEventListener("pagehide", () => {
  if (app.sessionId) { try { fetch(API + "/sessions/" + app.sessionId, { method: "DELETE", keepalive: true }); } catch (e) { /* ignore */ } }
});

renderStats(null);
renderRegisters(null);
showSession();
log("info", "Ready. Bridle API: " + API + ". Press New Session, or just press Execute.");