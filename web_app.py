import csv
import hmac
import io
import json
import math
import matplotlib
import os
import random
matplotlib.use("Agg")

import seaborn as sns
import sqlite3
import statistics
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from io import StringIO
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen

from matplotlib.backends.backend_svg import FigureCanvasSVG
from matplotlib.figure import Figure

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))
AUTO_OPEN_BROWSER = os.environ.get("AUTO_OPEN_BROWSER", "1") == "1"
DB_PATH = os.environ.get(
    "DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "telemetry.sqlite3")
)
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
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
    "mode",
    "response_text",
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

# The browser reads this to learn the app name, icon, and standalone display mode.
APP_MANIFEST = json.dumps({
    "name": "Model Cost & Performance",
    "short_name": "Model Metrics",
    "description": "Compare language model costs and performance.",
    "start_url": "/",
    "display": "standalone",
    "background_color": "#111820",
    "theme_color": "#111820",
    "icons": [{
        "src": "/app-icon.svg",
        "sizes": "any",
        "type": "image/svg+xml",
        "purpose": "any maskable",
    }],
})

# Cache only the app shell; live API responses and telemetry are never cached.
SERVICE_WORKER = r"""const CACHE_NAME = "model-cost-performance-shell-v1";
const APP_SHELL = ["/", "/manifest.webmanifest", "/app-icon.svg"];

self.addEventListener("install", event => {
  event.waitUntil(caches.open(CACHE_NAME).then(cache => cache.addAll(APP_SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(key => key !== CACHE_NAME).map(key => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", event => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== "GET" || url.origin !== self.location.origin || url.pathname.startsWith("/api/")) return;

  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request).then(async response => {
        if (response.ok) {
          const cache = await caches.open(CACHE_NAME);
          await cache.put("/", response.clone());
        }
        return response;
      }).catch(() => caches.match("/"))
    );
    return;
  }

  if (APP_SHELL.includes(url.pathname)) {
    event.respondWith(caches.match(request).then(response => response || fetch(request)));
  }
});
"""

APP_ICON = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">
<rect width="128" height="128" rx="26" fill="#111820"/>
<path d="M25 92h78" stroke="#526372" stroke-width="5" stroke-linecap="round"/>
<rect x="31" y="61" width="15" height="31" rx="4" fill="#78a6b8"/>
<rect x="56" y="43" width="15" height="49" rx="4" fill="#76a995"/>
<rect x="81" y="29" width="15" height="63" rx="4" fill="#78a6b8"/>
<path d="m31 48 26-15 24 6 19-17" fill="none" stroke="#e7edf2" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/>
<circle cx="100" cy="22" r="5" fill="#76a995"/>
</svg>"""

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#111820">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="apple-touch-icon" href="/app-icon.svg">
<title>Model Cost and Performance</title>
<style>
:root {
  color-scheme: dark;
  --page:#111820; --panel:#19232d; --input:#121b24; --line:#2c3946;
  --text:#e7edf2; --muted:#9aa9b6; --blue:#78a6b8; --green:#76a995;
  --raised:#202d38; --soft-blue:#253a47;
}
* { box-sizing:border-box }
body { margin:0; background:var(--page); color:var(--text); font:14px "Segoe UI",Arial,sans-serif; letter-spacing:.01em }
button,select,textarea { font:inherit }
header { display:flex; align-items:center; gap:24px; padding:18px 26px; border-bottom:1px solid var(--line); background:#151e27 }
h1 { margin:3px 0 0; font-size:21px; font-weight:600; letter-spacing:-.025em }
.eyebrow { color:#91a5b2; font-size:10px; font-weight:600; letter-spacing:.12em; text-transform:uppercase }
.subtitle { margin-top:5px; color:var(--muted); font-size:12px }
.brand { flex:1 }
.header-actions { display:flex; align-items:center; gap:8px; flex-wrap:wrap }
button { background:#25313c; border:1px solid #3a4855; color:var(--text); padding:8px 12px; border-radius:5px; cursor:pointer; transition:background .15s,border-color .15s }
button:hover { background:#303e4a; border-color:#52616e }
button.primary { background:#426f7c; border-color:#426f7c; color:#f4f7f8; font-weight:600 }
button.primary:hover { background:#4c7f8c }
button:disabled { opacity:.55; cursor:wait }
[hidden] { display:none !important }
.status { display:inline-flex; align-items:center; gap:7px; padding:7px 10px; border:1px solid #42505b; border-radius:5px; background:#202a33; color:#d1dbe2; font-size:10px; font-weight:600; letter-spacing:.055em; white-space:nowrap }
.status:before { content:""; width:6px; height:6px; border-radius:50%; background:#a0aab2 }
.status.live { border-color:#36584e; background:#20352f; color:#d0e5d9 }
.status.live:before { background:#76a995 }
.status.error { border-color:#594849; background:#392d30; color:#ead3d3 }
.status.error:before { background:#bd8585 }
main { display:grid; grid-template-columns:minmax(330px,370px) minmax(0,1fr); gap:18px; padding:20px 26px 26px; min-height:calc(100vh - 80px); max-width:1800px; margin:auto }
.panel { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:18px; min-width:0; box-shadow:0 6px 20px #050a101a }
.left { display:flex; flex-direction:column }
.right { display:grid; grid-template-rows:minmax(390px,1fr) minmax(220px,.62fr); gap:18px; min-width:0 }
h2 { font-size:15px; font-weight:600; margin:0 0 5px; letter-spacing:-.01em }
.section-heading { display:flex; justify-content:space-between; align-items:flex-start; gap:12px; margin-bottom:13px }
.section-kicker { color:var(--muted); font-size:10px; letter-spacing:.09em; text-transform:uppercase; margin-bottom:5px }
.help { color:var(--muted); font-size:12px; line-height:1.55; margin:0 0 14px }
label { display:block; font-size:11px; font-weight:600; color:#c9d2d9; margin:13px 0 6px }
textarea,select,input[type=search] { width:100%; border:1px solid #35424e; border-radius:5px; background:var(--input); color:var(--text); padding:10px 11px; outline:none }
textarea:focus,select:focus,input[type=search]:focus { border-color:#668d9a; box-shadow:0 0 0 2px #668d9a2c }
textarea { min-height:100px; resize:vertical; line-height:1.5 }
.buttons { display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-top:13px }
.buttons button:first-child { grid-column:1 / -1 }
.mode-box { margin-top:11px; padding:11px; border:1px solid var(--line); border-radius:5px; background:#16212a }
.mode-box label { margin:3px 0; display:flex; gap:8px; align-items:center }
.mode-box label[hidden] { display:none }
.warning { color:#cbb887; font-size:11px; line-height:1.5; margin:7px 0 0 }
.guide { margin:10px 26px 0; padding:11px 15px; background:#19232d; border:1px solid var(--line); border-radius:6px; color:var(--muted); font-size:12px; line-height:1.65 }
.guide summary { color:var(--text); cursor:pointer; font-weight:600 }
.guide h3 { color:var(--text); font-size:12px; margin:12px 0 3px }
.guide p { margin:4px 0 }
.guide code { color:#d5dde3 }
.response { display:none; margin-top:12px; padding:12px; border:1px solid #3d5962; border-radius:5px; background:#15232b; white-space:pre-wrap; overflow:auto; max-height:190px; font-size:12px; line-height:1.6 }
.response.visible { display:block }
.model-wrap { flex:1; min-height:220px; max-height:430px; overflow:auto; margin-top:8px; border:1px solid var(--line); border-radius:5px; background:#151e27 }
table { border-collapse:collapse; width:100%; font-size:11px; white-space:nowrap }
th,td { padding:9px 8px; border-bottom:1px solid #2a3742; text-align:left }
th { position:sticky; top:0; z-index:1; background:#25313c; color:#cbd5dc; font-size:10px; font-weight:600; letter-spacing:.04em; text-transform:uppercase }
td.num,th.num { text-align:right }
tr:last-child td { border-bottom:0 }
tbody tr:hover { background:#1d2b35 }
input[type=checkbox] { accent-color:var(--blue) }
.chart-panel,.log-panel { display:flex; flex-direction:column; min-height:0 }
.metrics { display:grid; grid-template-columns:repeat(auto-fit,minmax(135px,1fr)); gap:10px; margin:12px 0 8px }
.metric { padding:12px; background:#151f28; border:1px solid var(--line); border-radius:6px; min-width:0; border-top:2px solid #4d7180 }
.metric:nth-child(2) { border-top-color:#638d83 }
.metric:nth-child(3) { border-top-color:#8e8064 }
.metric:nth-child(4) { border-top-color:#71839b }
.metric-label { color:var(--muted); font-size:10px; letter-spacing:.035em }
.metric-value { margin-top:6px; font-size:19px; font-weight:600; letter-spacing:-.02em; overflow:hidden; text-overflow:ellipsis }
.filter-row { display:flex; justify-content:flex-end; gap:8px; margin:5px 0 8px }
.filter-row select { width:auto; min-width:150px; padding:7px 9px; font-size:11px }
.analytics-actions { display:flex; align-items:center; gap:8px }
.pro-only { display:none !important }
.pro-only.enabled { display:block !important }
.analytics-actions .pro-only.enabled { display:inline-flex !important }
.pro-metric.enabled { display:block !important }
.plans-modal { position:fixed; inset:0; z-index:20; display:none; align-items:center; justify-content:center; padding:20px; background:#080d13cc }
.plans-modal.open { display:flex }
.plans-dialog { width:min(760px,100%); max-height:90vh; overflow:auto; padding:22px; border:1px solid #43515d; border-radius:9px; background:#19232d; box-shadow:0 20px 70px #0009 }
.plans-top { display:flex; justify-content:space-between; align-items:flex-start; gap:15px; margin-bottom:18px }
.plans-top h2 { font-size:20px; margin:4px 0 }
.plan-grid { display:grid; grid-template-columns:1fr 1fr; gap:14px }
.plan-card { padding:18px; border:1px solid var(--line); border-radius:7px; background:#141e27 }
.plan-card.featured { border-color:#547e89 }
.plan-price { margin:12px 0 5px; font-size:28px; font-weight:600 }
.plan-price small { color:var(--muted); font-size:12px; font-weight:400 }
.plan-card ul { min-height:130px; padding-left:19px; color:#c3ced6; font-size:12px; line-height:1.9 }
.demo-note { margin:15px 0 0; padding:11px; border:1px solid #63583e; border-radius:5px; background:#29271f; color:#d5c69d; font-size:11px; line-height:1.55 }
.plan-action { width:100% }
.chart-grid { flex:1; display:grid; grid-template-columns:1fr 1fr; gap:14px; min-height:0 }
.chart-box { min-width:0; overflow:hidden }
.chart-title { font-size:11px; color:#bdc8d0; font-weight:600; margin:8px 0 7px }
.chart-image { display:block; width:100%; height:calc(100% - 24px); min-height:240px; object-fit:contain; object-position:top left }
.log-head { display:flex; justify-content:space-between; align-items:center; margin-bottom:9px }
#log { overflow:auto; flex:1; margin:0; padding:12px; white-space:pre-wrap; overflow-wrap:anywhere; background:#121a21; border:1px solid var(--line); border-radius:5px; color:#c5d5cf; font:11px/1.7 Consolas,"Courier New",monospace }
.log-count { color:var(--muted); font-size:11px }
.empty { color:var(--muted) }
#notice { position:fixed; right:20px; bottom:18px; max-width:440px; padding:11px 14px; border-radius:5px; background:#25313c; border:1px solid #52616e; box-shadow:0 8px 24px #0005; display:none; z-index:5 }
@media(max-width:1050px) { main { grid-template-columns:minmax(300px,340px) minmax(0,1fr); padding-left:16px; padding-right:16px } .metrics { grid-template-columns:repeat(2,minmax(0,1fr)) } .right { grid-template-rows:minmax(480px,1fr) minmax(220px,.6fr) } }
@media(max-width:800px) { header { padding:16px 18px; align-items:flex-start; flex-wrap:wrap } .header-actions { width:100% } main { grid-template-columns:1fr; padding:14px } .right { grid-template-rows:540px 260px } .left { min-height:690px } .guide { margin:10px 14px 0 } }
@media(max-width:600px) { header { gap:12px } .header-actions { gap:6px } .header-actions button { padding:7px 8px; font-size:11px } main { padding:10px; gap:10px } .chart-grid { grid-template-columns:1fr; overflow:auto } .chart-panel { min-height:760px } .filter-row { justify-content:stretch; flex-wrap:wrap } .filter-row select { flex:1; min-width:120px } }
</style>
</head>
<body>
<header>
  <div class="brand"><div class="eyebrow">Model operations workspace</div><h1>Cost &amp; performance</h1><div class="subtitle">Routing, benchmark telemetry, and tariff comparison</div></div>
  <div class="header-actions">
    <button onclick="syncRates()">Synchronize rates</button>
    <button onclick="document.getElementById('csvFile').click()">Import CSV</button>
    <button onclick="exportCsv()">Export CSV</button>
    <button onclick="document.getElementById('guide').open = !document.getElementById('guide').open">Demo guide</button>
    <button id="installAppButton" hidden>Install app</button>
    <button onclick="openPlans()">Plans</button>
    <span id="planBadge" class="status">FREE PLAN</span>
    <span id="status" class="status">Offline tariffs</span>
    <input id="csvFile" type="file" accept=".csv,text/csv" hidden>
  </div>
</header>
<div id="plansModal" class="plans-modal" role="dialog" aria-modal="true" aria-labelledby="plansTitle" onclick="if(event.target === this) closePlans()">
  <section class="plans-dialog">
    <div class="plans-top">
      <div><div class="section-kicker">Product plans</div><h2 id="plansTitle">Choose a demo plan</h2><p class="help">Compare a simple free tier with a Pro preview.</p></div>
      <button onclick="closePlans()" aria-label="Close plans">Close</button>
    </div>
    <div class="plan-grid">
      <article class="plan-card">
        <div class="section-kicker">For evaluation and classroom use</div>
        <h2>Free</h2>
        <div class="plan-price">$0 <small>/ always</small></div>
        <ul>
          <li>Model price synchronization</li>
          <li>Simulated requests and stress tests</li>
          <li>Summary metrics and provider charts</li>
          <li>CSV import and export</li>
        </ul>
        <button id="freePlanButton" class="plan-action" onclick="setDemoPlan('free')">Current plan</button>
      </article>
      <article class="plan-card featured">
        <div class="section-kicker">Illustrative premium tier</div>
        <h2>Pro preview</h2>
        <div class="plan-price">$12 <small>/ month · demo price</small></div>
        <ul>
          <li>Everything in Free</li>
          <li>Provider and execution-mode chart filters</li>
          <li>Provider-count comparison metric</li>
          <li>Downloadable analysis report</li>
        </ul>
        <button id="proPlanButton" class="primary plan-action" onclick="setDemoPlan('pro')">Preview Pro features</button>
      </article>
    </div>
    <p class="demo-note">Class-project preview only: no checkout, payment collection, or real subscription is implemented. Your preview choice is saved only in this browser. Live OpenRouter requests are separate, require server configuration, and may incur provider charges.</p>
  </section>
</div>
<details class="guide" id="guide">
  <summary>Project guide and viva notes</summary>
  <h3>Project objective</h3>
  <p>The dashboard helps compare language-model tariffs and observed or estimated response performance through one vendor-neutral interface. It combines catalog synchronization, routing policies, request metrology, data import/export, and analytics for a classroom demonstration.</p>
  <h3>Application architecture</h3>
  <p>The browser renders the interface and sends JSON requests to a Python HTTP server built with the standard library. The server validates input, performs routing and calculations, writes telemetry to SQLite, and returns results. Python generates SVG charts with Matplotlib; Seaborn provides the statistical bar-plot styling. The browser displays those SVGs and updates summary cards.</p>
  <h3>Installable app and beginner code map</h3>
  <p>This is a single-page web app: the dashboard updates in place instead of navigating between separate pages. On supported browsers, choose Install app (or use the browser’s install option) to open it in its own app-style window. The web manifest defines its name, colors, and icon; the service worker caches only the page shell so it can open offline. Data and charts still need the Python server and an internet connection. To explain the code: the Python section serves the page and API, the HTML defines controls, the CSS styles them, and the JavaScript connects buttons to API requests.</p>
  <h3>Plans and monetization status</h3>
  <p>Free and Pro preview are classroom-demo choices saved in this browser. The Pro price is illustrative only: there is no checkout, payment collection, user account, or real paid entitlement. A real subscription product would need secure server-side accounts, plan checks, and payment-provider integration.</p>
  <h3>Pricing source and normalization</h3>
  <p>Live tariffs come from OpenRouter’s public model catalog. Per-token input and output prices are multiplied by one million to produce comparable per-million-token values. If the catalog is unreachable, the dashboard retains its built-in sample profiles and marks pricing as offline.</p>
  <h3>Routing and measurement</h3>
  <p>Lowest Cost chooses the selected model with the lowest combined input and output tariff. Lowest Latency chooses the lowest estimated time to first token (TTFT). Broadcast All benchmarks all selected models; in simulation this does not contact providers.</p>
  <p>Input size is estimated from prompt length plus overhead. Simulated output size, latency and network jitter are generated locally. Throughput is output tokens divided by estimated total response time. Cost is (input tokens × input price + output tokens × output price) divided by one million. Simulation values are estimates, not provider measurements.</p>
  <h3>Live inference and safety</h3>
  <p>When enabled, a live request streams an actual answer from OpenRouter and records usage, response time, and estimated tariff cost. The API key remains on the server. Live calls may be billable. On a public deployment, configure both <code>OPENROUTER_API_KEY</code> and <code>DASHBOARD_PASSWORD</code>; the dashboard password protects its API. Avoid private prompts and unconfirmed broadcasts.</p>
  <h3>Telemetry and visual analysis</h3>
  <p>Each record includes timestamp, policy, model/vendor, input/output token counts, TTFT, tokens per second, cost, execution mode, and—when live—the response text. SQLite stores records across normal process restarts. Seaborn aggregates provider-level averages and Matplotlib draws the two charts as SVG. Provider and mode filters update the summary metrics and charts together.</p>
  <h3>Deployment and limitations</h3>
  <p>Render reads <code>render.yaml</code> to install requirements, start the service, and check its health. This project’s configured SQLite database path is temporary on Render’s free service, so data may be lost after a restart or redeploy; export CSV backups for important results. Persistent hosting storage or a managed database would be needed for durable production records.</p>
  <h3>Viva summary</h3>
  <p>“I built a vendor-neutral model cost and performance dashboard. A Python server syncs tariffs and applies cost- or latency-based routing; the simulator estimates telemetry without provider charges. SQLite stores session history, CSV supports transfer, and Seaborn/Matplotlib produce filterable analytics. Optional live inference uses a server-side OpenRouter key.”</p>
</details>
<main>
  <section class="panel left">
    <div class="section-heading"><div><div class="section-kicker">Workload configuration</div><h2>Request controls</h2></div></div>
    <p class="help">Compare a simulated request or optionally get a real model response.</p>
    <label for="prompt">Prompt</label>
    <textarea id="prompt" placeholder="Enter a sample prompt to estimate token use and performance."></textarea>
    <label for="policy">Routing policy</label>
    <select id="policy">
      <option>Lowest Cost</option><option>Lowest Latency</option><option>Broadcast All</option>
    </select>
    <div class="mode-box">
      <label><input id="liveMode" type="checkbox" onchange="updateMode()"> Use live OpenRouter response</label>
      <p class="warning">Live requests send the prompt to OpenRouter and may be billable. The owner must configure both a private server API key and a dashboard password to enable this mode.</p>
      <label id="broadcastConfirmRow" hidden><input id="broadcastConfirm" type="checkbox"> I understand Broadcast All may call and charge every selected model.</label>
    </div>
    <div class="buttons">
      <button id="runButton" class="primary" onclick="executeRequests(1)">Execute request</button>
      <button id="stressButton" onclick="executeRequests(10)">Run 10-request stress test</button>
      <button onclick="clearSession()">Clear session</button>
    </div>
    <pre id="response" class="response"></pre>
    <label>Model rates</label>
    <p class="help">Select one or more models. Rates are per million tokens.</p>
    <input id="modelSearch" type="search" placeholder="Search models or providers" aria-label="Search models" oninput="render()">
    <div class="model-wrap">
      <table>
        <thead><tr><th></th><th>Model</th><th>Vendor</th><th class="num">In $/M</th><th class="num">Out $/M</th><th class="num">TTFT</th></tr></thead>
        <tbody id="models"></tbody>
      </table>
    </div>
  </section>
  <section class="right">
    <section class="panel chart-panel">
      <div class="section-heading"><div><div class="section-kicker">Session analytics</div><h2>Performance overview</h2></div><div class="analytics-actions"><button id="reportButton" class="pro-only" hidden onclick="saveAnalysisReport()">Save analysis report</button><span class="status">SESSION VIEW</span></div></div>
      <div class="metrics">
        <div class="metric"><div class="metric-label">Executions</div><div id="metricExecutions" class="metric-value">0</div></div>
        <div class="metric"><div class="metric-label">Average throughput</div><div id="metricTps" class="metric-value">0 TPS</div></div>
        <div class="metric"><div class="metric-label">Total recorded cost</div><div id="metricCost" class="metric-value">$0.000000</div></div>
        <div class="metric"><div class="metric-label">Average TTFT</div><div id="metricTtft" class="metric-value">0.000s</div></div>
        <div id="metricProviderCard" class="metric pro-metric" hidden><div class="metric-label">Providers compared</div><div id="metricProviders" class="metric-value">0</div></div>
      </div>
      <div id="advancedAnalytics" class="filter-row">
        <select id="providerFilter" aria-label="Filter by provider" onchange="render()"><option value="">All providers</option></select>
        <select id="policyFilter" aria-label="Filter by mode" onchange="render()"><option value="">All modes</option></select>
      </div>
      <div class="chart-grid">
        <div class="chart-box"><div class="chart-title">Average throughput by provider (tokens/second)</div><img id="throughputChart" class="chart-image" alt="Seaborn and Matplotlib chart of average throughput by provider"></div>
        <div class="chart-box"><div class="chart-title">Average cost per 1,000 requests (USD)</div><img id="costChart" class="chart-image" alt="Seaborn and Matplotlib chart of average cost per thousand requests"></div>
      </div>
    </section>
    <section class="panel log-panel">
      <div class="log-head"><div><div class="section-kicker">Audit trail</div><h2>Execution history</h2></div><span id="count" class="log-count">0 executions</span></div>
      <pre id="log"></pre>
    </section>
  </section>
</main>
<div id="notice"></div>
<script>
let state = {models:{}, selected:[], telemetry:[], status:"offline", live_available:false};
let accessPassword = "";
let chartRequestId = 0;
function readDemoPlan() {
  try { return localStorage.getItem("model_dashboard_demo_plan") === "pro" ? "pro" : "free"; }
  catch(_error) { return "free"; }
}
let demoPlan = readDemoPlan();
const $ = id => document.getElementById(id);
function notify(message) {
  const box = $("notice"); box.textContent = message; box.style.display = "block";
  clearTimeout(notify.timer); notify.timer = setTimeout(() => box.style.display = "none", 4000);
}
async function api(path, body, allowPasswordPrompt = true) {
  const options = body === undefined ? {} : {
    method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)
  };
  options.headers = {...(options.headers || {}), "X-Dashboard-Password":accessPassword};
  const response = await fetch(path, options);
  const result = await response.json();
  if (response.status === 401 && allowPasswordPrompt) {
    const entered = window.prompt("Enter the dashboard password configured by the owner:");
    if (!entered) throw new Error("Dashboard password is required.");
    accessPassword = entered;
    return api(path, body, false);
  }
  if (!response.ok) throw new Error(result.error || `Request failed (${response.status})`);
  return result;
}
function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, ch => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));
}
function render() {
  updatePlanUI();
  const status = $("status");
  status.textContent = state.status === "live" ? "LIVE SYNC" : state.status === "syncing" ? "SYNCING" : "OFFLINE FALLBACK";
  status.className = "status" + (state.status === "live" ? " live" : state.status === "offline" ? " error" : "");
  const liveMode = $("liveMode");
  liveMode.disabled = !state.live_available;
  liveMode.title = state.live_available ? "Sends prompts through the server-side OpenRouter key." : "Configure OPENROUTER_API_KEY on the server to enable live responses.";
  updateMode();
  const selected = new Set(state.selected);
  const modelQuery = $("modelSearch").value.trim().toLowerCase();
  $("models").innerHTML = Object.entries(state.models).filter(([id,m]) =>
    !modelQuery || `${id} ${m.name} ${m.vendor}`.toLowerCase().includes(modelQuery)
  ).map(([id,m]) =>
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
  const providerFilter = $("providerFilter"), previousProvider = providerFilter.value;
  const providers = [...new Set(state.telemetry.map(row => row.provider))].sort();
  providerFilter.innerHTML = '<option value="">All providers</option>' +
    providers.map(value => `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`).join("");
  providerFilter.value = providers.includes(previousProvider) ? previousProvider : "";
  const modeFilter = $("policyFilter"), previousMode = modeFilter.value;
  const modes = [...new Set(state.telemetry.map(row => row.mode || "Imported"))].sort();
  modeFilter.innerHTML = '<option value="">All modes</option>' +
    modes.map(value => `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`).join("");
  modeFilter.value = modes.includes(previousMode) ? previousMode : "";
  const rows = filteredRows();
  const totalCost = rows.reduce((sum,row) => sum + Number(row.cost_usd || 0), 0);
  const average = key => rows.length ? rows.reduce((sum,row) => sum + Number(row[key] || 0), 0) / rows.length : 0;
  $("metricExecutions").textContent = rows.length.toLocaleString();
  $("metricTps").textContent = `${average("tps").toFixed(1)} TPS`;
  $("metricCost").textContent = `$${totalCost.toFixed(6)}`;
  $("metricTtft").textContent = `${average("ttft").toFixed(3)}s`;
  $("metricProviders").textContent = new Set(rows.map(row => row.provider)).size.toLocaleString();
  $("count").textContent = `${rows.length.toLocaleString()} shown / ${state.telemetry.length.toLocaleString()} total`;
  $("log").textContent = rows.slice(-300).map(row =>
    `[${row.timestamp}] ${row.policy} | ${row.model_name} (${row.provider}) | ` +
    `${row.mode || "Imported"} | TTFT ${Number(row.ttft).toFixed(3)}s | TPS ${Number(row.tps).toFixed(2)} | ` +
    `cost $${Number(row.cost_usd).toFixed(6)}`
  ).join("\n") || "No executions yet.";
  $("log").className = rows.length ? "" : "empty";
  const lastResponse = [...rows].reverse().find(row => row.response_text);
  const responseBox = $("response");
  responseBox.textContent = lastResponse ? `${lastResponse.model_name} response:\n\n${lastResponse.response_text}` : "";
  responseBox.classList.toggle("visible", Boolean(lastResponse));
  refreshCharts();
}
function updatePlanUI() {
  const isPro = demoPlan === "pro";
  $("planBadge").textContent = isPro ? "PRO PREVIEW" : "FREE PLAN";
  $("planBadge").classList.toggle("live", isPro);
  $("advancedAnalytics").hidden = !isPro;
  $("metricProviderCard").hidden = !isPro;
  $("reportButton").hidden = !isPro;
  $("reportButton").classList.toggle("enabled", isPro);
  $("freePlanButton").textContent = isPro ? "Switch to Free" : "Current plan";
  $("freePlanButton").disabled = !isPro;
  $("proPlanButton").textContent = isPro ? "Pro preview active" : "Preview Pro features";
  $("proPlanButton").disabled = isPro;
}
function openPlans() {
  $("plansModal").classList.add("open");
  $("proPlanButton").focus();
}
function closePlans() {
  $("plansModal").classList.remove("open");
}
function setDemoPlan(plan) {
  demoPlan = plan === "pro" ? "pro" : "free";
  try { localStorage.setItem("model_dashboard_demo_plan", demoPlan); }
  catch(_error) { notify("Plan preview will last for this browser session only."); }
  if (demoPlan === "free") {
    $("providerFilter").value = "";
    $("policyFilter").value = "";
  }
  updatePlanUI();
  closePlans();
  render();
  if (demoPlan === "pro") notify("Pro preview activated. No payment was taken.");
}
function filteredRows() {
  const provider = $("providerFilter").value, mode = $("policyFilter").value;
  return state.telemetry.filter(row =>
    (!provider || row.provider === provider) &&
    (!mode || (row.mode || "Imported") === mode)
  );
}
function updateMode() {
  const isLive = $("liveMode").checked;
  $("broadcastConfirmRow").hidden = !(isLive && $("policy").value === "Broadcast All");
  $("stressButton").disabled = isLive;
  $("runButton").textContent = isLive ? "Send live request" : "Execute request";
}
async function refreshCharts() {
  const requestId = ++chartRequestId;
  try {
    const charts = await api("/api/charts", {
      provider:$("providerFilter").value,
      mode:$("policyFilter").value
    });
    if (requestId !== chartRequestId) return;
    $("throughputChart").src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(charts.tps);
    $("costChart").src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(charts.cost);
  } catch(error) {
    if (requestId === chartRequestId) notify(`Could not update charts: ${error.message}`);
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
  const policy = $("policy").value;
  const live = $("liveMode").checked;
  if (live && count !== 1) { notify("Live mode only sends one request at a time."); return; }
  if (live && policy === "Broadcast All" && !$("broadcastConfirm").checked) {
    notify("Confirm that Broadcast All may make a billable call to each selected model."); return;
  }
  $("runButton").disabled=true; $("stressButton").disabled=true;
  try {
    state=await api("/api/execute",{prompt,policy,count,model_ids:state.selected,mode:live?"live":"simulation",confirm_broadcast:$("broadcastConfirm").checked});
    render();
  } catch(error) { notify(error.message); }
  finally { $("runButton").disabled=false; updateMode(); }
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
function saveAnalysisReport() {
  if (demoPlan !== "pro") { openPlans(); return; }
  const rows = filteredRows();
  const byProvider = {};
  rows.forEach(row => {
    const item = byProvider[row.provider] ||= {calls:0,tps:0,ttft:0,cost_usd:0};
    item.calls += 1;
    item.tps += Number(row.tps || 0);
    item.ttft += Number(row.ttft || 0);
    item.cost_usd += Number(row.cost_usd || 0);
  });
  Object.keys(byProvider).forEach(provider => {
    const item = byProvider[provider];
    item.average_tps = item.tps / item.calls;
    item.average_ttft = item.ttft / item.calls;
    item.average_cost_per_1k = item.cost_usd / item.calls * 1000;
    delete item.tps;
    delete item.ttft;
  });
  const report = {
    report_type:"Class project demo report",
    plan:"Pro preview (not a paid subscription)",
    generated_at:new Date().toISOString(),
    filters:{provider:$("providerFilter").value || "All providers",mode:$("policyFilter").value || "All modes"},
    summary:{
      executions:rows.length,
      average_tps:rows.length ? rows.reduce((sum,row)=>sum+Number(row.tps||0),0)/rows.length : 0,
      average_ttft:rows.length ? rows.reduce((sum,row)=>sum+Number(row.ttft||0),0)/rows.length : 0,
      total_cost_usd:rows.reduce((sum,row)=>sum+Number(row.cost_usd||0),0)
    },
    providers:byProvider,
    telemetry:rows
  };
  const blob = new Blob([JSON.stringify(report,null,2)],{type:"application/json"});
  const link=document.createElement("a");
  link.href=URL.createObjectURL(blob);
  link.download="model-performance-report.json";
  link.click();
  setTimeout(()=>URL.revokeObjectURL(link.href),1000);
  notify("Analysis report saved.");
}
const CSV_HEADERS=["timestamp","policy","provider","input_tokens","output_tokens","ttft","tps","cost_usd","cost_per_1k_calls","model_id","model_name","mode","response_text"];
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
$("policy").addEventListener("change", updateMode);
document.addEventListener("keydown", event => { if (event.key === "Escape") closePlans(); });
let pendingInstallPrompt = null;
window.addEventListener("beforeinstallprompt", event => {
  event.preventDefault();
  pendingInstallPrompt = event;
  $("installAppButton").hidden = false;
});
$("installAppButton").addEventListener("click", async () => {
  if (!pendingInstallPrompt) return;
  pendingInstallPrompt.prompt();
  const choice = await pendingInstallPrompt.userChoice;
  pendingInstallPrompt = null;
  $("installAppButton").hidden = true;
  if (choice.outcome === "accepted") notify("App installed.");
});
window.addEventListener("appinstalled", () => {
  pendingInstallPrompt = null;
  $("installAppButton").hidden = true;
  notify("App installed.");
});
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/service-worker.js").catch(error => {
      console.error("Could not register the app service worker:", error);
    });
  });
}
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
CHART_LOCK = threading.Lock()


def build_chart_svgs(rows, provider_filter="", mode_filter=""):
    filtered = [
        row for row in rows
        if (not provider_filter or row.get("provider") == provider_filter)
        and (not mode_filter or row.get("mode", "Imported") == mode_filter)
    ]
    grouped = {}
    for row in filtered:
        provider = str(row.get("provider") or "Unknown")
        grouped.setdefault(provider, {"tps": [], "cost": []})
        grouped[provider]["tps"].append(float(row.get("tps") or 0))
        grouped[provider]["cost"].append(float(row.get("cost_usd") or 0) * 1000)

    chart_specs = (
        ("tps", "Mean throughput by provider", "Tokens per second", "#78a6b8", True),
        ("cost", "Mean cost per 1,000 requests", "USD per 1,000 requests", "#76a995", False),
    )
    results = {}
    with CHART_LOCK:
        sns.set_theme(style="whitegrid", context="notebook", font="DejaVu Sans")
        matplotlib.rcParams["svg.fonttype"] = "none"
        for key, title, xlabel, color, descending in chart_specs:
            figure = Figure(figsize=(4.6, 3.0), dpi=120, facecolor="#19232d")
            axis = figure.add_subplot(111)
            axis.set_facecolor("#19232d")
            if grouped:
                plot_rows = [
                    {
                        "provider": provider,
                        "value": statistics.fmean(values[key]),
                    }
                    for provider, values in grouped.items()
                ]
                plot_rows.sort(
                    key=lambda item: item["value"],
                    reverse=descending,
                )
                plot_rows = plot_rows[:14]
                sns.barplot(
                    x=[item["value"] for item in plot_rows],
                    y=[item["provider"] for item in plot_rows],
                    color=color,
                    errorbar=None,
                    ax=axis,
                )
                axis.set_xlabel(xlabel, color="#a0aebb", fontsize=10)
                axis.set_ylabel("")
                axis.tick_params(axis="x", colors="#a0aebb", labelsize=9)
                axis.tick_params(axis="y", colors="#d3dce2", labelsize=9, length=0)
                axis.xaxis.grid(True, color="#34424d", linewidth=0.7)
                axis.yaxis.grid(False)
                maximum = max((item["value"] for item in plot_rows), default=0)
                axis.set_xlim(0, maximum * 1.22 if maximum > 0 else 1)
                for container in axis.containers:
                    axis.bar_label(
                        container,
                        fmt="%.1f" if key == "tps" else "%.2f",
                        padding=3,
                        color="#dce4e9",
                        fontsize=9,
                    )
            else:
                axis.text(
                    0.5,
                    0.52,
                    "No telemetry matches these filters",
                    transform=axis.transAxes,
                    ha="center",
                    va="center",
                    color="#a0aebb",
                    fontsize=10,
                )
                axis.set_xticks([])
                axis.set_yticks([])
                axis.set_xlabel("")
                axis.set_ylabel("")
                axis.grid(False)
            axis.set_title(title, color="#e4ebf0", fontsize=12, loc="left", pad=12)
            for spine in axis.spines.values():
                spine.set_visible(False)
            figure.subplots_adjust(left=0.34, right=0.91, top=0.83, bottom=0.2)
            output = StringIO()
            FigureCanvasSVG(figure).print_svg(output)
            results[key] = output.getvalue()
            figure.clear()
    return results


def initialize_store():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    database = sqlite3.connect(DB_PATH, timeout=10)
    try:
        database.execute(
            """
            CREATE TABLE IF NOT EXISTS telemetry (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                policy TEXT NOT NULL,
                provider TEXT NOT NULL,
                input_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL,
                ttft REAL NOT NULL,
                tps REAL NOT NULL,
                cost_usd TEXT NOT NULL,
                cost_per_1k_calls TEXT NOT NULL,
                model_id TEXT NOT NULL DEFAULT '',
                model_name TEXT NOT NULL DEFAULT '',
                mode TEXT NOT NULL DEFAULT 'Simulation',
                response_text TEXT NOT NULL DEFAULT ''
            )
            """
        )
        columns = {
            row[1] for row in database.execute("PRAGMA table_info(telemetry)")
        }
        for column, declaration in (
            ("mode", "TEXT NOT NULL DEFAULT 'Simulation'"),
            ("response_text", "TEXT NOT NULL DEFAULT ''"),
        ):
            if column not in columns:
                database.execute(
                    f"ALTER TABLE telemetry ADD COLUMN {column} {declaration}"
                )
        database.commit()
        cursor = database.execute(
            "SELECT timestamp, policy, provider, input_tokens, output_tokens, "
            "ttft, tps, cost_usd, cost_per_1k_calls, model_id, model_name, mode, response_text "
            "FROM telemetry ORDER BY id"
        )
        names = [entry[0] for entry in cursor.description]
        TELEMETRY.extend(dict(zip(names, row)) for row in cursor.fetchall())
    finally:
        database.close()


def persist_rows(rows):
    if not rows:
        return
    with STATE_LOCK:
        database = sqlite3.connect(DB_PATH, timeout=10)
        try:
            database.executemany(
                "INSERT INTO telemetry (timestamp, policy, provider, input_tokens, "
                "output_tokens, ttft, tps, cost_usd, cost_per_1k_calls, model_id, "
                "model_name, mode, response_text) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        row["timestamp"], row["policy"], row["provider"],
                        int(row["input_tokens"]), int(row["output_tokens"]),
                        float(row["ttft"]), float(row["tps"]), str(row["cost_usd"]),
                        str(row["cost_per_1k_calls"]), str(row.get("model_id", "")),
                        str(row.get("model_name", "")), str(row.get("mode", "Simulation")),
                        str(row.get("response_text", "")),
                    )
                    for row in rows
                ],
            )
            database.commit()
            TELEMETRY.extend(dict(row) for row in rows)
        finally:
            database.close()


def clear_persisted_rows():
    with STATE_LOCK:
        database = sqlite3.connect(DB_PATH, timeout=10)
        try:
            database.execute("DELETE FROM telemetry")
            database.commit()
            TELEMETRY.clear()
        finally:
            database.close()


initialize_store()


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
            "live_available": bool(
                os.environ.get("OPENROUTER_API_KEY", "").strip()
                and DASHBOARD_PASSWORD.strip()
            ),
            "authentication_required": bool(DASHBOARD_PASSWORD.strip()),
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
        "mode": str(source.get("mode") or "Imported"),
        "response_text": str(source.get("response_text") or ""),
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
        "mode": "Simulation",
        "response_text": "",
    }


def _extract_stream_content(choice):
    delta = choice.get("delta") or {}
    content = delta.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        )
    return ""


def call_openrouter(model_id, model, prompt):
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise ValueError(
            "Live responses are not configured. Add OPENROUTER_API_KEY to the server environment."
        )
    body = json.dumps(
        {
            "model": model_id,
            "messages": [
                {
                    "role": "system",
                    "content": "Answer the user clearly and concisely.",
                },
                {"role": "user", "content": prompt},
            ],
            "stream": True,
            "stream_options": {"include_usage": True},
        }
    ).encode("utf-8")
    request = Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://model-cost-performance.onrender.com",
            "X-Title": "Model Cost and Performance Dashboard",
            "User-Agent": "Model-Cost-Performance-Dashboard/1.0",
        },
        method="POST",
    )
    started = time.perf_counter()
    first_content_time = None
    chunks = []
    usage = {}
    try:
        response_context = urlopen(request, timeout=90)
    except Exception as exc:
        status = getattr(exc, "code", None)
        if status:
            raise RuntimeError(
                f"OpenRouter request failed with HTTP {status}. Check the server key, "
                "model access, and account balance."
            ) from exc
        raise RuntimeError(f"Could not reach OpenRouter: {exc}") from exc

    with response_context as response:
        for raw_line in response:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            event = line[5:].strip()
            if event == "[DONE]":
                break
            try:
                data = json.loads(event)
            except json.JSONDecodeError:
                continue
            if isinstance(data.get("error"), dict):
                raise RuntimeError(
                    data["error"].get("message", "The model provider returned an error.")
                )
            if isinstance(data.get("usage"), dict):
                usage = data["usage"]
            for choice in data.get("choices", []):
                text = _extract_stream_content(choice)
                if text:
                    if first_content_time is None:
                        first_content_time = time.perf_counter()
                    chunks.append(text)

    completed = time.perf_counter()
    response_text = "".join(chunks)
    if not response_text:
        raise RuntimeError("The selected model returned an empty response.")
    elapsed = max(completed - started, 0.001)
    ttft = max((first_content_time or completed) - started, 0.001)
    input_tokens = int(usage.get("prompt_tokens") or max(1, math.ceil((len(prompt) + 360) / 4)))
    output_tokens = int(usage.get("completion_tokens") or max(1, math.ceil(len(response_text) / 4)))
    tps = output_tokens / elapsed
    cost = (
        input_tokens / 1_000_000 * model["input_cost_per_m"]
        + output_tokens / 1_000_000 * model["output_cost_per_m"]
    )
    return {
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "policy": "Live response",
        "provider": model["vendor"],
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "ttft": round(ttft, 4),
        "tps": round(tps, 4),
        "cost_usd": f"{cost:.6f}",
        "cost_per_1k_calls": f"{cost * 1000:.6f}",
        "model_id": model_id,
        "model_name": model["name"],
        "mode": "Live",
        "response_text": response_text,
    }


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "ModelCostDashboard/1.0"

    def log_message(self, _format, *_args):
        return

    def _send(self, status, body, content_type="application/json; charset=utf-8", extra_headers=None):
        if isinstance(body, str):
            encoded = body.encode("utf-8")
        else:
            encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
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
        if self.path == "/healthz":
            self._send(200, {"status": "ok"})
            return
        if self.path.startswith("/api/") and not self._authorized():
            return
        if self.path == "/":
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif self.path == "/manifest.webmanifest":
            self._send(200, APP_MANIFEST, "application/manifest+json; charset=utf-8")
        elif self.path == "/service-worker.js":
            self._send(
                200,
                SERVICE_WORKER,
                "application/javascript; charset=utf-8",
                {"Service-Worker-Allowed": "/"},
            )
        elif self.path == "/app-icon.svg":
            self._send(200, APP_ICON, "image/svg+xml; charset=utf-8")
        elif self.path == "/api/state":
            self._send(200, app_state())
        else:
            self._send(404, {"error": "Not found."})

    def do_POST(self):
        global MODELS, SYNC_STATUS, SYNC_ERROR
        try:
            if not self._authorized():
                return
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
            elif self.path == "/api/charts":
                provider_filter = data.get("provider", "")
                mode_filter = data.get("mode", "")
                if not isinstance(provider_filter, str) or not isinstance(mode_filter, str):
                    raise ValueError("Chart filters must be text values.")
                with STATE_LOCK:
                    rows = [row.copy() for row in TELEMETRY]
                charts = build_chart_svgs(rows, provider_filter, mode_filter)
                self._send(200, charts)
            elif self.path == "/api/import":
                result = self._import_csv(data)
                self._send(200, result)
            elif self.path == "/api/clear":
                clear_persisted_rows()
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
        mode = data.get("mode", "simulation")
        if mode not in ("simulation", "live"):
            raise ValueError("Select simulation or live response mode.")
        if mode == "live":
            if count != 1:
                raise ValueError("Live mode only supports one request at a time.")
            if not os.environ.get("OPENROUTER_API_KEY", "").strip():
                raise ValueError("Live mode requires OPENROUTER_API_KEY on the server.")
            if not DASHBOARD_PASSWORD.strip():
                raise ValueError(
                    "Live mode is disabled on an unprotected public app. Set DASHBOARD_PASSWORD first."
                )
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

        if count * len(targets) > 100:
            raise ValueError(
                "This selection would create too many executions at once (maximum 100)."
            )
        if (
            mode == "live"
            and policy == "Broadcast All"
            and not data.get("confirm_broadcast")
        ):
            raise ValueError(
                "Confirm the possible provider charges before using live Broadcast All."
            )
        if mode == "live":
            futures = [
                SIMULATOR_POOL.submit(
                    call_openrouter, model_id, available[model_id], prompt
                )
                for model_id in targets
            ]
        else:
            futures = [
                SIMULATOR_POOL.submit(
                    simulate_one, model_id, available[model_id], prompt, policy
                )
                for _ in range(count) for model_id in targets
            ]
        rows = [future.result() for future in futures]
        if mode == "live":
            for row in rows:
                row["policy"] = policy
        persist_rows(rows)
        with STATE_LOCK:
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
        persist_rows(valid)
        result = app_state()
        result["imported"] = len(valid)
        result["skipped"] = skipped
        return result

    def _authorized(self):
        if not DASHBOARD_PASSWORD.strip():
            return True
        supplied = self.headers.get("X-Dashboard-Password", "")
        if hmac.compare_digest(
            supplied.encode("utf-8"), DASHBOARD_PASSWORD.encode("utf-8")
        ):
            return True
        self._send(401, {"error": "Dashboard password required."})
        return False


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
