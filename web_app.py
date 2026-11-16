import csv
import io
import json
import math
import os
import random
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen


HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))
AUTO_OPEN_BROWSER = os.environ.get("AUTO_OPEN_BROWSER", "1") == "1"
MAX_REQUEST_BYTES = 2_000_000
CSV_COLUMNS = [
    "timestamp",
    "policy",
    "provider",
    "input_tokens",
    "output_tokens",
    "ttft",
    "tps",
    "cost_usd",
    "cost_per_1k_calls",
    "model_id",
    "model_name",
]

FALLBACK_MODELS = {
    "openai/gpt-4o": {
        "name": "GPT-4o", "vendor": "OpenAI",
        "input_cost_per_m": 2.50, "output_cost_per_m": 10.00,
        "base_ttft": 0.42, "base_tps": 72.0,
    },
    "anthropic/claude-3.5-sonnet": {
        "name": "Claude 3.5 Sonnet", "vendor": "Anthropic",
        "input_cost_per_m": 3.00, "output_cost_per_m": 15.00,
        "base_ttft": 0.56, "base_tps": 58.0,
    },
    "google/gemini-pro-1.5": {
        "name": "Gemini 1.5 Pro", "vendor": "Google",
        "input_cost_per_m": 1.25, "output_cost_per_m": 5.00,
        "base_ttft": 0.38, "base_tps": 66.0,
    },
    "meta-llama/llama-3.3-70b-instruct": {
        "name": "Llama 3.3 70B Instruct", "vendor": "Meta",
        "input_cost_per_m": 0.12, "output_cost_per_m": 0.30,
        "base_ttft": 0.24, "base_tps": 82.0,
    },
    "deepseek/deepseek-r1": {
        "name": "DeepSeek R1", "vendor": "DeepSeek",
        "input_cost_per_m": 0.55, "output_cost_per_m": 2.19,
        "base_ttft": 0.82, "base_tps": 42.0,
    },
    "deepseek/deepseek-chat-v3": {
        "name": "DeepSeek V3", "vendor": "DeepSeek",
        "input_cost_per_m": 0.27, "output_cost_per_m": 1.10,
        "base_ttft": 0.34, "base_tps": 76.0,
    },
    "mistralai/mistral-large": {
        "name": "Mistral Large", "vendor": "Mistral",
        "input_cost_per_m": 2.00, "output_cost_per_m": 6.00,
        "base_ttft": 0.48, "base_tps": 54.0,
    },
}

CURATED_MARKERS = (
    "gpt-4o", "claude-3.5-sonnet", "gemini-pro-1.5", "llama-3.3-70b",
    "deepseek-r1", "deepseek-chat-v3", "deepseek-v3", "mistral-large",
)

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Model Cost and Performance</title>
<style>
:root {
  color-scheme: dark;
  --page:#171a1f; --panel:#20252c; --input:#191d23; --line:#39414b;
  --text:#e4e7eb; --muted:#a0a7b2; --blue:#78a6b8; --green:#76a995;
}
* { box-sizing:border-box }
body { margin:0; background:var(--page); color:var(--text); font:14px "Segoe UI",Arial,sans-serif }
button,select,textarea { font:inherit }
header { display:flex; align-items:center; gap:20px; padding:16px 22px; border-bottom:1px solid var(--line) }
h1 { margin:0; font-size:20px; font-weight:600 }
.subtitle { margin-top:4px; color:var(--muted); font-size:12px }
.brand { flex:1 }
.header-actions { display:flex; align-items:center; gap:8px; flex-wrap:wrap }
button { background:#303740; border:1px solid #444d58; color:var(--text); padding:8px 11px; border-radius:4px; cursor:pointer }
button:hover { background:#424b56 }
button.primary { background:#4c7887; border-color:#4c7887; color:#f2f5f6 }
button:disabled { opacity:.55; cursor:wait }
.status { padding:7px 9px; border-radius:4px; background:#343b44; color:#d1d6dc; font-size:11px; white-space:nowrap }
.status.live { background:#30483f; color:#d0e5d9 }
.status.error { background:#4a3a3b; color:#ead3d3 }
main { display:grid; grid-template-columns:minmax(330px,390px) minmax(0,1fr); gap:16px; padding:16px 20px; min-height:calc(100vh - 75px) }
.panel { background:var(--panel); border:1px solid var(--line); border-radius:6px; padding:16px; min-width:0 }
.left { display:flex; flex-direction:column }
.right { display:grid; grid-template-rows:minmax(320px,1fr) minmax(220px,.7fr); gap:16px }
h2 { font-size:15px; font-weight:600; margin:0 0 5px }
.help { color:var(--muted); font-size:12px; line-height:1.5; margin:0 0 14px }
label { display:block; font-size:12px; color:#c9cdd3; margin:12px 0 6px }
textarea,select { width:100%; border:1px solid var(--line); border-radius:4px; background:var(--input); color:var(--text); padding:10px }
textarea { min-height:104px; resize:vertical; line-height:1.45 }
.buttons { display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-top:12px }
.buttons button:first-child { grid-column:1 / -1 }
.model-wrap { flex:1; min-height:220px; max-height:430px; overflow:auto; margin-top:8px; border:1px solid var(--line); border-radius:4px }
table { border-collapse:collapse; width:100%; font-size:12px; white-space:nowrap }
th,td { padding:8px 7px; border-bottom:1px solid var(--line); text-align:left }
th { position:sticky; top:0; z-index:1; background:#303740; color:#d1d6dc; font-weight:600 }
td.num,th.num { text-align:right }
tr:last-child td { border-bottom:0 }
input[type=checkbox] { accent-color:var(--blue) }
.chart-panel,.log-panel { display:flex; flex-direction:column; min-height:0 }
.chart-grid { flex:1; display:grid; grid-template-columns:1fr 1fr; gap:14px; min-height:0 }
.chart-box { min-width:0; overflow:hidden }
.chart-title { font-size:12px; color:#c9cdd3; margin:9px 0 5px }
canvas { width:100%; height:calc(100% - 24px); min-height:240px }
.log-head { display:flex; justify-content:space-between; align-items:center; margin-bottom:9px }
.log-count { color:var(--muted); font-size:12px }
#log { overflow:auto; flex:1; margin:0; padding:10px; white-space:pre-wrap; overflow-wrap:anywhere; background:#171b20; border:1px solid var(--line); border-radius:4px; color:#c8d8d1; font:12px/1.6 Consolas,"Courier New",monospace }
.empty { color:var(--muted) }
#notice { position:fixed; right:20px; bottom:18px; max-width:440px; padding:11px 14px; border-radius:5px; background:#303740; border:1px solid #505a66; box-shadow:0 4px 20px #0005; display:none; z-index:5 }
@media(max-width:900px) { main { grid-template-columns:1fr } .right { grid-template-rows:440px 260px } .left { min-height:700px } }
@media(max-width:600px) { header { align-items:flex-start; flex-direction:column; gap:12px } .header-actions { width:100% } main { padding:10px; gap:10px } .chart-grid { grid-template-columns:1fr; overflow:auto } .chart-panel { min-height:700px } }
</style>
</head>
<body>
<header>
  <div class="brand"><h1>Model cost and performance</h1><div class="subtitle">Routing, benchmark telemetry, and tariff comparison</div></div>
  <div class="header-actions">
    <button onclick="syncRates()">Synchronize rates</button>
    <button onclick="document.getElementById('csvFile').click()">Import CSV</button>
    <button onclick="exportCsv()">Export CSV</button>
    <span id="status" class="status">Offline tariffs</span>
    <input id="csvFile" type="file" accept=".csv,text/csv" hidden>
  </div>
</header>
<main>
  <section class="panel left">
    <h2>Request</h2>
    <p class="help">Run a simulated workload against selected models.</p>
    <label for="prompt">Prompt</label>
    <textarea id="prompt" placeholder="Enter a sample prompt to estimate token use and performance."></textarea>
    <label for="policy">Routing policy</label>
    <select id="policy">
      <option>Lowest Cost</option><option>Lowest Latency</option><option>Broadcast All</option>
    </select>
    <div class="buttons">
      <button id="runButton" class="primary" onclick="executeRequests(1)">Execute request</button>
      <button id="stressButton" onclick="executeRequests(10)">Run 10-request stress test</button>
      <button onclick="clearSession()">Clear session</button>
    </div>
    <label>Model rates</label>
    <p class="help">Select one or more models. Rates are per million tokens.</p>
    <div class="model-wrap">
      <table>
        <thead><tr><th></th><th>Model</th><th>Vendor</th><th class="num">In $/M</th><th class="num">Out $/M</th><th class="num">TTFT</th></tr></thead>
        <tbody id="models"></tbody>
      </table>
    </div>
  </section>
  <section class="right">
    <section class="panel chart-panel">
      <h2>Performance</h2>
      <div class="chart-grid">
        <div class="chart-box"><div class="chart-title">Average throughput by provider (tokens/second)</div><canvas id="throughput"></canvas></div>
        <div class="chart-box"><div class="chart-title">Average cost per 1,000 requests (USD)</div><canvas id="cost"></canvas></div>
      </div>
    </section>
    <section class="panel log-panel">
      <div class="log-head"><h2>Execution log</h2><span id="count" class="log-count">0 executions</span></div>
      <pre id="log"></pre>
    </section>
  </section>
</main>
<div id="notice"></div>
<script>
let state = {models:{}, selected:[], telemetry:[], status:"offline"};
const $ = id => document.getElementById(id);
function notify(message) {
  const box = $("notice"); box.textContent = message; box.style.display = "block";
  clearTimeout(notify.timer); notify.timer = setTimeout(() => box.style.display = "none", 4000);
}
async function api(path, body) {
  const options = body === undefined ? {} : {
    method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)
  };
  const response = await fetch(path, options);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `Request failed (${response.status})`);
  return result;
}
function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, ch => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));
}
function render() {
  const status = $("status");
  status.textContent = state.status === "live" ? "LIVE SYNC" : state.status === "syncing" ? "SYNCING" : "OFFLINE FALLBACK";
  status.className = "status" + (state.status === "live" ? " live" : state.status === "offline" ? " error" : "");
  const selected = new Set(state.selected);
  $("models").innerHTML = Object.entries(state.models).map(([id,m]) =>
    `<tr><td><input type="checkbox" data-model="${escapeHtml(id)}" ${selected.has(id)?"checked":""}></td>
     <td title="${escapeHtml(id)}">${escapeHtml(m.name)}</td><td>${escapeHtml(m.vendor)}</td>
     <td class="num">${Number(m.input_cost_per_m).toFixed(4)}</td>
     <td class="num">${Number(m.output_cost_per_m).toFixed(4)}</td>
     <td class="num">${Number(m.base_ttft).toFixed(2)}s</td></tr>`
  ).join("");
  $("models").querySelectorAll("input[data-model]").forEach(box => box.addEventListener("change", () => {
    const id = box.dataset.model;
    state.selected = box.checked ? [...new Set([...state.selected,id])] : state.selected.filter(item => item !== id);
  }));
  $("count").textContent = `${state.telemetry.length.toLocaleString()} executions`;
  $("log").textContent = state.telemetry.slice(-300).map(row =>
    `[${row.timestamp}] ${row.policy} | ${row.model_name} (${row.provider}) | ` +
    `TTFT ${Number(row.ttft).toFixed(3)}s | TPS ${Number(row.tps).toFixed(2)} | ` +
    `cost $${Number(row.cost_usd).toFixed(6)}`
  ).join("\n") || "No executions yet.";
  $("log").className = state.telemetry.length ? "" : "empty";
  drawChart($("throughput"), "tps");
  drawChart($("cost"), "cost_usd");
}
function drawChart(canvas, metric) {
  const ratio = window.devicePixelRatio || 1, rect = canvas.getBoundingClientRect();
  if (!rect.width || !rect.height) return;
  canvas.width = Math.round(rect.width * ratio); canvas.height = Math.round(rect.height * ratio);
  const ctx = canvas.getContext("2d"); ctx.scale(ratio,ratio);
  const width = rect.width, height = rect.height;
  ctx.clearRect(0,0,width,height);
  const groups = {};
  state.telemetry.forEach(row => {
    const key = row.provider || "Unknown";
    (groups[key] ||= []).push(Number(row[metric]) * (metric === "cost_usd" ? 1000 : 1));
  });
  let items = Object.entries(groups).map(([name,values]) => ({
    name, value:values.reduce((a,b)=>a+b,0)/values.length
  }));
  if (metric === "tps") items.sort((a,b)=>b.value-a.value);
  else items.sort((a,b)=>a.value-b.value);
  items = items.slice(0,10);
  const left=metric==="tps"?106:112, right=35, top=8, rowH=25;
  ctx.font="11px Segoe UI, Arial"; ctx.fillStyle="#a0a7b2"; ctx.strokeStyle="#39414b"; ctx.lineWidth=1;
  if (!items.length) {
    ctx.fillText("Run a workload to view results", 12, 24); return;
  }
  const max=Math.max(...items.map(x=>x.value),0.000001);
  const plotW=Math.max(40,width-left-right), rows=Math.min(items.length,Math.floor((height-top-8)/rowH));
  for (let i=0;i<rows;i++) {
    const y=top+i*rowH, item=items[i], barW=Math.max(1,plotW*item.value/max);
    ctx.fillStyle="#c1c7ce"; const label=item.name.length>16?item.name.slice(0,15)+"…":item.name;
    ctx.fillText(label,2,y+14);
    ctx.fillStyle=metric==="tps"?"#78a6b8":"#76a995";
    ctx.fillRect(left,y+3,barW,14);
    ctx.fillStyle="#c1c7ce";
    const value=metric==="tps"?item.value.toFixed(1):item.value.toFixed(2);
    ctx.fillText(value,left+barW+5,y+14);
  }
}
async function refresh() { state = await api("/api/state"); render(); }
async function syncRates() {
  state.status="syncing"; render();
  try { const result=await api("/api/sync",{}); state=result; render(); notify(result.status==="live"?"Rates synchronized.":`Using offline tariff profiles${result.sync_error?`: ${result.sync_error}`:""}.`); }
  catch(error) { notify(error.message); }
}
async function executeRequests(count) {
  const prompt=$("prompt").value.trim();
  if (!prompt) { notify("Enter a prompt before executing."); $("prompt").focus(); return; }
  if (!state.selected.length) { notify("Select at least one model."); return; }
  $("runButton").disabled=true; $("stressButton").disabled=true;
  try {
    state=await api("/api/execute",{prompt,policy:$("policy").value,count,model_ids:state.selected});
    render();
  } catch(error) { notify(error.message); }
  finally { $("runButton").disabled=false; $("stressButton").disabled=false; }
}
async function clearSession() {
  try { state=await api("/api/clear",{}); render(); notify("Session telemetry cleared."); }
  catch(error) { notify(error.message); }
}
function csvEscape(value) {
  const text=String(value ?? "");
  return /[",\r\n]/.test(text) ? `"${text.replace(/"/g,'""')}"` : text;
}
function exportCsv() {
  if (!state.telemetry.length) { notify("No telemetry to export."); return; }
  const contents=[CSV_HEADERS.join(","),...state.telemetry.map(row=>CSV_HEADERS.map(key=>csvEscape(row[key])).join(","))].join("\r\n");
  const blob=new Blob(["\ufeff",contents],{type:"text/csv;charset=utf-8"});
  const link=document.createElement("a"); link.href=URL.createObjectURL(blob);
  link.download="llm_telemetry.csv"; link.click(); URL.revokeObjectURL(link.href);
}
const CSV_HEADERS=["timestamp","policy","provider","input_tokens","output_tokens","ttft","tps","cost_usd","cost_per_1k_calls","model_id","model_name"];
$("csvFile").addEventListener("change", async event => {
  const file=event.target.files[0]; if (!file) return;
  try {
    const text=await file.text();
    const result=await api("/api/import",{csv:text});
    state=result; render();
    notify(`Imported ${result.imported} row(s); skipped ${result.skipped} invalid row(s).`);
  } catch(error) { notify(error.message); }
  event.target.value="";
});
window.addEventListener("resize",()=>{drawChart($("throughput"),"tps");drawChart($("cost"),"cost_usd");});
refresh().then(syncRates).catch(error=>notify(error.message));
</script>
</body>
</html>
"""

STATE_LOCK = threading.RLock()
MODELS = {model_id: model.copy() for model_id, model in FALLBACK_MODELS.items()}
SELECTED_MODELS = set(FALLBACK_MODELS)
TELEMETRY = []
SYNC_STATUS = "offline"
SYNC_ERROR = ""
SIMULATOR_POOL = ThreadPoolExecutor(max_workers=16, thread_name_prefix="llm-simulation")


def _vendor_for(model_id, item):
    vendor = item.get("vendor")
    if isinstance(vendor, str) and vendor.strip():
        return vendor.strip()
    prefix = model_id.split("/", 1)[0].replace("-", " ").replace("_", " ")
    return " ".join(part.capitalize() for part in prefix.split()) or "Unknown"


def fetch_openrouter_models():
    request = Request(
        "https://openrouter.ai/api/v1/models",
        headers={"User-Agent": "Model-Cost-Performance-Dashboard/1.0"},
    )
    with urlopen(request, timeout=5) as response:
        payload = json.loads(response.read().decode("utf-8"))
    raw_models = payload.get("data")
    if not isinstance(raw_models, list):
        raise ValueError("OpenRouter response did not contain a model list.")
    models = {}
    for item in raw_models:
        model_id = item.get("id")
        pricing = item.get("pricing") or {}
        if not isinstance(model_id, str) or not isinstance(pricing, dict):
            continue
        try:
            input_cost = float(pricing["prompt"]) * 1_000_000
            output_cost = float(pricing["completion"]) * 1_000_000
        except (KeyError, TypeError, ValueError):
            continue
        if not all(math.isfinite(value) and value >= 0 for value in (input_cost, output_cost)):
            continue
        model_name = item.get("name")
        if not isinstance(model_name, str) or not model_name.strip():
            model_name = model_id
        lowered_id = model_id.lower()
        profile = next(
            (
                fallback for fallback_id, fallback in FALLBACK_MODELS.items()
                if fallback_id.split("/", 1)[1] in lowered_id
            ),
            None,
        )
        models[model_id] = {
            "name": model_name.strip(),
            "vendor": _vendor_for(model_id, item),
            "input_cost_per_m": input_cost,
            "output_cost_per_m": output_cost,
            "base_ttft": profile["base_ttft"] if profile else 0.5,
            "base_tps": profile["base_tps"] if profile else 55.0,
        }
    if not models:
        raise ValueError("OpenRouter returned no models with valid token prices.")
    return models


def app_state():
    with STATE_LOCK:
        return {
            "models": {key: value.copy() for key, value in MODELS.items()},
            "selected": sorted(SELECTED_MODELS),
            "telemetry": [row.copy() for row in TELEMETRY],
            "status": SYNC_STATUS,
            "sync_error": SYNC_ERROR,
        }


def normalize_import_row(source):
    provider = str(source.get("provider", "")).strip()
    timestamp = str(source.get("timestamp", "")).strip()
    policy = str(source.get("policy", "")).strip()
    if not provider or not timestamp or not policy:
        raise ValueError("Required text column is empty.")
    input_tokens = int(source["input_tokens"])
    output_tokens = int(source["output_tokens"])
    ttft = float(source["ttft"])
    tps = float(source["tps"])
    cost = float(source["cost_usd"])
    if (
        input_tokens < 0 or output_tokens < 0
        or not all(math.isfinite(value) and value >= 0 for value in (ttft, tps, cost))
    ):
        raise ValueError("Telemetry numbers must be finite and non-negative.")
    model_id = str(source.get("model_id") or "").strip()
    model_name = str(source.get("model_name") or model_id or provider).strip()
    return {
        "timestamp": timestamp, "policy": policy, "provider": provider,
        "input_tokens": input_tokens, "output_tokens": output_tokens,
        "ttft": round(ttft, 4), "tps": round(tps, 4),
        "cost_usd": f"{cost:.6f}", "cost_per_1k_calls": f"{cost * 1000:.6f}",
        "model_id": model_id, "model_name": model_name,
    }


def simulate_one(model_id, model, prompt, policy):
    input_tokens = max(1, round(math.ceil((len(prompt) + 360) / 4) * random.uniform(0.94, 1.08)))
    output_tokens = max(32, round(random.gauss(190, 45)))
    ttft = max(0.025, model["base_ttft"] * random.uniform(0.82, 1.42) * random.uniform(0.9, 1.1))
    effective_tps = max(3.0, model["base_tps"] * random.uniform(0.78, 1.18))
    total_time = ttft + output_tokens / effective_tps
    measured_tps = output_tokens / total_time
    cost = (
        input_tokens / 1_000_000 * model["input_cost_per_m"]
        + output_tokens / 1_000_000 * model["output_cost_per_m"]
    )
    time.sleep(random.uniform(0.04, 0.18))
    return {
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "policy": policy,
        "provider": model["vendor"],
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "ttft": round(ttft, 4),
        "tps": round(measured_tps, 4),
        "cost_usd": f"{cost:.6f}",
        "cost_per_1k_calls": f"{cost * 1000:.6f}",
        "model_id": model_id,
        "model_name": model["name"],
    }


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "ModelCostDashboard/1.0"

    def log_message(self, _format, *_args):
        return

    def _send(self, status, body, content_type="application/json; charset=utf-8"):
        if isinstance(body, str):
            encoded = body.encode("utf-8")
        else:
            encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(encoded)

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Invalid request length.") from exc
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise ValueError("Request body is too large.")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Request body must be valid JSON.") from exc
        if not isinstance(data, dict):
            raise ValueError("Request body must be a JSON object.")
        return data

    def do_GET(self):
        if self.path == "/":
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif self.path == "/api/state":
            self._send(200, app_state())
        else:
            self._send(404, {"error": "Not found."})

    def do_POST(self):
        global MODELS, SELECTED_MODELS, SYNC_STATUS, SYNC_ERROR
        try:
            data = self._read_json()
            if self.path == "/api/sync":
                with STATE_LOCK:
                    SYNC_STATUS = "syncing"
                try:
                    catalog = fetch_openrouter_models()
                except Exception as exc:
                    with STATE_LOCK:
                        SYNC_STATUS = "offline"
                        SYNC_ERROR = str(exc)
                    self._send(200, app_state())
                    return
                with STATE_LOCK:
                    MODELS.update(catalog)
                    SYNC_STATUS = "live"
                    SYNC_ERROR = ""
                self._send(200, app_state())
            elif self.path == "/api/execute":
                result = self._execute(data)
                self._send(200, result)
            elif self.path == "/api/import":
                result = self._import_csv(data)
                self._send(200, result)
            elif self.path == "/api/clear":
                with STATE_LOCK:
                    TELEMETRY.clear()
                self._send(200, app_state())
            else:
                self._send(404, {"error": "Not found."})
        except ValueError as exc:
            self._send(400, {"error": str(exc)})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            self._send(500, {"error": f"Request failed: {exc}"})

    def _execute(self, data):
        prompt = data.get("prompt")
        policy = data.get("policy")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("Enter a prompt before executing.")
        if len(prompt) > 50_000:
            raise ValueError("Prompt is too long (maximum 50,000 characters).")
        if policy not in ("Broadcast All", "Lowest Cost", "Lowest Latency"):
            raise ValueError("Select a valid routing policy.")
        try:
            count = int(data.get("count", 1))
        except (TypeError, ValueError) as exc:
            raise ValueError("Request count must be an integer.") from exc
        if count not in (1, 10):
            raise ValueError("Request count must be 1 or 10.")
        ids = data.get("model_ids")
        if not isinstance(ids, list):
            raise ValueError("Select one or more models.")
        with STATE_LOCK:
            available = {model_id: MODELS[model_id].copy() for model_id in ids if model_id in MODELS}
        if not available:
            raise ValueError("Select at least one available model.")
        if policy == "Broadcast All":
            targets = list(available)
        elif policy == "Lowest Cost":
            targets = [min(available, key=lambda model_id:
                available[model_id]["input_cost_per_m"] + available[model_id]["output_cost_per_m"])]
        else:
            targets = [min(available, key=lambda model_id: available[model_id]["base_ttft"])]

        futures = [
            SIMULATOR_POOL.submit(simulate_one, model_id, available[model_id], prompt, policy)
            for _ in range(count) for model_id in targets
        ]
        rows = [future.result() for future in futures]
        with STATE_LOCK:
            TELEMETRY.extend(rows)
            SELECTED_MODELS = set(available)
        return app_state()

    def _import_csv(self, data):
        text = data.get("csv")
        if not isinstance(text, str):
            raise ValueError("CSV content is missing.")
        try:
            reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
            required = {
                "timestamp", "policy", "provider", "input_tokens", "output_tokens",
                "ttft", "tps", "cost_usd",
            }
            missing = sorted(required - set(reader.fieldnames or []))
            if missing:
                raise ValueError("Missing CSV columns: " + ", ".join(missing))
            valid, skipped = [], 0
            for row in reader:
                try:
                    valid.append(normalize_import_row(row))
                except (KeyError, TypeError, ValueError):
                    skipped += 1
        except csv.Error as exc:
            raise ValueError(f"Could not parse CSV: {exc}") from exc
        with STATE_LOCK:
            TELEMETRY.extend(valid)
        result = app_state()
        result["imported"] = len(valid)
        result["skipped"] = skipped
        return result


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    request_slots = threading.BoundedSemaphore(32)
    daemon_threads = True

    def process_request(self, request, client_address):
        self.request_slots.acquire()
        try:
            super().process_request(request, client_address)
        except Exception:
            self.request_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.request_slots.release()


def main():
    server = BoundedThreadingHTTPServer((HOST, PORT), RequestHandler)
    display_host = "127.0.0.1" if HOST in ("0.0.0.0", "::") else HOST
    print(f"Dashboard is running at http://{display_host}:{PORT}")
    if AUTO_OPEN_BROWSER:
        print("Keep this window open while using the website. Press Ctrl+C to stop.")
        threading.Timer(
            0.7, lambda: webbrowser.open(f"http://{display_host}:{PORT}")
        ).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard...")
    finally:
        server.server_close()
        SIMULATOR_POOL.shutdown(wait=False, cancel_futures=True)


if __name__ == "__main__":
    main()
