import tkinter as tk
from tkinter import ttk, messagebox
import threading
import json
import os
import time
import re
import queue
import yfinance as yf
import pandas as pd
import datetime

# --- configuration & Constants ---
CACHE_FILE = ".cache/us_top100_tickers.json"
STRATEGY_PARAMS_FILE = ".cache/tfmr_strategy_params.json"
BROKER_PROFILES_FILE = ".cache/broker_profiles.json"
BROKER_PROFILE_STATE_FILE = ".cache/broker_profile_state.json"
MAX_HISTORY = "25y"
INTERVAL = "1wk"
NETWORK_TIMEOUT_SEC = 12
NETWORK_RETRIES = 2
SETTINGS_RECALC_DEBOUNCE_MS = 400

DEFAULT_STRATEGY_PARAMS = {
    "gc_fast_ma": 20,
    "gc_slow_ma": 50,
    "pullback_short_ma": 5,
    "pullback_base_ma": 20,
    "long_fast_ma": 150,
    "long_slow_ma": 200,
    "target_pullback_no": 1,
    "step_drop_pct": 3.0,
    "require_long_ma_order": True,
    "require_close_above_long_ma": True,
    "require_bearish_entry": True,
}

# KR online (US stock) fee model defaults
DEFAULT_BUY_FEE_RATE = 0.0007    # 0.0700%
DEFAULT_SELL_FEE_RATE = 0.000708 # 0.0708%
KR_MIN_BROKER_FEE_USD = 0.01
KR_SEC_FEE_RATE_BEFORE_2025_05_13 = 0.0000278
KR_SEC_FEE_MIN_USD = 0.01
KR_SEC_FEE_ZERO_FROM = datetime.date(2025, 5, 13)
KR_TAF_FEE_PER_SHARE_USD = 0.000166
KR_TAF_FEE_MIN_USD = 0.01
KR_TAF_FEE_MAX_USD = 8.30

DEFAULT_BROKER_PROFILES = {
    "KakaoPay": {
        "buy_fee_rate": DEFAULT_BUY_FEE_RATE,
        "sell_fee_rate": DEFAULT_SELL_FEE_RATE,
        "use_kr_fee_model": False,
    },
    "KIS": {
        "buy_fee_rate": 0.0025,
        "sell_fee_rate": 0.002508,
        "use_kr_fee_model": False,
    },
    "Custom": {
        "buy_fee_rate": DEFAULT_BUY_FEE_RATE,
        "sell_fee_rate": DEFAULT_SELL_FEE_RATE,
        "use_kr_fee_model": False,
    },
}

# Charting imports
import matplotlib
matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
import mplfinance as mpf
import matplotlib.pyplot as plt

# Hardcoded fallback list of ~100 top US stocks
FALLBACK_TOP100 = [
    "MSFT", "AAPL", "NVDA", "GOOG", "AMZN", "META", "BRK-B", "LLY", "TSLA", "AVGO",
    "JPM", "V", "UNH", "WMT", "MA", "XOM", "JNJ", "PG", "HD", "COST",
    "MRK", "ABBV", "CVX", "BAC", "CRM", "AMD", "PEP", "KO", "NFLX", "ADBE",
    "DIS", "TMO", "WFC", "LIN", "MCD", "CSCO", "ABT", "INTU", "QCOM", "CAT",
    "IBM", "GE", "VZ", "AMGN", "NOW", "UBER", "INTC", "TXN", "DHR", "SPGI",
    "PM", "ISRG", "UNP", "HON", "PFE", "COP", "LOW", "BKNG", "RTX", "AMAT",
    "SYK", "NKE", "GS", "ELV", "BLK", "PLD", "MDT", "BA", "TJX", "AXP",
    "DE", "SCHW", "LMT", "MS", "NEE", "C", "BMY", "VRTX", "ADI", "ADP",
    "MMC", "ZTS", "MDLZ", "CI", "GILD", "BX", "LRCX", "TMUS", "REGN",
    "SLB", "CVS", "MO", "SO", "BSX", "EOG", "PANW", "MU", "KLAC", "SNPS"
]

class TFMRScannerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("HTS TFMR Scanner (Simulated) + Backtest")
        self.root.geometry("1800x900") # Larger window for chart + detail panel
        self.root.report_callback_exception = self._report_tk_exception

        self.candidates = []
        self.all_tickers = []
        self.candidate_data = {} # Cache df for chart/backtest
        self.ticker_names = {} # Cache full names

        self.is_running = True
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.canvas_widget = None
        self.toolbar_widget = None # Track toolbar
        self._sel_job = None # Debounce selection job
        self.chart_markers = []
        self.chart_annotations = []
        self.backtest_logs = []
        self.strategy_params = self.load_strategy_params()
        self.broker_profiles = self.load_broker_profiles()
        self.active_broker_profile = self.load_broker_profile_selection()
        self._setting_recalc_job = None
        self._setting_recalc_token = 0
        self._ui_queue = queue.Queue()
        self._ui_pump_job = None
        self._worker_threads = set()
        self._worker_lock = threading.Lock()

        self._setup_ui()
        self._ui_pump_job = self.root.after(50, self._process_ui_queue)
        # self.load_tickers_from_cache() # REMOVED: Start empty per user request
        
        # Auto-Refresh on startup (User Request)
        self.root.after(500, self.refresh_top100)

    def on_close(self):
        """Handle window close event."""
        if not self.is_running:
            return
        self.is_running = False
        try:
            if self._setting_recalc_job:
                self.root.after_cancel(self._setting_recalc_job)
                self._setting_recalc_job = None
        except Exception:
            pass
        try:
            if self._sel_job:
                self.root.after_cancel(self._sel_job)
                self._sel_job = None
        except Exception:
            pass
        try:
            if self._ui_pump_job:
                self.root.after_cancel(self._ui_pump_job)
                self._ui_pump_job = None
        except Exception:
            pass

        # Wait briefly for worker threads to stop to avoid Tk teardown races.
        with self._worker_lock:
            workers = list(self._worker_threads)
        for t in workers:
            try:
                t.join(timeout=1.5)
            except Exception:
                pass

        try:
            plt.close("all")
        except Exception:
            pass
        try:
            self.root.quit()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass

    def _start_worker(self, target, args=(), name="worker"):
        def _run():
            try:
                target(*args)
            finally:
                with self._worker_lock:
                    self._worker_threads.discard(thread)

        thread = threading.Thread(target=_run, name=name, daemon=False)
        with self._worker_lock:
            self._worker_threads.add(thread)
        thread.start()
        return thread

    def _post_ui(self, fn):
        if not self.is_running:
            return
        self._ui_queue.put(fn)

    def _post_ui_event(self, event_name, *payload):
        if not self.is_running:
            return
        self._ui_queue.put(("event", event_name, payload))

    def _process_ui_queue(self):
        if not self.is_running:
            return
        while True:
            try:
                item = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                if isinstance(item, tuple) and len(item) == 3 and item[0] == "event":
                    _, event_name, payload = item
                    self._dispatch_ui_event(event_name, payload)
                else:
                    fn = item
                    fn()
            except Exception as e:
                print(f"UI dispatch error: {e}")
        if self.is_running:
            self._ui_pump_job = self.root.after(50, self._process_ui_queue)

    def _dispatch_ui_event(self, event_name, payload):
        if event_name == "setting_recalc_result":
            self._apply_setting_recalc_result(*payload)
            return
        if event_name == "top100_success":
            count, source_msg = payload
            self.lbl_status.config(text=f"Refreshed US market-cap Top {count} ({source_msg}).")
            self.btn_refresh.state(["!disabled"])
            self.mode_var.set("ALL")
            self.update_list_display()
            return
        if event_name == "top100_failure":
            err_msg, status_msg = payload
            messagebox.showerror("Fetch Error", f"Failed to fetch live list: {err_msg}")
            self.lbl_status.config(text=status_msg)
            self.btn_refresh.state(["!disabled"])
            self.mode_var.set("ALL")
            self.update_list_display()
            return
        if event_name == "scan_progress":
            (msg,) = payload
            self.lbl_status.config(text=msg)
            return
        if event_name == "scan_add_candidate":
            (ticker,) = payload
            self.add_candidate(ticker)
            return
        if event_name == "scan_complete":
            (found_count,) = payload
            self.lbl_status.config(text=f"Scan Complete. Found {found_count} candidates.")
            self.btn_scan.state(["!disabled"])
            return

    def _report_tk_exception(self, exc, val, tb):
        import traceback
        traceback.print_exception(exc, val, tb)

    def _normalize_strategy_params(self, raw):
        defaults = DEFAULT_STRATEGY_PARAMS
        out = {}

        int_keys = [
            "gc_fast_ma",
            "gc_slow_ma",
            "pullback_short_ma",
            "pullback_base_ma",
            "long_fast_ma",
            "long_slow_ma",
            "target_pullback_no",
        ]
        for key in int_keys:
            try:
                val = int(raw.get(key, defaults[key]))
            except Exception:
                val = defaults[key]
            if val < 1:
                val = defaults[key]
            out[key] = val

        try:
            step = float(raw.get("step_drop_pct", defaults["step_drop_pct"]))
        except Exception:
            step = defaults["step_drop_pct"]
        out["step_drop_pct"] = step if step >= 0 else defaults["step_drop_pct"]

        bool_keys = [
            "require_long_ma_order",
            "require_close_above_long_ma",
            "require_bearish_entry",
        ]
        for key in bool_keys:
            out[key] = bool(raw.get(key, defaults[key]))

        return out

    def load_strategy_params(self):
        params = DEFAULT_STRATEGY_PARAMS.copy()
        try:
            if os.path.exists(STRATEGY_PARAMS_FILE):
                with open(STRATEGY_PARAMS_FILE, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    params.update(loaded)
        except Exception as e:
            print(f"Strategy config load failed: {e}")
        return self._normalize_strategy_params(params)

    def save_strategy_params(self):
        try:
            os.makedirs(os.path.dirname(STRATEGY_PARAMS_FILE), exist_ok=True)
            with open(STRATEGY_PARAMS_FILE, "w", encoding="utf-8") as f:
                json.dump(self.strategy_params, f, indent=2)
        except Exception as e:
            messagebox.showerror("Save Error", f"Failed to save strategy params:\n{e}")

    def _normalize_broker_profiles(self, raw):
        profiles = {}
        source = raw if isinstance(raw, dict) else {}
        for name, conf in source.items():
            if not isinstance(conf, dict):
                continue
            try:
                b = float(conf.get("buy_fee_rate", DEFAULT_BUY_FEE_RATE))
                s = float(conf.get("sell_fee_rate", DEFAULT_SELL_FEE_RATE))
            except Exception:
                b = DEFAULT_BUY_FEE_RATE
                s = DEFAULT_SELL_FEE_RATE
            if b < 0:
                b = DEFAULT_BUY_FEE_RATE
            if s < 0:
                s = DEFAULT_SELL_FEE_RATE
            profiles[str(name)] = {
                "buy_fee_rate": b,
                "sell_fee_rate": s,
                "use_kr_fee_model": bool(conf.get("use_kr_fee_model", False)),
            }

        # Ensure default profiles always exist.
        for name, conf in DEFAULT_BROKER_PROFILES.items():
            if name not in profiles:
                profiles[name] = dict(conf)
        return profiles

    def load_broker_profiles(self):
        loaded = None
        try:
            if os.path.exists(BROKER_PROFILES_FILE):
                with open(BROKER_PROFILES_FILE, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
        except Exception as e:
            print(f"Broker profile load failed: {e}")
        profiles = self._normalize_broker_profiles(loaded or DEFAULT_BROKER_PROFILES)
        try:
            os.makedirs(os.path.dirname(BROKER_PROFILES_FILE), exist_ok=True)
            with open(BROKER_PROFILES_FILE, "w", encoding="utf-8") as f:
                json.dump(profiles, f, indent=2)
        except Exception as e:
            print(f"Broker profile save failed: {e}")
        return profiles

    def save_broker_profiles(self):
        try:
            os.makedirs(os.path.dirname(BROKER_PROFILES_FILE), exist_ok=True)
            with open(BROKER_PROFILES_FILE, "w", encoding="utf-8") as f:
                json.dump(self.broker_profiles, f, indent=2)
        except Exception as e:
            print(f"Broker profile save failed: {e}")

    def load_broker_profile_selection(self):
        try:
            if os.path.exists(BROKER_PROFILE_STATE_FILE):
                with open(BROKER_PROFILE_STATE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                profile = str(data.get("active_profile", "KakaoPay"))
                if profile in self.broker_profiles:
                    return profile
        except Exception as e:
            print(f"Broker profile state load failed: {e}")
        return "KakaoPay" if "KakaoPay" in self.broker_profiles else "Custom"

    def save_broker_profile_selection(self):
        try:
            os.makedirs(os.path.dirname(BROKER_PROFILE_STATE_FILE), exist_ok=True)
            with open(BROKER_PROFILE_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump({"active_profile": self.active_broker_profile}, f, indent=2)
        except Exception as e:
            print(f"Broker profile state save failed: {e}")

    def _apply_broker_profile_to_inputs(self, profile_name):
        conf = self.broker_profiles.get(profile_name)
        if not conf:
            return
        self.ent_buy_fee.delete(0, tk.END)
        self.ent_buy_fee.insert(0, f"{conf['buy_fee_rate'] * 100.0:.4f}%")
        self.ent_sell_fee.delete(0, tk.END)
        self.ent_sell_fee.insert(0, f"{conf['sell_fee_rate'] * 100.0:.4f}%")

    def on_broker_profile_change(self, event=None):
        name = self.broker_profile_var.get().strip() if hasattr(self, "broker_profile_var") else ""
        if not name:
            return
        self.active_broker_profile = name
        # Always apply selected profile values, including Custom.
        self._apply_broker_profile_to_inputs(name)
        self.save_broker_profile_selection()
        self.on_setting_change()

    def _load_cached_tickers(self):
        try:
            if not os.path.exists(CACHE_FILE):
                return None
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                return None
            cleaned = [str(t).strip().upper() for t in data if str(t).strip()]
            deduped = []
            seen = set()
            for t in cleaned:
                key = self._issuer_group_key(t)
                if key in seen:
                    continue
                seen.add(key)
                deduped.append(t)
            return deduped if len(deduped) >= 20 else None
        except Exception:
            return None

    def _issuer_group_key(self, ticker):
        t = str(ticker).upper().replace("/", "-").replace(".", "-")
        share_class_groups = {
            "GOOG": "GOOGLE",
            "GOOGL": "GOOGLE",
            "FOX": "FOX",
            "FOXA": "FOX",
            "NWS": "NWS",
            "NWSA": "NWS",
            "BRK-A": "BERKSHIRE",
            "BRK-B": "BERKSHIRE",
        }
        return share_class_groups.get(t, t)

    def _normalize_ticker_symbol(self, symbol):
        t = str(symbol).strip().upper()
        # NASDAQ feed may use slash for share class (e.g., BRK/A).
        return t.replace(".", "-").replace("/", "-")

    def _company_issuer_key(self, ticker, company_name=""):
        mapped = self._issuer_group_key(ticker)
        if mapped != str(ticker).upper():
            return mapped

        name = str(company_name or "").strip().upper()
        if not name:
            return str(ticker).upper()

        # Remove common class/share suffixes so GOOG/GOOGL-like variants collapse.
        name = re.sub(r"\s+CLASS\s+[A-Z0-9\-]+\b.*$", "", name)
        name = re.sub(r"\s+COMMON STOCK\b.*$", "", name)
        name = re.sub(r"\s+ORDINARY SHARES\b.*$", "", name)
        name = re.sub(r"\s+SHARES\b.*$", "", name)
        return name.strip() if name.strip() else str(ticker).upper()

    def _fetch_top100_from_nasdaq_screener(self):
        import requests

        url = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&download=true"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://www.nasdaq.com/market-activity/stocks/screener",
            "Origin": "https://www.nasdaq.com",
        }

        response = requests.get(url, headers=headers, timeout=NETWORK_TIMEOUT_SEC)
        response.raise_for_status()
        rows = response.json().get("data", {}).get("rows", [])
        if not rows:
            raise ValueError("NASDAQ screener returned no rows")

        ranked = []
        for row in rows:
            country = str(row.get("country", "")).strip().lower()
            if country != "united states":
                continue

            symbol = self._normalize_ticker_symbol(row.get("symbol", ""))
            if not symbol:
                continue

            try:
                mcap = float(str(row.get("marketCap", "0")).replace(",", ""))
            except Exception:
                mcap = 0.0
            if mcap <= 0:
                continue

            company = str(row.get("name", "")).strip()
            issuer_key = self._company_issuer_key(symbol, company)
            ranked.append((symbol, mcap, issuer_key))

        ranked.sort(key=lambda x: x[1], reverse=True)

        deduped = []
        seen_issuers = set()
        for symbol, _, issuer_key in ranked:
            if issuer_key in seen_issuers:
                continue
            seen_issuers.add(issuer_key)
            deduped.append(symbol)
            if len(deduped) >= 100:
                break

        if len(deduped) < 100:
            raise ValueError(f"NASDAQ source produced only {len(deduped)} symbols")

        return deduped

    def _fetch_top100_from_tradingview(self):
        import requests

        url = "https://scanner.tradingview.com/america/scan"
        payload = {
            "markets": ["america"],
            "symbols": {"query": {"types": []}, "tickers": []},
            "options": {"lang": "en"},
            "columns": ["name", "description", "market_cap_basic", "exchange", "type", "subtype"],
            "sort": {"sortBy": "market_cap_basic", "sortOrder": "desc"},
            "range": [0, 300],
            "filter": [
                {"left": "type", "operation": "equal", "right": "stock"},
                {"left": "exchange", "operation": "in_range", "right": ["NASDAQ", "NYSE", "AMEX"]},
                {"left": "subtype", "operation": "in_range", "right": ["common", "dr"]},
                {"left": "market_cap_basic", "operation": "nempty"},
            ],
        }

        response = requests.post(url, json=payload, timeout=NETWORK_TIMEOUT_SEC)
        response.raise_for_status()
        rows = response.json().get("data", [])
        if len(rows) < 100:
            raise ValueError(f"TradingView returned too few rows: {len(rows)}")

        deduped = []
        seen_issuers = set()
        for row in rows:
            raw_symbol = str(row.get("s", ""))
            ticker = self._normalize_ticker_symbol(raw_symbol.split(":")[-1] if ":" in raw_symbol else raw_symbol)
            if not ticker:
                continue

            dvals = row.get("d", [])
            company = str(dvals[1]).strip() if isinstance(dvals, list) and len(dvals) > 1 and dvals[1] else ""
            issuer_key = self._company_issuer_key(ticker, company)
            if issuer_key in seen_issuers:
                continue

            seen_issuers.add(issuer_key)
            deduped.append(ticker)
            if len(deduped) >= 100:
                break

        if len(deduped) < 100:
            raise ValueError(f"TradingView source produced only {len(deduped)} symbols")

        return deduped

    def _required_ma_periods(self, strategy_params=None):
        periods = {5, 20, 50, 150, 200}
        params = strategy_params or (self.strategy_params if hasattr(self, "strategy_params") else DEFAULT_STRATEGY_PARAMS)
        periods.update([
            int(params.get("gc_fast_ma", 20)),
            int(params.get("gc_slow_ma", 50)),
            int(params.get("pullback_short_ma", 5)),
            int(params.get("pullback_base_ma", 20)),
            int(params.get("long_fast_ma", 150)),
            int(params.get("long_slow_ma", 200)),
        ])
        return sorted([p for p in periods if p >= 1])

    def _strategy_ma_periods(self, strategy_params=None):
        params = strategy_params or (self.strategy_params if hasattr(self, "strategy_params") else DEFAULT_STRATEGY_PARAMS)
        periods = {
            int(params.get("gc_fast_ma", 20)),
            int(params.get("gc_slow_ma", 50)),
            int(params.get("pullback_short_ma", 5)),
            int(params.get("pullback_base_ma", 20)),
            int(params.get("long_fast_ma", 150)),
            int(params.get("long_slow_ma", 200)),
        }
        return sorted([p for p in periods if p >= 1])

    def _setup_ui(self):
        # Main Layout: PanedWindow (Left=List, Right=Chart+Table)
        self.paned = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        self.paned.pack(fill=tk.BOTH, expand=True)

        # --- LEFT PANEL: Controls & List ---
        left_frame = ttk.Frame(self.paned, width=300, padding=10)
        self.paned.add(left_frame, weight=1)

        # Buttons
        btn_frame = ttk.Frame(left_frame)
        btn_frame.pack(fill=tk.X, pady=5)
        self.btn_refresh = ttk.Button(btn_frame, text="Refresh Top100", command=self.refresh_top100)
        self.btn_refresh.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2)
        self.btn_scan = ttk.Button(btn_frame, text="Run Scan", command=self.start_scan)
        self.btn_scan.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2)

        # Status
        self.lbl_status = ttk.Label(left_frame, text="Ready. Click 'Refresh Top100' to load tickers.", foreground="blue", wraplength=280)
        self.lbl_status.pack(pady=5)

        # View Mode Toggle
        mode_frame = ttk.Frame(left_frame)
        mode_frame.pack(fill=tk.X, pady=(5, 0))
        ttk.Label(mode_frame, text="Show:").pack(side=tk.LEFT)
        self.mode_var = tk.StringVar(value="SCAN") # Default to Scan results (or Fallback if empty)
        
        self.rb_scan = ttk.Radiobutton(mode_frame, text="Candidates", variable=self.mode_var, value="SCAN", command=self.update_list_display)
        self.rb_scan.pack(side=tk.LEFT, padx=5)
        
        self.rb_all = ttk.Radiobutton(mode_frame, text="Top 100", variable=self.mode_var, value="ALL", command=self.update_list_display)
        self.rb_all.pack(side=tk.LEFT, padx=5)

        # Search
        ttk.Label(left_frame, text="Search Ticker:").pack(anchor="w", pady=(5, 0))
        self.search_var = tk.StringVar()
        try:
            self.search_var.trace_add("write", self.filter_list)
        except AttributeError:
            self.search_var.trace("w", self.filter_list) # fallback
        self.ent_search = ttk.Entry(left_frame, textvariable=self.search_var)
        self.ent_search.pack(fill=tk.X, pady=(0, 10))

        # Ticker List (Treeview for Columns)
        self.trv_candidates = ttk.Treeview(left_frame, columns=("Rank", "Ticker"), show="headings", height=20)
        self.trv_candidates.heading("Rank", text="Rank")
        self.trv_candidates.heading("Ticker", text="Ticker")
        self.trv_candidates.column("Rank", width=40, anchor="center")
        self.trv_candidates.column("Ticker", width=120, anchor="w")
        
        self.trv_candidates.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.trv_candidates.bind("<<TreeviewSelect>>", self.on_ticker_select)
        
        scroll = ttk.Scrollbar(left_frame, orient=tk.VERTICAL, command=self.trv_candidates.yview)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.trv_candidates.config(yscrollcommand=scroll.set)

        # --- RIGHT PANEL: Chart & Backtest ---
        # Use Vertical PanedWindow for resizing Chart vs Table
        right_pane = ttk.PanedWindow(self.paned, orient=tk.VERTICAL)
        self.paned.add(right_pane, weight=4)

        # Top: Chart Canvas
        self.chart_frame = ttk.Frame(right_pane)
        right_pane.add(self.chart_frame, weight=1) # 50% approx
        
        # Placeholder for canvas
        self.canvas_widget = None

        # Bottom: Backtest Results (Treeview)
        # Wrap table in a frame to hold scrollbar etc.
        self.table_frame = ttk.Frame(right_pane)
        right_pane.add(self.table_frame, weight=1) # 50% approx
        
        # --- DETAIL PANEL (Right-most) ---
        # Add 3rd pane to main paned window
        detail_frame = ttk.Frame(self.paned, width=550) # Start wider (550px) to prevent cut-off
        self.paned.add(detail_frame, weight=0) # weight=0 keeps it fixed logic, but let's see. 
        # Actually user wants it to NOT shrink chart. If we increase window size, this is fine.
        self._setup_detail_panel(detail_frame)

        table_header = ttk.Frame(self.table_frame)
        table_header.pack(fill=tk.X, side=tk.TOP, pady=(5, 0))

        ttk.Label(
            table_header,
            text="TFMR Weekly Strategy Backtest Result (End-to-End)",
            font=("Arial", 10, "bold")
        ).pack(anchor="w", side=tk.LEFT)

        self.btn_strategy_cond = ttk.Button(
            table_header,
            text="Strategy Conditions",
            command=self.open_strategy_params_window
        )
        self.btn_strategy_cond.pack(side=tk.RIGHT, padx=(0, 6))

        # Treeview Scrollbars (Vertical & Horizontal)
        tree_scroll_y = ttk.Scrollbar(self.table_frame, orient=tk.VERTICAL)
        tree_scroll_y.pack(side=tk.RIGHT, fill=tk.Y)
        
        tree_scroll_x = ttk.Scrollbar(self.table_frame, orient=tk.HORIZONTAL)
        tree_scroll_x.pack(side=tk.BOTTOM, fill=tk.X)

        self.cols_map = {
            "Ticker": "Ticker",
            "CycleStart": "CycleStart",
            "Pullback#": "Pullback#",
            "EntryDate": "EntryDate",
            "ExitDate": "ExitDate",
            "Weeks": "Weeks",
            "Rounds": "MaxRounds",
            "Units": "Units",
            "AvgPrice": "AvgPrice",
            "Return%": "Return%",
            "Profit": "Profit"
        }
        self.cols = list(self.cols_map.values())

        self.tree = ttk.Treeview(self.table_frame, columns=self.cols, show="headings", 
                                 yscrollcommand=tree_scroll_y.set, xscrollcommand=tree_scroll_x.set, height=8)
        
        # Optimized Column Widths
        col_widths = {
            "Ticker": 60, "CycleStart": 80, "Pullback#": 60, "EntryDate": 80, "ExitDate": 80,
            "Weeks": 50, "MaxRounds": 70, "Units": 60, "AvgPrice": 70, "Return%": 70, "Profit": 80
        }
        
        for col in self.cols:
            self.tree.heading(col, text=col)
            width = col_widths.get(col, 80)
            self.tree.column(col, width=width, anchor="center")
        
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        tree_scroll_y.config(command=self.tree.yview)
        tree_scroll_x.config(command=self.tree.xview)
        
        # Bind Click
        self.tree.bind("<<TreeviewSelect>>", self.on_backtest_select)

        # Initialize Empty Chart
        self.init_empty_chart()

    def _setup_detail_panel(self, parent):
        # 4 Sections: Settings (Table), Buy Log (TV), Sell Log (TV), Summary (TV)
        
        # 1. Top Section: Setting / Overview (Table Box)
        # Use a Frame with grid of Labels/Entries
        f1 = ttk.LabelFrame(parent, text="Settings & Overview", padding=5)
        f1.pack(fill=tk.X, padx=5, pady=5)
        
        # Headers: Add "1 Unit (1/15)"
        headers = ["Initial Capital", "Max Rounds", "Multiplier", "Buy Fee", "Sell Fee", "1 Unit (1/15)"]
        for i, h in enumerate(headers):
            lbl = ttk.Label(f1, text=h, font=("Arial", 9, "bold"))
            lbl.grid(row=0, column=i, padx=5, sticky="w")
            if i == 5:
                self.lbl_one_unit_header = lbl
            
        # Values (Entries)
        self.ent_init_capital = ttk.Entry(f1, width=12)
        self.ent_init_capital.grid(row=1, column=0, padx=5, pady=2)
        self.ent_init_capital.insert(0, "10,000")
        
        # Bind Recalculate Logic to Enter Key & FocusOut
        # Bind Recalculate Logic to Enter Key & FocusOut
        self.ent_init_capital.bind("<Return>", self.on_setting_change)
        self.ent_init_capital.bind("<FocusOut>", self.on_setting_change)
        
        # Read-only fields -> Now Editable
        self.ent_max_rounds = ttk.Entry(f1, width=8)
        self.ent_max_rounds.grid(row=1, column=1, padx=5, pady=2)
        self.ent_max_rounds.insert(0, "5")
        
        self.ent_multiplier = ttk.Entry(f1, width=8)
        self.ent_multiplier.grid(row=1, column=2, padx=5, pady=2)
        self.ent_multiplier.insert(0, "1")
        
        self.ent_buy_fee = ttk.Entry(f1, width=10)
        self.ent_buy_fee.grid(row=1, column=3, padx=5, pady=2)
        self.ent_buy_fee.insert(0, "0.0700%")
        
        self.ent_sell_fee = ttk.Entry(f1, width=10)
        self.ent_sell_fee.grid(row=1, column=4, padx=5, pady=2)
        self.ent_sell_fee.insert(0, "0.0708%")

        # Bind Changes
        for ent in [self.ent_max_rounds, self.ent_multiplier, self.ent_buy_fee, self.ent_sell_fee]:
            ent.bind("<Return>", self.on_setting_change)
            ent.bind("<FocusOut>", self.on_setting_change)
        
        # New Field: 1 Unit
        self.ent_one_unit = ttk.Entry(f1, width=12, state="readonly")
        self.ent_one_unit.grid(row=1, column=5, padx=5, pady=2)

        ttk.Label(f1, text="Broker Profile", font=("Arial", 9, "bold")).grid(row=2, column=0, padx=5, pady=(4, 0), sticky="w")
        self.broker_profile_var = tk.StringVar(value=self.active_broker_profile)
        self.cmb_broker_profile = ttk.Combobox(
            f1,
            textvariable=self.broker_profile_var,
            values=list(self.broker_profiles.keys()),
            state="readonly",
            width=12
        )
        self.cmb_broker_profile.grid(row=2, column=1, padx=5, pady=(4, 0), sticky="w")
        self.cmb_broker_profile.bind("<<ComboboxSelected>>", self.on_broker_profile_change)

        if self.active_broker_profile in self.broker_profiles:
            self._apply_broker_profile_to_inputs(self.active_broker_profile)
        
        # Initial calculation for 10,000
        self.on_setting_change()
        
        # 2. Middle 1: Buy History
        f2 = ttk.LabelFrame(parent, text="Buy History", padding=5)
        f2.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        cols_buy = ("Round", "Date", "Qty", "Hold", "Price", "Amount", "AvgPrice")
        self.tv_buy = ttk.Treeview(f2, columns=cols_buy, show="headings", height=5)
        for c in cols_buy:
            self.tv_buy.heading(c, text=c)
            w = 50 if c in ["Round", "Qty", "Hold"] else 70
            if c == "Date": w = 80
            self.tv_buy.column(c, width=w, anchor="center")
            
        sb_buy = ttk.Scrollbar(f2, orient=tk.VERTICAL, command=self.tv_buy.yview)
        self.tv_buy.configure(yscrollcommand=sb_buy.set)
        self.tv_buy.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb_buy.pack(side=tk.RIGHT, fill=tk.Y)
        
        # 3. Middle 2: Sell History (Height 1)
        f3 = ttk.LabelFrame(parent, text="Sell History", padding=5)
        f3.pack(fill=tk.X, padx=5, pady=5)
        
        # Reason Removed
        cols_sell = ("Date", "Qty", "Price", "Amount", "Fee", "PnL")
        self.tv_sell = ttk.Treeview(f3, columns=cols_sell, show="headings", height=1) # Reduced Height
        for c in cols_sell:
            self.tv_sell.heading(c, text=c)
            w = 70
            if c == "Date": w = 80
            self.tv_sell.column(c, width=w, anchor="center")
            
        self.tv_sell.pack(fill=tk.X, expand=True)
        
        # 4. Bottom: Summary (Table Box)
        f4 = ttk.LabelFrame(parent, text="Trade Summary", padding=5)
        f4.pack(fill=tk.X, padx=5, pady=5)
        
        cols_sum = ("Total Cost", "Net Proceeds", "Realized PnL", "ROI", "Final Capital")
        self.tv_sum = ttk.Treeview(f4, columns=cols_sum, show="headings", height=1)
        for c in cols_sum:
            self.tv_sum.heading(c, text=c)
            self.tv_sum.column(c, width=105, anchor="center") # Slightly wider columns (105*5 = 525)
            
        self.tv_sum.pack(fill=tk.X, expand=True)
        
    def on_setting_change(self, event=None):
        # 1. Parse Inputs
        try:
            # Capital
            cap_str = self.ent_init_capital.get().replace(",", "")
            init_cap = float(cap_str)
            # Format Capital
            formatted = f"{int(init_cap):,}"
            if self.ent_init_capital.get() != formatted:
                self.ent_init_capital.delete(0, tk.END)
                self.ent_init_capital.insert(0, formatted)
                
            # Max Rounds
            max_rounds = int(self.ent_max_rounds.get())
            
            # Multiplier
            mult_str = self.ent_multiplier.get()
            multiplier = float(mult_str) if mult_str else 1.0
            
            # Fees (Strip %)
            b_fee_str = self.ent_buy_fee.get().replace("%", "")
            buy_fee = float(b_fee_str) / 100.0 if b_fee_str else 0.0
            
            s_fee_str = self.ent_sell_fee.get().replace("%", "")
            sell_fee = float(s_fee_str) / 100.0 if s_fee_str else 0.0
            
        except ValueError:
            return # Invalid input, ignore

        # If user changed values while a fixed profile is selected, switch to Custom.
        selected_profile = self.broker_profile_var.get().strip() if hasattr(self, "broker_profile_var") else ""
        if selected_profile and selected_profile != "Custom":
            conf = self.broker_profiles.get(selected_profile, {})
            p_buy = float(conf.get("buy_fee_rate", -1))
            p_sell = float(conf.get("sell_fee_rate", -1))
            if (
                abs(p_buy - buy_fee) > 1e-12
                or abs(p_sell - sell_fee) > 1e-12
            ):
                self.active_broker_profile = "Custom"
                self.broker_profile_var.set("Custom")
                self.save_broker_profile_selection()
                selected_profile = "Custom"

        if selected_profile == "Custom":
            custom_conf = self.broker_profiles.get("Custom", {})
            custom_conf["buy_fee_rate"] = buy_fee
            custom_conf["sell_fee_rate"] = sell_fee
            # keep existing flag value (UI toggle removed)
            custom_conf["use_kr_fee_model"] = bool(custom_conf.get("use_kr_fee_model", False))
            self.broker_profiles["Custom"] = custom_conf
            self.save_broker_profiles()

        # 2. Update 1 Unit Display
        # Logic: Linear Sum (1+2+..+MaxRounds)
        if max_rounds < 1: max_rounds = 1

        # Update dynamic header: 1 Unit (1/n), where n = 1+2+...+MaxRounds
        if hasattr(self, "lbl_one_unit_header"):
            header_denominator = max_rounds * (max_rounds + 1) // 2
            if header_denominator < 1:
                header_denominator = 1
            self.lbl_one_unit_header.config(text=f"1 Unit (1/{header_denominator})")

        # Total Units = Sum(Round * Multiplier)
        total_units = sum([r * multiplier for r in range(1, max_rounds + 1)])
        one_unit = init_cap / total_units if total_units > 0 else 0
        
        if hasattr(self, 'ent_one_unit'):
            self.ent_one_unit.config(state="normal")
            self.ent_one_unit.delete(0, tk.END)
            self.ent_one_unit.insert(0, f"${int(one_unit):,}")
            self.ent_one_unit.config(state="readonly")

        # 3. Heavy recalculation is debounced and moved to background.
        self._schedule_setting_recalc()

    def _schedule_setting_recalc(self):
        if self._setting_recalc_job:
            self.root.after_cancel(self._setting_recalc_job)
        self._setting_recalc_job = self.root.after(SETTINGS_RECALC_DEBOUNCE_MS, self._start_setting_recalc)

    def _start_setting_recalc(self):
        self._setting_recalc_job = None

        ticker, df = self._get_active_ticker_data()
        if not ticker or df is None or df.empty:
            return

        trade_key = self._get_selected_trade_key()
        init_cap, max_rounds, multiplier, buy_fee, sell_fee, use_kr_fee_model = self._read_sim_inputs()
        params_snapshot = dict(self.strategy_params)
        df_snapshot = df.copy()

        self._setting_recalc_token += 1
        token = self._setting_recalc_token
        self.lbl_status.config(text=f"Recalculating {ticker}...")

        self._start_worker(
            target=self._setting_recalc_worker,
            args=(
                token,
                ticker,
                df_snapshot,
                trade_key,
                init_cap,
                max_rounds,
                multiplier,
                buy_fee,
                sell_fee,
                use_kr_fee_model,
                params_snapshot,
            ),
            name="setting-recalc",
        )

    def _setting_recalc_worker(
        self,
        token,
        ticker,
        df_snapshot,
        trade_key,
        init_cap,
        max_rounds,
        multiplier,
        buy_fee,
        sell_fee,
        use_kr_fee_model,
        params_snapshot,
    ):
        try:
            df_local = self.compute_ma(df_snapshot, params_snapshot)
            logs = self.run_backtest(
                df_local,
                ticker,
                init_capital=init_cap,
                max_rounds=max_rounds,
                buy_fee=buy_fee,
                sell_fee=sell_fee,
                multiplier=multiplier,
                use_kr_fee_model=use_kr_fee_model,
                strategy_params=params_snapshot,
            )
            self._post_ui_event("setting_recalc_result", token, ticker, df_local, trade_key, logs, None)
        except Exception as e:
            self._post_ui_event("setting_recalc_result", token, ticker, None, trade_key, None, e)

    def _apply_setting_recalc_result(self, token, ticker, df_local, trade_key, logs, error):
        if token != self._setting_recalc_token:
            return

        if error is not None:
            self.lbl_status.config(text=f"Recalc error ({ticker}): {error}")
            return

        if df_local is not None:
            self.candidate_data[ticker] = df_local
            if getattr(self, "current_ticker", None) == ticker:
                self.chart_df = df_local

        target_log = self._find_matching_trade_log(logs, trade_key)
        if target_log is None and logs:
            target_log = logs[-1]

        if target_log:
            self._populate_detail_view(target_log)
            self.draw_chart_markers(df_local, target_log)
            self.lbl_status.config(text=f"Recalculated {ticker}")
        else:
            for item in self.tv_buy.get_children():
                self.tv_buy.delete(item)
            for item in self.tv_sell.get_children():
                self.tv_sell.delete(item)
            for item in self.tv_sum.get_children():
                self.tv_sum.delete(item)
            self._clear_chart_markers()
            self.lbl_status.config(text=f"No trades after recalculation ({ticker})")

    def _get_active_ticker_data(self):
        # Prefer the currently drawn chart context.
        ticker = getattr(self, "current_ticker", None)
        df = getattr(self, "chart_df", None)
        if ticker and isinstance(df, pd.DataFrame) and not df.empty:
            return ticker, df

        # Fallback to currently selected ticker in the left list.
        if hasattr(self, "trv_candidates"):
            sel_items = self.trv_candidates.selection()
            if sel_items:
                item = self.trv_candidates.item(sel_items[0])
                vals = item.get("values", [])
                if len(vals) >= 2:
                    ticker = str(vals[1])
                    df = self.candidate_data.get(ticker)
                    if isinstance(df, pd.DataFrame) and not df.empty:
                        return ticker, df
        return None, None

    def _get_selected_trade_key(self):
        if not hasattr(self, "tree"):
            return None
        sel_rows = self.tree.selection()
        if not sel_rows:
            return None
        vals = self.tree.item(sel_rows[0]).get("values", [])
        if len(vals) < 5:
            return None
        return {
            "Ticker": str(vals[0]),
            "CycleStart": str(vals[1]),
            "Pullback#": str(vals[2]),
            "EntryDate": str(vals[3]),
            "ExitDate": str(vals[4]),
        }

    def _find_matching_trade_log(self, logs, trade_key):
        if not logs or not trade_key:
            return None

        # Primary match: same ticker + pullback + entry date.
        for log in logs:
            if (
                str(log.get("Ticker", "")) == trade_key["Ticker"]
                and str(log.get("Pullback#", "")) == trade_key["Pullback#"]
                and str(log.get("EntryDate", "")) == trade_key["EntryDate"]
            ):
                return log

        # Fallback: entry date only.
        for log in logs:
            if str(log.get("EntryDate", "")) == trade_key["EntryDate"]:
                return log

        return None

    def _read_sim_inputs(self):
        # Defaults aligned with existing UI defaults.
        init_cap = 10000.0
        max_rounds = 5
        multiplier = 1.0
        conf = self.broker_profiles.get(self.active_broker_profile, {})
        buy_fee = float(conf.get("buy_fee_rate", DEFAULT_BUY_FEE_RATE))
        sell_fee = float(conf.get("sell_fee_rate", DEFAULT_SELL_FEE_RATE))
        # Keep for backward compatibility in schema/engine. No UI toggle.
        use_kr_fee_model = bool(conf.get("use_kr_fee_model", False))

        try:
            init_cap = float(self.ent_init_capital.get().replace(",", ""))
        except Exception:
            pass
        try:
            max_rounds = int(self.ent_max_rounds.get())
        except Exception:
            pass
        try:
            multiplier = float(self.ent_multiplier.get()) if self.ent_multiplier.get() else 1.0
        except Exception:
            pass
        try:
            buy_fee = float(self.ent_buy_fee.get().replace("%", "")) / 100.0 if self.ent_buy_fee.get() else 0.0
        except Exception:
            pass
        try:
            sell_fee = float(self.ent_sell_fee.get().replace("%", "")) / 100.0 if self.ent_sell_fee.get() else 0.0
        except Exception:
            pass

        if max_rounds < 1:
            max_rounds = 1
        if multiplier <= 0:
            multiplier = 1.0
        if init_cap <= 0:
            init_cap = 10000.0

        return init_cap, max_rounds, multiplier, buy_fee, sell_fee, use_kr_fee_model

    def _refresh_current_backtest_table(self):
        ticker, df = self._get_active_ticker_data()
        if not ticker or df is None or df.empty:
            return
        params_snapshot = dict(self.strategy_params)
        df = self.compute_ma(df.copy(), params_snapshot)
        self.candidate_data[ticker] = df
        if getattr(self, "current_ticker", None) == ticker:
            self.chart_df = df
        self.draw_chart(df, ticker)

        selected_key = self._get_selected_trade_key()
        init_cap, max_rounds, multiplier, buy_fee, sell_fee, use_kr_fee_model = self._read_sim_inputs()

        logs = self.run_backtest(
            df,
            ticker,
            init_capital=init_cap,
            max_rounds=max_rounds,
            buy_fee=buy_fee,
            sell_fee=sell_fee,
            multiplier=multiplier,
            use_kr_fee_model=use_kr_fee_model,
            strategy_params=params_snapshot
        )
        self.update_backtest_table(logs)

        if not logs:
            for item in self.tv_buy.get_children():
                self.tv_buy.delete(item)
            for item in self.tv_sell.get_children():
                self.tv_sell.delete(item)
            for item in self.tv_sum.get_children():
                self.tv_sum.delete(item)
            self._clear_chart_markers()
            return

        target_iid = None
        if selected_key:
            for iid in self.tree.get_children():
                vals = self.tree.item(iid).get("values", [])
                if len(vals) < 5:
                    continue
                if (
                    str(vals[0]) == selected_key["Ticker"]
                    and str(vals[2]) == selected_key["Pullback#"]
                    and str(vals[3]) == selected_key["EntryDate"]
                ):
                    target_iid = iid
                    break

        if target_iid is None:
            items = self.tree.get_children()
            if items:
                target_iid = items[0]

        if target_iid:
            self.tree.selection_set(target_iid)
            self.tree.focus(target_iid)
            self.on_backtest_select(None)

    def open_strategy_params_window(self):
        if hasattr(self, "_strategy_win") and self._strategy_win and self._strategy_win.winfo_exists():
            self._strategy_win.lift()
            self._strategy_win.focus_force()
            return

        win = tk.Toplevel(self.root)
        self._strategy_win = win
        win.title("TFMR Strategy Conditions")
        win.geometry("460x280")
        win.resizable(False, False)
        win.transient(self.root)
        win.grab_set()

        frame = ttk.Frame(win, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frame, text="Search / Backtest Condition Variables", font=("Arial", 10, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 10)
        )

        # Keep MA parameters fixed in this UI (as requested); expose only frequently tuned knobs.
        numeric_fields = [
            ("target_pullback_no", "Target Pullback Max (1~N)"),
            ("step_drop_pct", "Step Drop %"),
        ]

        entry_vars = {}
        row = 1
        for key, label in numeric_fields:
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=4)
            val = self.strategy_params.get(key, DEFAULT_STRATEGY_PARAMS[key])
            var = tk.StringVar(value=str(val))
            entry_vars[key] = var
            ttk.Entry(frame, textvariable=var, width=14).grid(row=row, column=1, sticky="w", pady=4)
            row += 1

        bool_vars = {}
        bool_fields = [
            ("require_bearish_entry", "Require Bearish Entry Candle"),
        ]
        for key, label in bool_fields:
            var = tk.BooleanVar(value=bool(self.strategy_params.get(key, DEFAULT_STRATEGY_PARAMS[key])))
            bool_vars[key] = var
            ttk.Checkbutton(frame, text=label, variable=var).grid(
                row=row, column=0, columnspan=2, sticky="w", pady=3
            )
            row += 1

        ttk.Label(
            frame,
            text="MA-related strategy values are fixed in this dialog.",
            foreground="gray"
        ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(4, 0))
        row += 1

        ttk.Label(
            frame,
            text="Save applies conditions and recalculates current Backtest Result.",
            foreground="gray"
        ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(4, 10))
        row += 1

        btns = ttk.Frame(frame)
        btns.grid(row=row, column=0, columnspan=3, sticky="e")

        def _reset_defaults():
            for k, _ in numeric_fields:
                entry_vars[k].set(str(DEFAULT_STRATEGY_PARAMS[k]))
            for k, _ in bool_fields:
                bool_vars[k].set(bool(DEFAULT_STRATEGY_PARAMS[k]))

        def _save_and_apply():
            try:
                new_params = self.strategy_params.copy()
                for key, _ in numeric_fields:
                    raw = entry_vars[key].get().strip()
                    if key == "step_drop_pct":
                        new_params[key] = float(raw)
                    else:
                        new_params[key] = int(raw)
            except Exception:
                messagebox.showerror("Input Error", "Numeric fields contain invalid values.")
                return

            for key, _ in bool_fields:
                new_params[key] = bool_vars[key].get()

            if new_params["target_pullback_no"] < 1:
                messagebox.showwarning("Validation", "Target Pullback Max (1~N) must be >= 1.")
                return
            if new_params["step_drop_pct"] < 0:
                messagebox.showwarning("Validation", "Step Drop % must be >= 0.")
                return

            self.strategy_params = self._normalize_strategy_params(new_params)
            self.save_strategy_params()
            self.lbl_status.config(text="Strategy conditions saved.")

            try:
                win.destroy()
            except Exception:
                pass

            # Recompute currently loaded ticker/table immediately.
            self._refresh_current_backtest_table()

        ttk.Button(btns, text="Reset Default", command=_reset_defaults).pack(side=tk.LEFT, padx=4)
        ttk.Button(btns, text="Cancel", command=win.destroy).pack(side=tk.LEFT, padx=4)
        ttk.Button(btns, text="Save", command=_save_and_apply).pack(side=tk.LEFT, padx=4)

        # Enter key submits immediately.
        win.bind("<Return>", lambda event: _save_and_apply())
        win.bind("<KP_Enter>", lambda event: _save_and_apply())

        # Trim extra blank space by fitting to content height.
        win.update_idletasks()
        req_w = max(460, frame.winfo_reqwidth() + 24)
        req_h = frame.winfo_reqheight() + 24
        win.geometry(f"{req_w}x{req_h}")

    def init_empty_chart(self):
        # Create an empty chart with grid
        fig, ax = plt.subplots(figsize=(6, 3))
        
        # Style like the main chart
        ax.set_facecolor('white')
        ax.grid(True, linestyle=':', color='#E0E0E0')
        
        # Hide axes ticks for cleanliness
        ax.set_xticks([])
        ax.set_yticks([])
        
        # Add Text
        ax.text(0.5, 0.5, "Select a Ticker to View Chart", 
                transform=ax.transAxes, ha='center', va='center', 
                fontsize=14, color='gray', fontweight='bold')
        
        self.canvas_widget = FigureCanvasTkAgg(fig, master=self.chart_frame)
        self.canvas_widget.draw()
        self.canvas_widget.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        
        # --- Custom Pan/Zoom Logic (Clean startup) ---
        # (Listeners attached when draw_chart is called)

    def refresh_top100(self):
        self.btn_refresh.state(["disabled"])
        self.lbl_status.config(text="Fetching US market-cap Top 100 (NASDAQ source)...")
        self._start_worker(target=self.fetch_top100_thread, name="fetch-top100")

    def fetch_top100_thread(self):
        try:
            sorted_tickers = None
            source_msg = ""
            source_errors = []

            # Primary: NASDAQ screener (more stable for US equity universe).
            try:
                sorted_tickers = self._fetch_top100_from_nasdaq_screener()
                source_msg = "NASDAQ screener"
            except Exception as e:
                source_errors.append(f"NASDAQ failed: {e}")

            # Secondary fallback: TradingView scanner.
            if not sorted_tickers:
                try:
                    sorted_tickers = self._fetch_top100_from_tradingview()
                    source_msg = "TradingView scanner (fallback)"
                except Exception as e:
                    source_errors.append(f"TradingView failed: {e}")

            if not sorted_tickers:
                raise ValueError("; ".join(source_errors) if source_errors else "No source returned data")

            os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(sorted_tickers, f)
            
            self.all_tickers = sorted_tickers
            if self.is_running:
                self._post_ui_event("top100_success", len(sorted_tickers), source_msg)
                
        except Exception as e:
            # Fallback to cache first, then hardcoded list.
            print(f"Fetch failed: {e}")
            cached = self._load_cached_tickers()
            if cached:
                self.all_tickers = cached
                msg = f"Loaded {len(cached)} tickers from cache (live fetch failed)."
            else:
                deduped_fallback = []
                seen = set()
                for t in FALLBACK_TOP100:
                    key = self._issuer_group_key(t)
                    if key in seen:
                        continue
                    seen.add(key)
                    deduped_fallback.append(t)
                self.all_tickers = deduped_fallback
                msg = f"Loaded {len(self.all_tickers)} fallback tickers."
            if self.is_running:
                self._post_ui_event("top100_failure", str(e), msg)

    def start_scan(self):
        if not self.all_tickers:
            messagebox.showwarning("Warning", "Refresh Top100 first.")
            return
        self.btn_scan.state(["disabled"])
        self.candidates = []
        self.candidate_data = {}
        self.update_list_display()
        self._start_worker(target=self.run_scan_thread, name="run-scan")

    def run_scan_thread(self):
        total = len(self.all_tickers)
        found_count = 0
        params_snapshot = dict(self.strategy_params)
        min_required_len = max(self._required_ma_periods(params_snapshot)) + 5
        for i, ticker in enumerate(self.all_tickers):
            if not self.is_running: break

            self._post_ui_event("scan_progress", f"Scanning {i+1}/{total}: {ticker}")
            try:
                df = self.download_weekly(ticker)
                
                if not self.is_running: break
                
                if df is None or len(df) < min_required_len:
                    continue
                
                df = self.compute_ma(df, params_snapshot)
                if self.analyze_setup(df, params_snapshot):
                    self.candidate_data[ticker] = df # Store for chart
                    found_count += 1
                    self._post_ui_event("scan_add_candidate", ticker)
            except Exception as e:
                print(f"Scan error [{ticker}]: {e}")
                continue
        
        if self.is_running:
            self._post_ui_event("scan_complete", found_count)

    def add_candidate(self, ticker):
        self.candidates.append(ticker)
        self.update_list_display()

    def download_weekly(self, ticker):
        last_err = None
        for attempt in range(NETWORK_RETRIES + 1):
            try:
                df = yf.download(
                    ticker,
                    period=MAX_HISTORY,
                    interval=INTERVAL,
                    auto_adjust=False,
                    progress=False,
                    threads=False,
                    timeout=NETWORK_TIMEOUT_SEC,
                    multi_level_index=False
                )
                if not df.empty:
                    return df
            except Exception as e:
                last_err = e
            if attempt < NETWORK_RETRIES:
                time.sleep(0.6 * (attempt + 1))
        if last_err:
            print(f"Download failed [{ticker}]: {last_err}")
        return None

    def compute_ma(self, df, strategy_params=None):
        close = df['Close']
        for period in self._required_ma_periods(strategy_params):
            df[f'MA{period}'] = close.rolling(period).mean()
        return df

    def analyze_setup(self, df, strategy_params=None):
        # Scan Logic: Must simulate history to know Pullback #
        # Return True if currently in Pullback #1

        params = strategy_params or self.strategy_params
        gc_fast_ma = int(params["gc_fast_ma"])
        gc_slow_ma = int(params["gc_slow_ma"])
        pb_short_ma = int(params["pullback_short_ma"])
        pb_base_ma = int(params["pullback_base_ma"])
        long_fast_ma = int(params["long_fast_ma"])
        long_slow_ma = int(params["long_slow_ma"])
        target_pullback_max = int(params["target_pullback_no"])

        require_long_ma_order = bool(params["require_long_ma_order"])
        require_close_above_long = bool(params["require_close_above_long_ma"])
        require_bearish_entry = bool(params["require_bearish_entry"])

        gc_fast_col = f"MA{gc_fast_ma}"
        gc_slow_col = f"MA{gc_slow_ma}"
        pb_short_col = f"MA{pb_short_ma}"
        pb_base_col = f"MA{pb_base_ma}"
        long_fast_col = f"MA{long_fast_ma}"
        long_slow_col = f"MA{long_slow_ma}"

        # State
        cycle_started = False
        in_pullback = False
        ready_for_pullback = False
        pullback_number = 0
        pullback_trade_eligible = False
        
        # Loop
        start_idx = max([gc_fast_ma, gc_slow_ma, pb_short_ma, pb_base_ma, long_fast_ma, long_slow_ma])
        if len(df) <= start_idx:
            return False
        
        # To speed up, we can start loop earlier but valid start_idx is safer
        # We need to reach 'today' with correct state
        
        # Optimization: We only need accurate state for current cycle.
        # But simple loop is fast enough for 25y weekly data (approx 1300 bars)
        
        for i in range(start_idx, len(df)):
            curr = df.iloc[i]
            prev = df.iloc[i-1]
            
            # --- 2. GC Event ---
            if prev[gc_fast_col] <= prev[gc_slow_col] and curr[gc_fast_col] > curr[gc_slow_col]:
                cycle_started = True
                pullback_number = 0
                in_pullback = False
                ready_for_pullback = False
                pullback_trade_eligible = False
            
            # Cycle Break
            if curr[gc_fast_col] < curr[gc_slow_col]:
                cycle_started = False
                in_pullback = False
                ready_for_pullback = False
                pullback_trade_eligible = False
            
            if not cycle_started:
                continue

            # --- 3. Pullback Logic ---
            # Recovery arms the trigger
            if curr['Close'] > curr[pb_base_col]:
                ready_for_pullback = True
            
            # Condition B: Under MAs
            is_under_ma = (curr['Close'] < curr[pb_short_col]) and (curr['Close'] < curr[pb_base_col])
            
            # Start Pullback
            # Pullback numbering is based only on GC cycle (MA20 > MA50) and MA5/MA20 break.
            if is_under_ma and not in_pullback and ready_for_pullback:
                in_pullback = True
                pullback_number += 1
                ready_for_pullback = False
                # Numbering ignores MA150/200, but trade eligibility does not.
                pullback_trade_eligible = True
                if require_long_ma_order:
                    pullback_trade_eligible = pullback_trade_eligible and (curr[long_fast_col] > curr[long_slow_col])
                if require_close_above_long:
                    pullback_trade_eligible = pullback_trade_eligible and (curr['Close'] > curr[long_slow_col])
                
            # End Pullback
            if in_pullback and (curr['Close'] > curr[pb_short_col] or curr['Close'] > curr[pb_base_col]):
                in_pullback = False
                pullback_trade_eligible = False

            # Check if this is the LAST bar (Current setup)
            if i == len(df) - 1:
                # We want: 
                # 1. Cycle Started (implied by loop continuation)
                # 2. In Pullback (Currently under MAs)
                # 3. Pullback Number == 1
                # 4. Bearish Candle (Entry Trigger)
                
                is_bearish = curr['Close'] < curr['Open']

                entry_candle_ok = (not require_bearish_entry) or is_bearish

                if in_pullback and (1 <= pullback_number <= target_pullback_max) and pullback_trade_eligible and entry_candle_ok:
                    return True
                    
        return False

    def filter_list(self, *args):
        query = self.search_var.get().upper()
        mode = self.mode_var.get()
        
        # Determine source list
        if mode == "SCAN":
            source_list = self.candidates
        else:
            source_list = self.all_tickers
            
        # Clear Treeview
        for item in self.trv_candidates.get_children():
            self.trv_candidates.delete(item)
            
        # Populate
        for t in source_list:
            if query in t:
                # Find Rank
                try:
                    rank = self.all_tickers.index(t) + 1
                except ValueError:
                    rank = "-"
                
                self.trv_candidates.insert("", tk.END, values=(rank, t))

    def update_list_display(self):
        self.filter_list()

    def on_ticker_select(self, event):
        # Debounce: Cancel pending job
        if self._sel_job:
            self.root.after_cancel(self._sel_job)
        
        # Schedule slightly delayed processing (250ms) to allow UI update & scrolling
        self._sel_job = self.root.after(250, self._process_ticker_selection)

    def _process_ticker_selection(self):
        sel = self.trv_candidates.selection()
        if not sel: return
        
        # Get item values
        item = self.trv_candidates.item(sel[0])
        val = item['values'] # (Rank, Ticker)
        ticker = str(val[1]) # Ticker is 2nd column
        
        # Load data logic
        if ticker in self.candidate_data:
            # Recompute MA columns for current strategy to avoid stale cached columns.
            df = self.compute_ma(self.candidate_data[ticker].copy(), self.strategy_params)
            self.candidate_data[ticker] = df
        else:
            self.lbl_status.config(text=f"Fetching data for {ticker}...")
            # Ideally async, but sync for now as per previous logic
            df = self.download_weekly(ticker)
            if df is not None:
                df = self.compute_ma(df, self.strategy_params)
                self.candidate_data[ticker] = df
            else:
                self.lbl_status.config(text=f"Error: No data for {ticker}")
                return

        # 1. Update Chart
        self.draw_chart(df, ticker)
        
        # 2. Read simulation settings from UI
        init_cap, max_rounds, multiplier, buy_fee, sell_fee, use_kr_fee_model = self._read_sim_inputs()

        # 3. Run Backtest & Update Table
        logs = self.run_backtest(
            df,
            ticker,
            init_capital=init_cap,
            max_rounds=max_rounds,
            buy_fee=buy_fee,
            sell_fee=sell_fee,
            multiplier=multiplier,
            use_kr_fee_model=use_kr_fee_model,
            strategy_params=self.strategy_params
        )
        self.update_backtest_table(logs)
        self.lbl_status.config(text=f"Loaded {ticker}")

        # Fix Focus: Return focus to List so arrow keys work
        # Schedule it to run AFTER chart events settle (100ms)
        self.root.after(100, lambda: self.trv_candidates.focus_set())

    # --- Charting Logic ---
    def draw_chart(self, df, ticker):
        # Clear old canvas and toolbar
        if self.canvas_widget:
            self.canvas_widget.get_tk_widget().destroy()
            self.canvas_widget = None
        
        # Cleanup old figures to prevent memory leak
        plt.close("all")

        # Use FULL data for plotting so we can scroll back
        # But set initial view to last ~150 candles
        subset = df.copy()
        
        # Custom Style (Investing.com Korean Theme)
        mc = mpf.make_marketcolors(
            up='#D32F2F', down='#1976D2', 
            edge={'up': '#D32F2F', 'down': '#1976D2'},
            wick={'up': '#D32F2F', 'down': '#1976D2'},
            volume={'up': '#FFCDD2', 'down': '#BBDEFB'},
            inherit=True
        )
        
        s = mpf.make_mpf_style(
            marketcolors=mc,
            gridstyle=':', 
            gridcolor='#E0E0E0',
            rc={'axes.labelsize': 10, 'xtick.labelsize': 8, 'ytick.labelsize': 8}
        )
        
        # MAs (dynamic by current strategy)
        ma_periods = self._strategy_ma_periods()
        default_colors = {
            5: "green",
            20: "cyan",
            50: "blue",
            150: "maroon",
            200: "magenta",
        }
        extra_palette = ["#FF8C00", "#6A5ACD", "#2E8B57", "#8B4513", "#DA70D6", "#708090"]
        extra_idx = 0
        ma_max = max(ma_periods) if ma_periods else None

        apds = []
        chart_ma_cols = []
        for p in ma_periods:
            col = f"MA{p}"
            if col not in subset.columns:
                continue
            color = default_colors.get(p)
            if color is None:
                color = extra_palette[extra_idx % len(extra_palette)]
                extra_idx += 1
            width = 2.0 if (ma_max is not None and p == ma_max) else 1.2
            apds.append(mpf.make_addplot(subset[col], color=color, width=width))
            chart_ma_cols.append(col)

        import matplotlib.ticker as mticker
        
        # Plot FULL data
        # xrotation=0: Horizontal labels
        # datetime_format: Default fallback
        result = mpf.plot(subset, type='candle', style=s, addplot=apds, 
                           # title=..., # REMOVED center title
                           volume=False, returnfig=True, figsize=(10, 6),
                           tight_layout=True, 
                           ylabel='Price',
                           xrotation=0, 
                           datetime_format='%Y-%m',
                           warn_too_much_data=10000)
        
        fig = result[0]
        ax = result[1][0] # Main price axis
        self.chart_ax = ax 
        self.chart_df = subset 
        self.current_ticker = ticker # Track current ticker
        self.chart_ma_cols = chart_ma_cols

        # Set Initial Title (Inside, Top-Left) - "Ticker, Weekly"
        # transform=ax.transAxes means (0,0) is bottom-left, (1,1) is top-right
        ax.text(0.02, 0.95, f"{ticker}, Weekly", transform=ax.transAxes, 
                fontsize=12, fontweight='bold', va='top', ha='left')

        # --- Dynamic Date Formatter (Korean Style) ---
        # "2024-01-01" -> "2024-01" -> "2024"
        def my_date_formatter(x, pos):
            try:
                if x < 0 or x >= len(subset): return ""
                idx = int(x)
                date = subset.index[idx]
                
                # Check visible range
                xlim = ax.get_xlim()
                visible_weeks = xlim[1] - xlim[0]
                
                # High Zoom (<= 26 weeks ~ 6 months): Full Date
                if visible_weeks <= 26:
                    return date.strftime('%Y-%m-%d')
                
                # Medium Zoom (<= 104 weeks ~ 2 years): Year-Month (e.g., 23-07)
                elif visible_weeks <= 104:
                    return date.strftime('%y-%m')
                
                # Low Zoom (> 2 years): Year
                else:
                    if date.month == 1:
                        return date.strftime('%Y')
                    return date.strftime('%b') # "Jan", "Feb"
            except:
                return ""

        ax.xaxis.set_major_formatter(mticker.FuncFormatter(my_date_formatter))

        # Set Initial Zoom (Last 150 candles)
        total_len = len(subset)
        if total_len > 150:
            ax.set_xlim(total_len - 150, total_len + 5)
            
            # Initial Y-Scale
            visible_df = subset.iloc[-150:]
            v_high = visible_df['High'].max()
            v_low = visible_df['Low'].min()
            if not pd.isna(v_high) and not pd.isna(v_low):
                padding = (v_high - v_low) * 0.05
                ax.set_ylim(v_low - padding, v_high + padding)
        
        self.canvas_widget = FigureCanvasTkAgg(fig, master=self.chart_frame)
        self.canvas_widget.draw()
        self.canvas_widget.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        # --- Custom Pan/Zoom Logic ---
        self.drag_start_x = None
        self.drag_start_xlim = None
        
        def on_press(event):
            if event.inaxes != ax: return
            if event.button != 1: return # Left click only
            self.drag_start_x = event.x
            self.drag_start_xlim = ax.get_xlim()

        def on_drag(event):
            if self.drag_start_x is None or event.inaxes != ax: return
            if event.button != 1: return
            
            # Calculate Delta in pixels
            dx = event.x - self.drag_start_x
            
            # Current view width in data units
            xlim = self.drag_start_xlim
            x_range = xlim[1] - xlim[0]
            
            # Chart width in pixels
            bbox = ax.get_window_extent()
            width_pixels = bbox.width
            
            # Scale factor: data units per pixel
            scale = x_range / width_pixels
            
            # Delta in data units (Drag Right -> View Left -> Subtract)
            dx_data = dx * scale
            
            new_min = xlim[0] - dx_data
            new_max = xlim[1] - dx_data
            
            # Boundary check
            if new_max > total_len + 5: 
                diff = new_max - (total_len + 5)
                new_max -= diff
                new_min -= diff
            if new_min < -5:
                diff = -5 - new_min
                new_min += diff
                new_max += diff
                
            ax.set_xlim(new_min, new_max)
            
            # Dynamic Y-Axis (Copy from zoom logic)
            start_idx = int(max(0, new_min))
            end_idx = int(min(total_len, new_max))
            
            # Reuse Y-Axis Logic
            visible_df = subset.iloc[start_idx:end_idx]
            
            # Check price and active MAs
            check_cols = ['High', 'Low'] + list(getattr(self, "chart_ma_cols", []))
            check_cols = [c for c in check_cols if c in visible_df.columns]
            
            if not check_cols: return

            v_high = visible_df[check_cols].max().max()
            v_low = visible_df[check_cols].min().min()
            
            if not pd.isna(v_high) and not pd.isna(v_low):
                padding = (v_high - v_low) * 0.1
                ax.set_ylim(v_low - padding, v_high + padding)
                
            self.canvas_widget.draw_idle()

        def on_release(event):
            self.drag_start_x = None
            self.drag_start_xlim = None

        self.canvas_widget.mpl_connect('button_press_event', on_press)
        self.canvas_widget.mpl_connect('motion_notify_event', on_drag)
        self.canvas_widget.mpl_connect('button_release_event', on_release)
        
        # Bind Mouse Wheel for Zoom
        def on_scroll(event):
            if event.inaxes != ax: return
            
            cur_xlim = ax.get_xlim()
            cur_range = cur_xlim[1] - cur_xlim[0]
            xdata = event.xdata
            if xdata is None: return 
            
            base_scale = 0.8 if event.button == 'up' else 1.25
            new_range = cur_range * base_scale
            
            rel_pos = (xdata - cur_xlim[0]) / cur_range
            new_min = xdata - new_range * rel_pos
            new_max = new_min + new_range
            
            # Boundary checks
            if new_max > total_len + 5: new_max = total_len + 5
            if new_min < -5: new_min = -5
            
            ax.set_xlim([new_min, new_max])
            
            # --- Dynamic Y-Axis Scaling ---
            start_idx = int(max(0, new_min))
            end_idx = int(min(total_len, new_max))
            
            if end_idx > start_idx + 1:
                visible_df = subset.iloc[start_idx:end_idx]
                check_cols = ['High', 'Low'] + list(getattr(self, "chart_ma_cols", []))
                check_cols = [c for c in check_cols if c in visible_df.columns]
                if not check_cols:
                    check_cols = ['High', 'Low']
                v_high = visible_df[check_cols].max().max()
                v_low = visible_df[check_cols].min().min()
                if not pd.isna(v_high) and not pd.isna(v_low):
                    padding = (v_high - v_low) * 0.05
                    ax.set_ylim(v_low - padding, v_high + padding)

            self.canvas_widget.draw_idle()

        self.canvas_widget.mpl_connect('scroll_event', on_scroll)
        self.canvas_widget.get_tk_widget().pack(fill=tk.BOTH, expand=True)

    def on_backtest_select(self, event):
        sel = self.tree.selection()
        if not sel: return
        item = self.tree.item(sel[0])
        vals = item['values']
        
        # vals: Ticker, CycleStart, Pullback#, EntryDate, ExitDate...
        # Indices: 0, 1, 2, 3, 4 ...
        entry_date_str = vals[3]
        exit_date_str = vals[4]
        
        if not hasattr(self, 'chart_ax') or not hasattr(self, 'chart_df'): return
        
        # Find index in dataframe
        df = self.chart_df
        # df index is DatetimeIndex. We need to match dates.
        # String 'YYYY-MM-DD' comparison
        
        try:
            # We locate roughly where the date is
            # Ideally we iterate or use searchsorted. 
            # Since index is sorted datetime:
            target_entry = pd.to_datetime(entry_date_str)
            target_exit = pd.to_datetime(exit_date_str)
            
            # Find closest integer locations
            # get_loc might fail if exact date missing, use searchsorted
            idx_start = df.index.searchsorted(target_entry)
            idx_end = df.index.searchsorted(target_exit)
            
            # Add padding (e.g. +/- 10 weeks)
            pad = 20
            view_start = max(0, idx_start - pad)
            view_end = min(len(df), idx_end + pad)
            
            # Update Chart
            self.chart_ax.set_xlim(view_start, view_end)
            
            # Update Y-Axis to include Price AND MAs
            visible_df = df.iloc[view_start:view_end]
            
            # Calculate Min/Max across Price and MAs
            check_cols = ['High', 'Low'] + list(getattr(self, "chart_ma_cols", []))
            check_cols = [c for c in check_cols if c in visible_df.columns]
            if not check_cols:
                check_cols = ['High', 'Low']
            
            v_high = visible_df[check_cols].max().max()
            v_low = visible_df[check_cols].min().min()
            
            if not pd.isna(v_high) and not pd.isna(v_low):
                padding = (v_high - v_low) * 0.1 # Increase padding to 10%
                self.chart_ax.set_ylim(v_low - padding, v_high + padding)
                
            # --- Draw Buy Markers ---
            # Get Trade Log
            trade_key = {
                "Ticker": str(vals[0]),
                "CycleStart": str(vals[1]),
                "Pullback#": str(vals[2]),
                "EntryDate": str(vals[3]),
                "ExitDate": str(vals[4]),
            }

            try:
                trade_log = None
                sim_df = df

                # Recalculate with CURRENT settings so detail values stay in sync with Settings panel.
                ticker = str(vals[0])
                if ticker and ticker == str(getattr(self, "current_ticker", "")):
                    init_cap, max_rounds, multiplier, buy_fee, sell_fee, use_kr_fee_model = self._read_sim_inputs()
                    params_snapshot = dict(self.strategy_params)
                    sim_df = self.compute_ma(df.copy(), params_snapshot)

                    sim_logs = self.run_backtest(
                        sim_df,
                        ticker,
                        init_capital=init_cap,
                        max_rounds=max_rounds,
                        buy_fee=buy_fee,
                        sell_fee=sell_fee,
                        multiplier=multiplier,
                        use_kr_fee_model=use_kr_fee_model,
                        strategy_params=params_snapshot
                    )
                    trade_log = self._find_matching_trade_log(sim_logs, trade_key)

                # Fallback to table snapshot logs if current-settings simulation has no exact match.
                if trade_log is None and hasattr(self, "backtest_logs") and self.backtest_logs:
                    all_items = self.tree.get_children()
                    sel_idx = all_items.index(sel[0])
                    log_idx = len(self.backtest_logs) - 1 - sel_idx
                    trade_log = self.backtest_logs[log_idx]
                
                # POPULATE DETAIL VIEW
                if trade_log:
                    self._populate_detail_view(trade_log)
                    self.draw_chart_markers(sim_df, trade_log)
                             
            except Exception as e:
                print(f"Marker error: {e}")

            self.canvas_widget.draw_idle()
            
        except Exception as e:
            print(f"Nav error: {e}")

    def _clear_chart_markers(self):
        if hasattr(self, "chart_markers"):
            for marker in self.chart_markers:
                try:
                    marker.remove()
                except Exception:
                    pass
        self.chart_markers = []

        if hasattr(self, "chart_annotations"):
            for annotation in self.chart_annotations:
                try:
                    annotation.remove()
                except Exception:
                    pass
        self.chart_annotations = []

    def draw_chart_markers(self, df, trade_log):
        if not hasattr(self, "chart_ax") or df is None or df.empty:
            return

        self._clear_chart_markers()

        ax = self.chart_ax
        xlim = ax.get_xlim()
        start_idx = int(max(0, xlim[0]))
        end_idx = int(min(len(df), xlim[1] + 1))
        visible_df = df.iloc[start_idx:end_idx] if end_idx > start_idx else df

        check_cols = ["High", "Low"] + list(getattr(self, "chart_ma_cols", []))
        check_cols = [c for c in check_cols if c in visible_df.columns]

        if check_cols:
            v_high = visible_df[check_cols].max().max()
            v_low = visible_df[check_cols].min().min()
        else:
            v_high = visible_df["High"].max()
            v_low = visible_df["Low"].min()

        if pd.isna(v_high) or pd.isna(v_low):
            v_high = df["High"].max()
            v_low = df["Low"].min()

        y_range = float(v_high - v_low) if not pd.isna(v_high) and not pd.isna(v_low) else 0.0
        if y_range <= 0:
            y_range = max(float(df["High"].max() - df["Low"].min()), 1.0)
        offset = y_range * 0.05

        buy_details = trade_log.get("BuyDetails")
        if buy_details is None:
            buy_details = trade_log.get("details", {}).get("buys", [])

        for buy in buy_details:
            b_date_raw = buy.get("Date")
            if not b_date_raw:
                continue

            b_date = pd.Timestamp(b_date_raw)
            if b_date not in df.index:
                continue

            x_loc = df.index.get_loc(b_date)
            candle_low = df.loc[b_date, "Low"]
            marker_y = candle_low - offset

            marker, = ax.plot(
                x_loc,
                marker_y,
                marker="^",
                color="#FF00FF",
                markersize=12,
                markeredgecolor="black",
                zorder=10,
            )
            self.chart_markers.append(marker)

            drop_raw = buy.get("Drop", 0.0)
            try:
                drop_val = float(drop_raw)
            except Exception:
                drop_val = 0.0

            drop_txt = "Entry" if abs(drop_val) < 1e-9 else f"{drop_val:.1f}%"
            round_no = buy.get("Round", "?")
            txt = f"#{round_no}\n{drop_txt}"

            ann = ax.annotate(
                txt,
                (x_loc, marker_y),
                xytext=(0, -25),
                textcoords="offset points",
                ha="center",
                fontsize=9,
                color="black",
                fontweight="bold",
            )
            self.chart_annotations.append(ann)

        ex_str = trade_log.get("ExitDate", "")
        if ex_str:
            ex_date = pd.Timestamp(ex_str)
            if ex_date in df.index:
                x_loc = df.index.get_loc(ex_date)
                candle_high = df.loc[ex_date, "High"]
                marker_y = candle_high + offset

                marker, = ax.plot(
                    x_loc,
                    marker_y,
                    marker="v",
                    color="#00BFFF",
                    markersize=12,
                    markeredgecolor="black",
                    zorder=10,
                )
                self.chart_markers.append(marker)

                ret = trade_log.get("Return%", 0.0)
                txt = f"Exit\n{ret}%"
                ann = ax.annotate(
                    txt,
                    (x_loc, marker_y),
                    xytext=(0, 15),
                    textcoords="offset points",
                    ha="center",
                    fontsize=9,
                    color="blue",
                    fontweight="bold",
                )
                self.chart_annotations.append(ann)

        if self.canvas_widget:
            self.canvas_widget.draw_idle()

    def _populate_detail_view(self, trade_log):
        if not hasattr(self, 'tv_buy'): return
        
        # 1. Clear old data
        for item in self.tv_buy.get_children(): self.tv_buy.delete(item)
        for item in self.tv_sell.get_children(): self.tv_sell.delete(item)
        for item in self.tv_sum.get_children(): self.tv_sum.delete(item)
        
        # 2. Pop Settings
        details = trade_log.get('details', {})
        init_cap = details.get('initial_capital', 100000.0)
        summary = details.get('summary', {})
        
        # Instead of label config, populate Entries IF EMPTY
        if not self.ent_init_capital.get():
            self.ent_init_capital.insert(0, f"{int(init_cap):,}")

        # Static fields - DO NOT OVERWRITE if already set (User Control)
        if not self.ent_max_rounds.get():
            self.ent_max_rounds.insert(0, "5")
        
        if not self.ent_multiplier.get():
            self.ent_multiplier.insert(0, "1")
        
        if not self.ent_buy_fee.get():
            conf = self.broker_profiles.get(self.active_broker_profile, {})
            self.ent_buy_fee.insert(0, f"{float(conf.get('buy_fee_rate', DEFAULT_BUY_FEE_RATE)) * 100.0:.4f}%")
        
        if not self.ent_sell_fee.get():
            conf = self.broker_profiles.get(self.active_broker_profile, {})
            self.ent_sell_fee.insert(0, f"{float(conf.get('sell_fee_rate', DEFAULT_SELL_FEE_RATE)) * 100.0:.4f}%")
        
        # 3. Pop Buys
        buys = details.get('buys', [])
        for b in buys:
            vals = (b['Round'], b['Date'], b['Qty'], b['HoldingQty'], 
                    f"${b['Price']:.2f}", f"${b['TotalCost']:.2f}", f"${b['AvgPrice']:.2f}")
            self.tv_buy.insert("", "end", values=vals)
            
        # 4. Pop Sells
        sells = details.get('sells', [])
        for s in sells:
            vals = (s['Date'], s['Qty'], f"${s['Price']:.2f}", f"${s['Amount']:.2f}", 
                    f"${s['Fee']:.2f}", f"${s['PnL']:.2f}")
            self.tv_sell.insert("", "end", values=vals)
            
        # 5. Pop Summary
        cost = summary.get('total_cost', 0.0)
        net = summary.get('net_proceeds', 0.0)
        pnl = summary.get('pnl', 0.0)
        roi = summary.get('roi', 0.0)
        
        # Store for recalc
        self.current_pnl = pnl 
        
        # Read current capital from Entry
        try:
            user_cap = float(self.ent_init_capital.get().replace(",", ""))
        except: 
            user_cap = init_cap
            
        final = user_cap + pnl
        
        vals = (f"${cost:,.2f}", f"${net:,.2f}",
                f"${pnl:,.2f}", f"{roi:.2f}%", f"${final:,.2f}")
        self.tv_sum.insert("", "end", values=vals)


    # --- Backtest Logic (TFMR Weekly) ---
    def run_backtest(
        self,
        df,
        ticker,
        init_capital=10000.0,
        max_rounds=5,
        buy_fee=DEFAULT_BUY_FEE_RATE,
        sell_fee=DEFAULT_SELL_FEE_RATE,
        multiplier=1.0,
        use_kr_fee_model=True,
        strategy_params=None
    ):
        params = strategy_params or self.strategy_params
        gc_fast_ma = int(params["gc_fast_ma"])
        gc_slow_ma = int(params["gc_slow_ma"])
        pb_short_ma = int(params["pullback_short_ma"])
        pb_base_ma = int(params["pullback_base_ma"])
        long_fast_ma = int(params["long_fast_ma"])
        long_slow_ma = int(params["long_slow_ma"])
        target_pullback_max = int(params["target_pullback_no"])

        require_long_ma_order = bool(params["require_long_ma_order"])
        require_close_above_long = bool(params["require_close_above_long_ma"])
        require_bearish_entry = bool(params["require_bearish_entry"])

        gc_fast_col = f"MA{gc_fast_ma}"
        gc_slow_col = f"MA{gc_slow_ma}"
        pb_short_col = f"MA{pb_short_ma}"
        pb_base_col = f"MA{pb_base_ma}"
        long_fast_col = f"MA{long_fast_ma}"
        long_slow_col = f"MA{long_slow_ma}"

        # Parameters (HTS Quality Simulation)
        STEP_DROP_PCT = float(params["step_drop_pct"]) / 100.0
        MAX_ROUNDS = max_rounds
        
        # Capital Allocation: Linear Progression (1*M + 2*M + ... + MAX_ROUNDS*M)
        # Weights: [1*multiplier, 2*multiplier, ...]
        weights = [r * multiplier for r in range(1, MAX_ROUNDS + 1)]
        total_weight = sum(weights)
        
        # Base Unit Cash (if total_weight > 0)
        # Note: If multiplier applied to everything, it cancels out for fixed capital. 
        # But we implement as requested.
        BASE_UNIT_CASH = init_capital / total_weight if total_weight > 0 else 0
        INITIAL_CAPITAL = init_capital # For Reference
        
        # Fees & Tax
        BUY_FEE_RATE = buy_fee
        SELL_FEE_RATE = sell_fee
        TAX_RATE = 0.0

        def money(v):
            # Broker statements are cash-based; keep values at cent precision.
            return round(float(v) + 1e-12, 2)

        def calc_broker_fee(amount, fee_rate):
            base_fee = amount * fee_rate
            if use_kr_fee_model:
                base_fee = max(KR_MIN_BROKER_FEE_USD, base_fee)
            return money(base_fee)

        def calc_sec_fee(exit_dt, sell_amount):
            if not use_kr_fee_model:
                return 0.0
            if exit_dt >= KR_SEC_FEE_ZERO_FROM:
                return 0.0
            sec_fee = max(KR_SEC_FEE_MIN_USD, sell_amount * KR_SEC_FEE_RATE_BEFORE_2025_05_13)
            return money(sec_fee)

        def calc_taf_fee(quantity):
            if not use_kr_fee_model or quantity <= 0:
                return 0.0
            taf_fee = quantity * KR_TAF_FEE_PER_SHARE_USD
            taf_fee = min(KR_TAF_FEE_MAX_USD, max(KR_TAF_FEE_MIN_USD, taf_fee))
            return money(taf_fee)
        
        # State
        trade_log = []
        
        # Store params for UI
        sim_params = {
            "initial_capital": init_capital,
            "max_rounds": max_rounds,
            "buy_fee": buy_fee,
            "sell_fee": sell_fee,
            "use_kr_fee_model": use_kr_fee_model,
            "strategy": dict(params),
        }
        cycle_started = False
        gc_date = None
        
        in_pullback = False
        ready_for_pullback = False # Must be above MA20 first
        pullback_number = 0
        pullback_trade_eligible = False
        
        bearish_candle_idx = 0
        buy_round = 0
        prev_buy_close = 0.0
        
        # Position
        holding_units = 0     # Total qty
        total_cost = 0.0      # Total spent including fees
        avg_price = 0.0       # Breakeven price
        entry_date = None
        rounds_participated = 0
        
        # Detailed Transaction Logs for Current Trade
        current_buys_detail = [] # List of dicts
        current_sells_detail = [] 
        
        # Loop
        start_idx = max([gc_fast_ma, gc_slow_ma, pb_short_ma, pb_base_ma, long_fast_ma, long_slow_ma])
        if len(df) <= start_idx:
            return trade_log

        for i in range(start_idx, len(df)):
            curr = df.iloc[i]
            prev = df.iloc[i-1]
            
            # --- 1. MA Update (Done in df) ---
            
            # --- 2. GC Event ---
            if prev[gc_fast_col] <= prev[gc_slow_col] and curr[gc_fast_col] > curr[gc_slow_col]:
                cycle_started = True
                gc_date = curr.name.date()
                pullback_number = 0
                in_pullback = False
                ready_for_pullback = False # Reset state
                pullback_trade_eligible = False
            
            if curr[gc_fast_col] < curr[gc_slow_col]:
                cycle_started = False
                in_pullback = False
                ready_for_pullback = False
                pullback_trade_eligible = False
            
            if not cycle_started:
                # Still check exit if we hold position
                pass # Logic continues to Exit Check
            
            # --- 3. Pullback Logic Update ---
            # Check for Recovery (Arms the trigger)
            if curr['Close'] > curr[pb_base_col]:
                ready_for_pullback = True
            
            # Condition B: Close < MA5 & MA20
            is_under_ma = (curr['Close'] < curr[pb_short_col]) and (curr['Close'] < curr[pb_base_col])
            
            # Start Pullback (Only if recovered previously)
            if is_under_ma and not in_pullback and ready_for_pullback:
                in_pullback = True
                pullback_number += 1
                bearish_candle_idx = 0
                buy_round = 0
                prev_buy_close = 0.0
                ready_for_pullback = False # Trigger fired, must recover again for next #
                pullback_trade_eligible = True
                if require_long_ma_order:
                    pullback_trade_eligible = pullback_trade_eligible and (curr[long_fast_col] > curr[long_slow_col])
                if require_close_above_long:
                    pullback_trade_eligible = pullback_trade_eligible and (curr['Close'] > curr[long_slow_col])
                
            # End Pullback: Recover MA5 *AND* MA20? Or just "Not under"?
            if in_pullback and (curr['Close'] > curr[pb_short_col] or curr['Close'] > curr[pb_base_col]):
                in_pullback = False
                pullback_trade_eligible = False

            # --- 4. Buyer Logic (Only if in_pullback) ---
            if in_pullback and (1 <= pullback_number <= target_pullback_max) and pullback_trade_eligible:
                entry_candle_ok = (curr['Close'] < curr['Open']) if require_bearish_entry else True
                if entry_candle_ok:
                    bearish_candle_idx += 1
                    
                    # Check Buy Round Trigger
                    should_buy = False
                    if buy_round == 0:
                        should_buy = True
                    elif buy_round < MAX_ROUNDS:
                        if curr['Close'] <= prev_buy_close * (1.0 - STEP_DROP_PCT):
                            should_buy = True
                    
                    if should_buy:
                        next_round = buy_round + 1
                        price = float(curr['Close'])

                        # Per-round allocation (1x, 2x, ...). Keep each buy inside remaining capital.
                        round_weight = weights[next_round - 1]
                        round_budget = BASE_UNIT_CASH * round_weight
                        remaining_cash = max(0.0, INITIAL_CAPITAL - total_cost)
                        order_budget = min(round_budget, remaining_cash)

                        if order_budget <= 0:
                            continue

                        unit_cost_with_fee = price * (1.0 + BUY_FEE_RATE)
                        qty = int(order_budget / unit_cost_with_fee) if unit_cost_with_fee > 0 else 0
                        if qty < 1:
                            continue

                        # Calculate drop from PREVIOUS buy price (if round > 1)
                        drop_val = 0.0
                        if next_round > 1 and prev_buy_close > 0:
                            drop_val = (price - prev_buy_close) / prev_buy_close * 100.0

                        raw_amt = money(price * qty)
                        broker_fee = calc_broker_fee(raw_amt, BUY_FEE_RATE)
                        total_amt = money(raw_amt + broker_fee)

                        # Guard against cent-rounding overflow.
                        while qty > 0 and total_amt > remaining_cash + 1e-9:
                            qty -= 1
                            raw_amt = money(price * qty)
                            broker_fee = calc_broker_fee(raw_amt, BUY_FEE_RATE)
                            total_amt = money(raw_amt + broker_fee)
                        if qty < 1:
                            continue

                        buy_round = next_round
                        prev_buy_close = price
                        
                        holding_units += qty
                        total_cost = money(total_cost + total_amt)
                        avg_price = total_cost / holding_units
                        
                        rounds_participated = max(rounds_participated, buy_round)
                        
                        if entry_date is None:
                            entry_date = curr.name.date()
                            
                        # Record Buy Detail
                        current_buys_detail.append({
                            "Round": buy_round,
                            "Date": curr.name.strftime('%Y-%m-%d'),
                            "Qty": qty,
                            "HoldingQty": holding_units,
                            "Price": price,
                            "Fee": broker_fee,
                            "BrokerFee": broker_fee,
                            "Amount": raw_amt,
                            "TotalCost": total_amt, # Inclusive
                            "CumCost": total_cost,
                            "AvgPrice": avg_price,
                            "Drop": drop_val
                        })

            # --- 5. Exit Logic ---
            if holding_units > 0:
                signal_exit = (curr['Close'] > curr[pb_base_col]) or (curr['Close'] > curr[pb_short_col])
                trend_broken = (curr[gc_fast_col] < curr[gc_slow_col])
                
                if signal_exit or trend_broken:
                    exit_price = float(curr['Close'])
                    exit_date = curr.name.date()
                    
                    # SELL EXECUTION
                    raw_amount = money(exit_price * holding_units)
                    broker_fee = calc_broker_fee(raw_amount, SELL_FEE_RATE)
                    sec_fee = calc_sec_fee(exit_date, raw_amount)
                    taf_fee = calc_taf_fee(holding_units)
                    fee = money(broker_fee + sec_fee + taf_fee)
                    # Annual capital gains tax excluded by design.
                    tax = money(raw_amount * TAX_RATE)
                    net_proceeds = money(raw_amount - fee - tax)
                    
                    profit = money(net_proceeds - total_cost)

                    # Trade Return% for main table: based on invested capital of this trade.
                    if total_cost > 0:
                        trade_ret_pct = (profit / total_cost) * 100.0
                    else:
                        trade_ret_pct = 0.0

                    # ROI for summary: based on initial account capital.
                    if init_capital > 0:
                        roi_pct = (profit / init_capital) * 100.0
                    else:
                        roi_pct = 0.0
                    
                    if trend_broken and not signal_exit:
                        reason = f"Trend Broken (MA{gc_fast_ma}<MA{gc_slow_ma})"
                    elif curr['Close'] > curr[pb_base_col]:
                        reason = f"Signal (Close > MA{pb_base_ma})"
                    else:
                        reason = f"Signal (Close > MA{pb_short_ma})"
                    
                    # Log Sell Detail
                    current_sells_detail.append({
                        "Date": exit_date.strftime('%Y-%m-%d'),
                        "Qty": holding_units,
                        "Price": exit_price,
                        "Amount": raw_amount,
                        "BrokerFee": broker_fee,
                        "SECFee": sec_fee,
                        "TAFFee": taf_fee,
                        "Fee": fee,
                        "Tax": tax,
                        "Net": net_proceeds, # Amount after cost
                        "PnL": profit,
                        "Reason": reason
                    })
                    
                    log_entry = {
                        "Ticker": ticker,
                        "CycleStart": str(gc_date) if gc_date else "Existing",
                        "Pullback#": pullback_number,
                        "EntryDate": str(entry_date if entry_date else exit_date),
                        "ExitDate": str(exit_date),
                        "Weeks": (exit_date - (entry_date if entry_date else exit_date)).days // 7,
                        "Rounds": rounds_participated, # This shows "Actual Rounds Executed"
                        "Units": holding_units,
                        "AvgPrice": round(avg_price, 2),
                        "Return%": round(trade_ret_pct, 2),
                        "Profit": round(profit, 2),
                        # DETAILED INFO for UI
                        "details": {
                            "initial_capital": INITIAL_CAPITAL,
                            "buys": current_buys_detail,
                            "sells": current_sells_detail,
                            "summary": {
                                "total_cost": total_cost,
                                "net_proceeds": net_proceeds,
                                "pnl": profit,
                                "roi": roi_pct,
                                "final_capital": INITIAL_CAPITAL + profit # Virtual (per trade isolation)
                            },
                            "params": sim_params
                        },
                        "BuyDetails": current_buys_detail # For Chart Markers (Backwards compatibility)
                    }
                    trade_log.append(log_entry)
                    
                    # Reset Position
                    holding_units = 0
                    total_cost = 0.0
                    avg_price = 0.0
                    entry_date = None
                    rounds_participated = 0
                    current_buys_detail = []
                    current_sells_detail = []
                    
                    # Force pullback end
                    in_pullback = False
                    pullback_trade_eligible = False

        return trade_log

    def update_backtest_table(self, logs):
        self.backtest_logs = logs # Store for click access
        # Clear table
        for item in self.tree.get_children():
            self.tree.delete(item)
        
        # Insert new rows (reverse order)
        # Insert new rows (reverse order)
        for log in reversed(logs):
            vals = (
                log["Ticker"], log["CycleStart"], log["Pullback#"], 
                log["EntryDate"], log["ExitDate"], log["Weeks"], 
                log["Rounds"], log["Units"], log["AvgPrice"], 
                log["Return%"], log["Profit"]
            )
            self.tree.insert("", "end", values=vals)



if __name__ == "__main__":
    root = tk.Tk()
    app = TFMRScannerApp(root)
    root.mainloop()
