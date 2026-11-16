import csv
import json
import math
import random
import threading
import time
from datetime import datetime
from tkinter import filedialog, messagebox, ttk
import tkinter as tk
from urllib.request import Request, urlopen

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure


BG = "#c2cfe6"
CARD = "#a4c0e8"
PANEL = "#191d23"
TEXT = "#010913"
MUTED = "#a0a7b2"
BLUE = "#78a6b8"
GREEN = "#76a995"
GRID = "#39414b"

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
        "name": "GPT-4o",
        "vendor": "OpenAI",
        "input_cost_per_m": 2.50,
        "output_cost_per_m": 10.00,
        "base_ttft": 0.42,
        "base_tps": 72.0,
    },
    "anthropic/claude-3.5-sonnet": {
        "name": "Claude 3.5 Sonnet",
        "vendor": "Anthropic",
        "input_cost_per_m": 3.00,
        "output_cost_per_m": 15.00,
        "base_ttft": 0.56,
        "base_tps": 58.0,
    },
    "google/gemini-pro-1.5": {
        "name": "Gemini 1.5 Pro",
        "vendor": "Google",
        "input_cost_per_m": 1.25,
        "output_cost_per_m": 5.00,
        "base_ttft": 0.38,
        "base_tps": 66.0,
    },
    "meta-llama/llama-3.3-70b-instruct": {
        "name": "Llama 3.3 70B Instruct",
        "vendor": "Meta",
        "input_cost_per_m": 0.12,
        "output_cost_per_m": 0.30,
        "base_ttft": 0.24,
        "base_tps": 82.0,
    },
    "deepseek/deepseek-r1": {
        "name": "DeepSeek R1",
        "vendor": "DeepSeek",
        "input_cost_per_m": 0.55,
        "output_cost_per_m": 2.19,
        "base_ttft": 0.82,
        "base_tps": 42.0,
    },
    "deepseek/deepseek-chat-v3": {
        "name": "DeepSeek V3",
        "vendor": "DeepSeek",
        "input_cost_per_m": 0.27,
        "output_cost_per_m": 1.10,
        "base_ttft": 0.34,
        "base_tps": 76.0,
    },
    "mistralai/mistral-large": {
        "name": "Mistral Large",
        "vendor": "Mistral",
        "input_cost_per_m": 2.00,
        "output_cost_per_m": 6.00,
        "base_ttft": 0.48,
        "base_tps": 54.0,
    },
}

CURATED_MARKERS = (
    "gpt-4o",
    "claude-3.5-sonnet",
    "gemini-pro-1.5",
    "llama-3.3-70b",
    "deepseek-r1",
    "deepseek-chat-v3",
    "deepseek-v3",
    "mistral-large",
)


def display_vendor(model_id, api_model):
    vendor = api_model.get("vendor")
    if isinstance(vendor, str) and vendor.strip():
        return vendor.strip()
    prefix = model_id.split("/", 1)[0].replace("-", " ").replace("_", " ")
    return " ".join(part.capitalize() for part in prefix.split()) or "Unknown"


def sync_openrouter_catalog():
    request = Request(
        "https://openrouter.ai/api/v1/models",
        headers={"User-Agent": "Enterprise-LLM-Price-Performance-Control-Plane/1.0"},
    )
    with urlopen(request, timeout=5) as response:
        payload = json.loads(response.read().decode("utf-8"))
    raw_models = payload.get("data")
    if not isinstance(raw_models, list):
        raise ValueError("OpenRouter response did not contain a model catalog.")

    synced = {}
    for api_model in raw_models:
        model_id = api_model.get("id")
        pricing = api_model.get("pricing") or {}
        if not isinstance(model_id, str) or not isinstance(pricing, dict):
            continue
        try:
            input_cost = float(pricing["prompt"]) * 1_000_000
            output_cost = float(pricing["completion"]) * 1_000_000
        except (KeyError, TypeError, ValueError):
            continue
        if (
            not math.isfinite(input_cost)
            or not math.isfinite(output_cost)
            or input_cost < 0
            or output_cost < 0
        ):
            continue

        model_name = api_model.get("name")
        if not isinstance(model_name, str) or not model_name.strip():
            model_name = model_id
        markers = model_id.lower()
        is_curated = any(marker in markers for marker in CURATED_MARKERS)
        if is_curated:
            base_ttft = 0.5
            base_tps = 55.0
            for fallback_id, profile in FALLBACK_MODELS.items():
                if fallback_id.split("/", 1)[1] in markers:
                    base_ttft = profile["base_ttft"]
                    base_tps = profile["base_tps"]
                    break
        else:
            base_ttft = 0.5
            base_tps = 55.0

        synced[model_id] = {
            "name": model_name.strip(),
            "vendor": display_vendor(model_id, api_model),
            "input_cost_per_m": input_cost,
            "output_cost_per_m": output_cost,
            "base_ttft": base_ttft,
            "base_tps": base_tps,
        }

    if not synced:
        raise ValueError("OpenRouter returned no models with usable token pricing.")
    return synced


class PricePerformanceApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Model Cost and Performance")
        self.root.geometry("1420x900")
        self.root.minsize(1120, 740)
        self.root.configure(bg=BG)
        self.models = {key: value.copy() for key, value in FALLBACK_MODELS.items()}
        self.selected_model_ids = set(FALLBACK_MODELS)
        self.telemetry = []
        self.pending_jobs = 0
        self.sync_in_progress = False

        self._configure_theme()
        self._build_layout()
        self._refresh_model_table()
        self._refresh_charts()
        self._append_audit("Application ready. Offline tariff profiles are loaded.")
        self.root.after(0, self.sync_rates)

    def _configure_theme(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(
            ".",
            background=BG,
            foreground=TEXT,
            fieldbackground=PANEL,
            bordercolor=GRID,
            lightcolor=GRID,
            darkcolor=GRID,
            font=("Segoe UI", 9),
        )
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Card.TLabel", background=CARD, foreground=TEXT)
        style.configure("Muted.TLabel", background=CARD, foreground=MUTED)
        style.configure(
            "Title.TLabel",
            background=BG,
            foreground=TEXT,
            font=("Segoe UI Semibold", 16),
        )
        style.configure(
            "Section.TLabel",
            background=CARD,
            foreground=TEXT,
            font=("Segoe UI Semibold", 11),
        )
        style.configure(
            "TButton",
            background="#303740",
            foreground=TEXT,
            padding=(10, 7),
            borderwidth=0,
        )
        style.map(
            "TButton",
            background=[("active", "#424b56"), ("disabled", "#282e36")],
            foreground=[("disabled", "#737d89")],
        )
        style.configure(
            "Accent.TButton",
            background="#4c7887",
            foreground="#f2f5f6",
            font=("Segoe UI Semibold", 9),
        )
        style.map("Accent.TButton", background=[("active", "#5b8998")])
        style.configure(
            "Treeview",
            background=PANEL,
            fieldbackground=PANEL,
            foreground=TEXT,
            rowheight=25,
            borderwidth=0,
        )
        style.configure(
            "Treeview.Heading",
            background="#303740",
            foreground=TEXT,
            font=("Segoe UI Semibold", 9),
            relief="flat",
        )
        style.map(
            "Treeview",
            background=[("selected", "#3b5965")],
            foreground=[("selected", "#f2f5f6")],
        )
        style.configure(
            "TCombobox",
            fieldbackground=PANEL,
            background="#303740",
            foreground=TEXT,
            arrowcolor=TEXT,
        )

    def _build_layout(self):
        header = ttk.Frame(self.root, padding=(20, 15, 20, 13))
        header.pack(fill="x")
        title_block = ttk.Frame(header)
        title_block.pack(side="left")
        ttk.Label(
            title_block, text="Model cost and performance", style="Title.TLabel"
        ).pack(anchor="w")
        ttk.Label(
            title_block, text="Routing, benchmark telemetry, and tariff comparison"
        ).pack(anchor="w", pady=(2, 0))
        self.status_label = tk.Label(
            header,
            text="Offline tariffs",
            bg="#343b44",
            fg="#d1d6dc",
            font=("Segoe UI", 9),
            padx=9,
            pady=5,
        )
        self.status_label.pack(side="right", padx=(12, 0))
        ttk.Button(header, text="Synchronize rates", command=self.sync_rates).pack(
            side="right", padx=(0, 8)
        )
        ttk.Button(header, text="Export CSV", command=self.export_csv).pack(
            side="right", padx=(0, 8)
        )
        ttk.Button(header, text="Import CSV", command=self.import_csv).pack(
            side="right", padx=(0, 8)
        )

        body = ttk.Panedwindow(self.root, orient="horizontal")
        body.pack(fill="both", expand=True, padx=20, pady=(0, 18))
        left = ttk.Frame(body, style="Card.TFrame", padding=16)
        right = ttk.Frame(body, style="Card.TFrame", padding=16)
        body.add(left, weight=0)
        body.add(right, weight=1)

        ttk.Label(left, text="Request", style="Section.TLabel").pack(anchor="w")
        ttk.Label(
            left,
            text="Run a simulated workload against selected models.",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(3, 10))
        ttk.Label(left, text="Prompt", style="Card.TLabel").pack(
            anchor="w", pady=(0, 4)
        )
        self.prompt_text = tk.Text(
            left,
            height=6,
            wrap="word",
            bg=PANEL,
            fg=TEXT,
            insertbackground=TEXT,
            selectbackground="#3b5965",
            relief="flat",
            padx=9,
            pady=8,
            font=("Segoe UI", 10),
        )
        self.prompt_text.pack(fill="x")
        ttk.Label(left, text="Routing policy", style="Card.TLabel").pack(
            anchor="w", pady=(12, 5)
        )
        self.policy_var = tk.StringVar(value="Lowest Cost")
        policy_box = ttk.Combobox(
            left,
            textvariable=self.policy_var,
            values=("Broadcast All", "Lowest Cost", "Lowest Latency"),
            state="readonly",
        )
        policy_box.pack(fill="x")
        self.execute_button = ttk.Button(
            left,
            text="Execute Request",
            style="Accent.TButton",
            command=lambda: self.execute_requests(1),
        )
        self.execute_button.pack(fill="x", pady=(11, 6))
        self.stress_button = ttk.Button(
            left,
            text="Run 10-request stress test",
            command=lambda: self.execute_requests(10),
        )
        self.stress_button.pack(fill="x")
        ttk.Button(left, text="Clear Session", command=self.clear_session).pack(
            fill="x", pady=(6, 12)
        )
        ttk.Label(left, text="Model rates", style="Section.TLabel").pack(
            anchor="w", pady=(0, 4)
        )
        ttk.Label(
            left,
            text="Select one or more models. Use Ctrl-click to change the selection.",
            style="Muted.TLabel",
            wraplength=360,
        ).pack(anchor="w", pady=(0, 6))
        table_frame = ttk.Frame(left, style="Card.TFrame")
        table_frame.pack(fill="both", expand=True)
        self.model_tree = ttk.Treeview(
            table_frame,
            columns=("vendor", "input", "output", "ttft"),
            show="tree headings",
            selectmode="extended",
            height=14,
        )
        self.model_tree.heading("#0", text="Model")
        self.model_tree.heading("vendor", text="Vendor")
        self.model_tree.heading("input", text="In $/M")
        self.model_tree.heading("output", text="Out $/M")
        self.model_tree.heading("ttft", text="TTFT")
        self.model_tree.column("#0", width=190, minwidth=130)
        self.model_tree.column("vendor", width=80, minwidth=65)
        self.model_tree.column("input", width=70, anchor="e", minwidth=60)
        self.model_tree.column("output", width=75, anchor="e", minwidth=60)
        self.model_tree.column("ttft", width=55, anchor="e", minwidth=50)
        tree_scroll = ttk.Scrollbar(
            table_frame, orient="vertical", command=self.model_tree.yview
        )
        self.model_tree.configure(yscrollcommand=tree_scroll.set)
        self.model_tree.pack(side="left", fill="both", expand=True)
        tree_scroll.pack(side="right", fill="y")
        self.model_tree.bind("<<TreeviewSelect>>", self._on_model_selection)

        ttk.Label(right, text="Performance", style="Section.TLabel").pack(
            anchor="w", padx=4, pady=(0, 6)
        )
        chart_frame = ttk.Frame(right, style="Card.TFrame")
        chart_frame.pack(fill="both", expand=True)
        self.figure = Figure(figsize=(10.5, 4.7), dpi=100, facecolor=CARD)
        self.ax_tps = self.figure.add_subplot(1, 2, 1)
        self.ax_cost = self.figure.add_subplot(1, 2, 2)
        self.figure.subplots_adjust(left=0.15, right=0.98, top=0.88, bottom=0.2, wspace=0.48)
        self.canvas = FigureCanvasTkAgg(self.figure, master=chart_frame)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)

        terminal_header = ttk.Frame(right, style="Card.TFrame")
        terminal_header.pack(fill="x", pady=(9, 5))
        ttk.Label(
            terminal_header, text="Execution log", style="Section.TLabel"
        ).pack(side="left")
        self.telemetry_count_label = ttk.Label(
            terminal_header, text="0 executions", style="Muted.TLabel"
        )
        self.telemetry_count_label.pack(side="right")
        audit_frame = ttk.Frame(right, style="Card.TFrame")
        audit_frame.pack(fill="both", expand=True)
        self.audit_text = tk.Text(
            audit_frame,
            height=11,
            wrap="word",
            state="disabled",
            bg="#171b20",
            fg="#c8d8d1",
            insertbackground=TEXT,
            selectbackground="#3b5965",
            relief="flat",
            padx=10,
            pady=8,
            font=("Segoe UI", 9),
        )
        audit_scroll = ttk.Scrollbar(
            audit_frame, orient="vertical", command=self.audit_text.yview
        )
        self.audit_text.configure(yscrollcommand=audit_scroll.set)
        self.audit_text.pack(side="left", fill="both", expand=True)
        audit_scroll.pack(side="right", fill="y")

    def _refresh_model_table(self):
        for item in self.model_tree.get_children():
            self.model_tree.delete(item)
        sorted_ids = sorted(
            self.models,
            key=lambda model_id: (
                0 if model_id in FALLBACK_MODELS else 1,
                self.models[model_id]["vendor"].lower(),
                self.models[model_id]["name"].lower(),
            ),
        )
        for model_id in sorted_ids:
            model = self.models[model_id]
            self.model_tree.insert(
                "",
                "end",
                iid=model_id,
                text=model["name"],
                values=(
                    model["vendor"],
                    f'{model["input_cost_per_m"]:.4f}',
                    f'{model["output_cost_per_m"]:.4f}',
                    f'{model["base_ttft"]:.2f}s',
                ),
            )
        for model_id in self.selected_model_ids:
            if self.model_tree.exists(model_id):
                self.model_tree.selection_add(model_id)
        self.model_tree.selection_set(
            [model_id for model_id in self.selected_model_ids if self.model_tree.exists(model_id)]
        )

    def _on_model_selection(self, _event=None):
        selected = set(self.model_tree.selection())
        if selected:
            self.selected_model_ids = selected
        else:
            self.selected_model_ids = set()

    def _append_audit(self, message):
        timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
        self.audit_text.configure(state="normal")
        self.audit_text.insert(tk.END, f"[{timestamp}] {message}\n")
        self.audit_text.see(tk.END)
        self.audit_text.configure(state="disabled")

    def sync_rates(self):
        if self.sync_in_progress:
            return
        self.sync_in_progress = True
        self.status_label.configure(text="SYNCING", bg="#4a4130", fg="#f0dfb8")
        self._append_audit("Synchronizing rates from OpenRouter.")

        def worker():
            try:
                catalog = sync_openrouter_catalog()
                error = None
            except Exception as exc:
                catalog = None
                error = str(exc)
            self.root.after(0, self._finish_sync, catalog, error)

        threading.Thread(target=worker, name="openrouter-catalog-sync", daemon=True).start()

    def _finish_sync(self, catalog, error):
        self.sync_in_progress = False
        if catalog:
            self.models.update(catalog)
            self.status_label.configure(
                text="LIVE SYNC", bg="#30483f", fg="#d0e5d9"
            )
            self._append_audit(
                f"Live sync complete: {len(catalog)} priced models loaded; "
                "new catalog models are available for activation."
            )
        else:
            self.status_label.configure(
                text="OFFLINE FALLBACK", bg="#4a3a3b", fg="#ead3d3"
            )
            detail = error or "No live model data was returned."
            self._append_audit(f"Live sync unavailable; using offline tariff profiles ({detail}).")
        self._refresh_model_table()
        self._refresh_charts()

    def _active_model_ids(self):
        return [
            model_id
            for model_id in self.model_tree.selection()
            if model_id in self.models
        ]

    def _route_targets(self, model_ids, policy):
        if policy == "Broadcast All":
            return model_ids
        if policy == "Lowest Cost":
            target = min(
                model_ids,
                key=lambda model_id: (
                    self.models[model_id]["input_cost_per_m"]
                    + self.models[model_id]["output_cost_per_m"]
                ),
            )
            return [target]
        if policy == "Lowest Latency":
            return [
                min(model_ids, key=lambda model_id: self.models[model_id]["base_ttft"])
            ]
        raise ValueError(f"Unknown routing policy: {policy}")

    def execute_requests(self, count):
        prompt = self.prompt_text.get("1.0", "end-1c").strip()
        if not prompt:
            messagebox.showwarning("Prompt required", "Enter a prompt before executing.")
            return
        model_ids = self._active_model_ids()
        if not model_ids:
            messagebox.showwarning("No active models", "Sync the catalog or activate a model.")
            return
        policy = self.policy_var.get()
        try:
            targets = self._route_targets(model_ids, policy)
        except ValueError as exc:
            messagebox.showerror("Routing error", str(exc))
            return
        if not targets:
            messagebox.showwarning("No route targets", "No models are available for this policy.")
            return

        jobs = [(model_id, prompt, policy, run_index) for run_index in range(count) for model_id in targets]
        self.pending_jobs += len(jobs)
        self._set_execution_buttons()
        self._append_audit(
            f"Routing policy={policy}; target count={len(targets)}; "
            f"{count} workload(s) queued."
        )
        for model_id, workload, selected_policy, run_index in jobs:
            model = self.models[model_id].copy()
            thread = threading.Thread(
                target=self._simulate_execution,
                args=(model_id, model, workload, selected_policy, run_index),
                name=f"simulation-{run_index}-{model_id}",
                daemon=True,
            )
            thread.start()

    def _simulate_execution(self, model_id, model, prompt, policy, run_index):
        input_tokens = max(1, math.ceil((len(prompt) + 360) / 4))
        input_tokens = max(1, round(input_tokens * random.uniform(0.94, 1.08)))
        output_tokens = max(32, round(random.gauss(190, 45)))
        network_jitter = random.uniform(0.82, 1.42)
        ttft = max(0.025, model["base_ttft"] * network_jitter * random.uniform(0.9, 1.1))
        effective_tps = max(3.0, model["base_tps"] * random.uniform(0.78, 1.18))
        total_round_trip_time = ttft + output_tokens / effective_tps
        measured_tps = output_tokens / total_round_trip_time
        exact_cost = (
            input_tokens / 1_000_000 * model["input_cost_per_m"]
            + output_tokens / 1_000_000 * model["output_cost_per_m"]
        )
        cost_usd = round(exact_cost, 6)
        row = {
            "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
            "policy": policy,
            "provider": model["vendor"],
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "ttft": round(ttft, 4),
            "tps": round(measured_tps, 4),
            "cost_usd": f"{cost_usd:.6f}",
            "cost_per_1k_calls": f"{cost_usd * 1000:.6f}",
            "model_id": model_id,
            "model_name": model["name"],
        }
        time.sleep(random.uniform(0.04, 0.18))
        self.root.after(0, self._accept_execution, row, run_index)

    def _accept_execution(self, row, run_index):
        self.telemetry.append(row)
        self.pending_jobs = max(0, self.pending_jobs - 1)
        self._append_audit(
            f'{row["policy"]} | {row["model_name"]} ({row["provider"]}) | '
            f'TTFT {float(row["ttft"]):.3f}s | TPS {float(row["tps"]):.2f} | '
            f'cost ${float(row["cost_usd"]):.6f} | workload {run_index + 1}'
        )
        self.telemetry_count_label.configure(
            text=f"{len(self.telemetry):,} executions"
        )
        self._refresh_charts()
        self._set_execution_buttons()

    def _set_execution_buttons(self):
        state = "disabled" if self.pending_jobs else "normal"
        self.execute_button.configure(state=state)
        self.stress_button.configure(state=state)

    def _refresh_charts(self):
        self.ax_tps.clear()
        self.ax_cost.clear()
        for axis in (self.ax_tps, self.ax_cost):
            axis.set_facecolor(CARD)
            axis.tick_params(colors=MUTED, labelsize=8)
            axis.xaxis.label.set_color(MUTED)
            axis.yaxis.label.set_color(MUTED)
            axis.title.set_color(TEXT)
            for spine in axis.spines.values():
                spine.set_color(GRID)
            axis.grid(axis="x", color=GRID, linewidth=0.7, alpha=0.8)
            axis.set_axisbelow(True)

        grouped = {}
        for row in self.telemetry:
            provider = row["provider"]
            values = grouped.setdefault(provider, {"tps": [], "cost": []})
            values["tps"].append(float(row["tps"]))
            values["cost"].append(float(row["cost_usd"]) * 1000)
        if not grouped:
            self.ax_tps.set_title("Average throughput by provider")
            self.ax_tps.set_xlabel("Tokens / second")
            self.ax_tps.text(
                0.5, 0.5, "Run a workload to populate analytics", color=MUTED,
                ha="center", va="center", transform=self.ax_tps.transAxes,
            )
            self.ax_cost.set_title("Average cost per 1,000 requests")
            self.ax_cost.set_xlabel("USD per 1K calls")
            self.ax_cost.text(
                0.5, 0.5, "No telemetry yet", color=MUTED,
                ha="center", va="center", transform=self.ax_cost.transAxes,
            )
        else:
            ordered_tps = sorted(
                grouped.items(), key=lambda item: sum(item[1]["tps"]) / len(item[1]["tps"])
            )[-12:]
            ordered_cost = sorted(
                grouped.items(), key=lambda item: sum(item[1]["cost"]) / len(item[1]["cost"])
            )[:12]
            tps_names = [name for name, _ in ordered_tps]
            tps_values = [sum(v["tps"]) / len(v["tps"]) for _, v in ordered_tps]
            cost_names = [name for name, _ in ordered_cost]
            cost_values = [sum(v["cost"]) / len(v["cost"]) for _, v in ordered_cost]
            self.ax_tps.barh(tps_names, tps_values, color=BLUE, height=0.65)
            self.ax_tps.set_title("Average throughput by provider")
            self.ax_tps.set_xlabel("Tokens / second")
            self.ax_tps.grid(axis="y", visible=False)
            self.ax_cost.barh(cost_names, cost_values, color=GREEN, height=0.65)
            self.ax_cost.set_title("Average cost per 1,000 requests")
            self.ax_cost.set_xlabel("USD per 1K calls")
            self.ax_cost.invert_yaxis()
            self.ax_cost.grid(axis="y", visible=False)

        self.canvas.draw_idle()

    def clear_session(self):
        if self.pending_jobs:
            messagebox.showinfo(
                "Executions in progress",
                "Wait for active simulations to finish before clearing the session.",
            )
            return
        self.telemetry.clear()
        self.telemetry_count_label.configure(text="0 executions")
        self._append_audit("Session telemetry cleared; tariff catalog retained.")
        self._refresh_charts()

    def import_csv(self):
        filename = filedialog.askopenfilename(
            title="Import historical telemetry",
            filetypes=(("CSV files", "*.csv"), ("All files", "*.*")),
        )
        if not filename:
            return
        required = {
            "timestamp",
            "policy",
            "provider",
            "input_tokens",
            "output_tokens",
            "ttft",
            "tps",
            "cost_usd",
        }
        valid_rows = []
        invalid_rows = 0
        try:
            with open(filename, "r", newline="", encoding="utf-8-sig") as file:
                reader = csv.DictReader(file)
                headers = set(reader.fieldnames or [])
                missing = sorted(required - headers)
                if missing:
                    messagebox.showerror(
                        "Invalid CSV",
                        "Missing required columns: " + ", ".join(missing),
                    )
                    return
                for source_row in reader:
                    try:
                        normalized = self._normalize_import_row(source_row)
                    except (TypeError, ValueError, KeyError):
                        invalid_rows += 1
                        continue
                    valid_rows.append(normalized)
        except (OSError, csv.Error, UnicodeError) as exc:
            messagebox.showerror("Import failed", f"Could not read the CSV file:\n{exc}")
            return

        self.telemetry.extend(valid_rows)
        self.telemetry_count_label.configure(text=f"{len(self.telemetry):,} executions")
        self._append_audit(
            f"Imported {len(valid_rows)} historical telemetry row(s); "
            f"{invalid_rows} invalid row(s) skipped."
        )
        self._refresh_charts()
        messagebox.showinfo(
            "Import complete",
            f"Imported {len(valid_rows)} valid row(s). Skipped {invalid_rows} invalid row(s).",
        )

    def _normalize_import_row(self, source_row):
        provider = str(source_row["provider"]).strip()
        timestamp = str(source_row["timestamp"]).strip()
        policy = str(source_row["policy"]).strip()
        if not provider or not timestamp or not policy:
            raise ValueError("Required text value is empty.")
        input_tokens = int(source_row["input_tokens"])
        output_tokens = int(source_row["output_tokens"])
        ttft = float(source_row["ttft"])
        tps = float(source_row["tps"])
        cost = float(source_row["cost_usd"])
        if (
            input_tokens < 0
            or output_tokens < 0
            or not all(math.isfinite(value) for value in (ttft, tps, cost))
            or ttft < 0
            or tps < 0
            or cost < 0
        ):
            raise ValueError("Telemetry values must be finite and non-negative.")
        model_id = str(source_row.get("model_id") or "").strip()
        model_name = str(source_row.get("model_name") or model_id or provider).strip()
        return {
            "timestamp": timestamp,
            "policy": policy,
            "provider": provider,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "ttft": round(ttft, 4),
            "tps": round(tps, 4),
            "cost_usd": f"{cost:.6f}",
            "cost_per_1k_calls": f"{cost * 1000:.6f}",
            "model_id": model_id,
            "model_name": model_name,
        }

    def export_csv(self):
        if not self.telemetry:
            messagebox.showinfo("Nothing to export", "No session telemetry is available.")
            return
        filename = filedialog.asksaveasfilename(
            title="Export session telemetry",
            defaultextension=".csv",
            filetypes=(("CSV files", "*.csv"),),
            initialfile="llm_telemetry.csv",
        )
        if not filename:
            return
        try:
            with open(filename, "w", newline="", encoding="utf-8-sig") as file:
                writer = csv.DictWriter(file, fieldnames=CSV_COLUMNS, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(self.telemetry)
        except (OSError, csv.Error) as exc:
            messagebox.showerror("Export failed", f"Could not write the CSV file:\n{exc}")
            return
        self._append_audit(f"Exported {len(self.telemetry)} telemetry row(s) to {filename}.")
        messagebox.showinfo("Export complete", f"Exported {len(self.telemetry)} row(s).")


def main():
    root = tk.Tk()
    PricePerformanceApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
