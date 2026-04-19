import argparse
import asyncio
import csv
import hmac
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, deque
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, session, url_for
from market_hours import get_us_market_clock


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
CHARTS_DIR = BASE_DIR / "charts"
LOGS_DIR = DATA_DIR / "logs"
LLM_CALLS_LOG_PATH = LOGS_DIR / "llm_calls.jsonl"
RUNTIME_LOG_PATH = LOGS_DIR / "web_runtime.log"
FINBERT_HISTORY_LOG_PATH = LOGS_DIR / "finbert_history.jsonl"

DATA_DIR.mkdir(parents=True, exist_ok=True)
CHARTS_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.getenv("BUBO_WEB_SECRET", "change-this-secret")

AUTH_ENABLED = str(os.getenv("BUBO_WEB_AUTH_ENABLED", "1")).strip().lower() in {"1", "true", "yes", "on"}
AUTH_USER = os.getenv("BUBO_WEB_USER", "admin")
AUTH_PASSWORD = os.getenv("BUBO_WEB_PASSWORD", "change-me")

_STATE_LOCK = threading.Lock()
_LOGS = deque(maxlen=5000)
_RUN_STATE: dict[str, Any] = {
    "process": None,
    "mode": None,
    "command": None,
    "started_epoch": None,
    "started_at": None,
    "last_exit_code": None,
    "last_finished_at": None,
}

try:
    CONNECTIVITY_CACHE_TTL_S = max(10, int(os.getenv("BUBO_CONNECTIVITY_CACHE_TTL_S", "120")))
except Exception:
    CONNECTIVITY_CACHE_TTL_S = 120

STOCKTWITS_BASE_URL = str(
    os.getenv("BUBO_STOCKTWITS_BASE_URL", os.getenv("STOCKTWITS_BASE_URL", "https://api.stocktwits.com/api/2")) or ""
).strip().rstrip("/")
if not STOCKTWITS_BASE_URL:
    STOCKTWITS_BASE_URL = "https://api.stocktwits.com/api/2"
STOCKTWITS_TEST_SYMBOL = str(
    os.getenv("BUBO_STOCKTWITS_TEST_SYMBOL", os.getenv("STOCKTWITS_TEST_SYMBOL", "AAPL")) or "AAPL"
).strip().upper() or "AAPL"
REDDIT_TEST_SUBREDDIT = str(os.getenv("BUBO_REDDIT_TEST_SUBREDDIT", "stocks") or "stocks").strip() or "stocks"
REDDIT_TEST_QUERY = str(os.getenv("BUBO_REDDIT_TEST_QUERY", STOCKTWITS_TEST_SYMBOL) or STOCKTWITS_TEST_SYMBOL).strip()
if not REDDIT_TEST_QUERY:
    REDDIT_TEST_QUERY = STOCKTWITS_TEST_SYMBOL

_CONNECTIVITY_CACHE_LOCK = threading.Lock()
_CONNECTIVITY_CACHE: dict[str, Any] = {
    "signature": "",
    "timestamp": 0.0,
    "report": None,
}

try:
    SYSTEM_STATUS_CACHE_TTL_S = max(5, int(os.getenv("BUBO_SYSTEM_STATUS_CACHE_TTL_S", "15")))
except Exception:
    SYSTEM_STATUS_CACHE_TTL_S = 15

_SYSTEM_STATUS_CACHE_LOCK = threading.Lock()
_SYSTEM_STATUS_CACHE: dict[str, Any] = {
    "timestamp": 0.0,
    "status": None,
}

try:
    BROKER_SNAPSHOT_CACHE_TTL_S = max(10, int(os.getenv("BUBO_BROKER_SNAPSHOT_CACHE_TTL_S", "60")))
except Exception:
    BROKER_SNAPSHOT_CACHE_TTL_S = 60

_BROKER_CACHE_LOCK = threading.Lock()
_BROKER_CACHE: dict[str, Any] = {
    "signature": "",
    "timestamp": 0.0,
    "report": None,
    "last_ok_signature": "",
    "last_ok_timestamp": 0.0,
    "last_ok_report": None,
}

_PORTFOLIO_CFG_CACHE_LOCK = threading.Lock()
_PORTFOLIO_CFG_CACHE: dict[str, Any] = {
    "cfg": None,
    "updated_at": 0.0,
}

BUDGET_MODE_CUSTOM = "custom"
BUDGET_MODE_050_SHORT = "budget_050_short"
_SUPPORTED_BUDGET_MODES = {BUDGET_MODE_CUSTOM, BUDGET_MODE_050_SHORT}

WEB_TIMEZONE = str(os.getenv("BUBO_WEB_TIMEZONE", "Europe/Paris") or "Europe/Paris").strip() or "Europe/Paris"
try:
    _WEB_TZ = ZoneInfo(WEB_TIMEZONE)
except Exception:
    _WEB_TZ = ZoneInfo("Europe/Paris")


def _now_text() -> str:
    return datetime.now(_WEB_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _append_log(line: str):
    msg = line.rstrip("\n")
    if not msg:
        return
    entry = f"[{_now_text()}] {msg}"
    _LOGS.append(entry)
    try:
        with RUNTIME_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(entry + "\n")
    except Exception:
        # Runtime logging must never break the web process.
        pass


def _is_authenticated() -> bool:
    return bool(session.get("auth_ok"))


def _check_credentials(username: str, password: str) -> bool:
    user_ok = hmac.compare_digest(str(username or ""), AUTH_USER)
    pass_ok = hmac.compare_digest(str(password or ""), AUTH_PASSWORD)
    return user_ok and pass_ok


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _normalize_budget_mode(value: Any) -> str:
    mode = str(value or "").strip().lower()
    if mode in _SUPPORTED_BUDGET_MODES:
        return mode
    return BUDGET_MODE_CUSTOM


def _apply_budget_mode(cfg: dict[str, Any]) -> dict[str, Any]:
    mode = _normalize_budget_mode(cfg.get("budget_mode", BUDGET_MODE_CUSTOM))
    cfg["budget_mode"] = mode

    if mode == BUDGET_MODE_050_SHORT:
        # Preset "Budget 0,50€/jour": plus d'opportunites, LLM Flash, shorts actifs.
        cfg["decision_engine"] = "llm"
        cfg["preselect_top"] = 90
        cfg["max_deep"] = 18
        cfg["watch_interval_min"] = 30
        cfg["us_market_only"] = True
        cfg["allow_short"] = True
        cfg["no_budget_gate"] = False
        cfg["gemini_model_chain"] = "gemini-2.5-flash"
        cfg["gemini_max_output_tokens"] = 900
        cfg["gemini_thinking_budget"] = 0
        cfg["gemini_prompt_max_events"] = 6
        cfg["gemini_prompt_max_headlines"] = 5
        cfg["gemini_prompt_max_posts"] = 3
        cfg["gemini_prompt_max_post_chars"] = 120
    return cfg


def _build_process_env(cfg: dict[str, Any]) -> dict[str, str]:
    env = os.environ.copy()
    env["BUBO_BUDGET_MODE"] = str(cfg.get("budget_mode", BUDGET_MODE_CUSTOM))
    env["BUBO_GEMINI_MODEL_CHAIN"] = str(cfg.get("gemini_model_chain", "gemini-2.5-flash"))
    env["BUBO_GEMINI_MAX_OUTPUT_TOKENS"] = str(_coerce_int(cfg.get("gemini_max_output_tokens"), 700, minimum=256))
    env["BUBO_GEMINI_THINKING_BUDGET"] = str(_coerce_int(cfg.get("gemini_thinking_budget"), 0, minimum=0))
    env["BUBO_GEMINI_PROMPT_MAX_EVENTS"] = str(_coerce_int(cfg.get("gemini_prompt_max_events"), 4, minimum=0))
    env["BUBO_GEMINI_PROMPT_MAX_HEADLINES"] = str(_coerce_int(cfg.get("gemini_prompt_max_headlines"), 3, minimum=0))
    env["BUBO_GEMINI_PROMPT_MAX_POSTS"] = str(_coerce_int(cfg.get("gemini_prompt_max_posts"), 2, minimum=0))
    env["BUBO_GEMINI_PROMPT_MAX_POST_CHARS"] = str(
        _coerce_int(cfg.get("gemini_prompt_max_post_chars"), 80, minimum=20)
    )
    return env


def _coerce_int(value: Any, default: int, minimum: int | None = None) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    if minimum is not None:
        parsed = max(minimum, parsed)
    return parsed


def _coerce_float(value: Any, default: float, minimum: float | None = None) -> float:
    try:
        parsed = float(value)
    except Exception:
        parsed = default
    if minimum is not None:
        parsed = max(minimum, parsed)
    return parsed


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _safe_float_or_none(value: Any) -> float | None:
    try:
        parsed = float(value)
    except Exception:
        return None
    if parsed != parsed:  # NaN
        return None
    return parsed


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _clean_symbol_name(raw_name: Any, symbol: str) -> str:
    name = str(raw_name or "").strip()
    if not name:
        return symbol
    # Avoid duplicated "name == symbol" noise while keeping useful names.
    if name.upper() == symbol.upper():
        return symbol
    return name


def _read_json_file(path: Path) -> dict[str, Any]:
    if not path.exists() or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_csv_rows(path: Path, limit: int = 200) -> list[dict[str, Any]]:
    if not path.exists() or not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if isinstance(row, dict):
                    rows.append({str(k): row.get(k) for k in row.keys()})
    except Exception:
        return []
    if limit > 0:
        rows = rows[-limit:]
    return rows


def _tail_file_lines(path: Path, tail: int) -> list[str]:
    if tail <= 0 or not path.exists() or not path.is_file():
        return []
    out: deque[str] = deque(maxlen=tail)
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = str(raw).rstrip("\n")
                if line:
                    out.append(line)
    except Exception:
        return []
    return list(out)


def _read_jsonl_rows(path: Path, limit: int = 5000) -> list[dict[str, Any]]:
    if not path.exists() or not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return []
    if limit > 0 and len(lines) > limit:
        lines = lines[-limit:]
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except Exception:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _parse_ts(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None


def _current_engine_command() -> list[str]:
    with _STATE_LOCK:
        cmd = _RUN_STATE.get("command")
        if isinstance(cmd, list):
            return [str(x) for x in cmd]
    return []


def _is_finbert_enabled_runtime() -> bool | None:
    cmd = _current_engine_command()
    if not cmd:
        return None
    return "--no-finbert" not in cmd


def _latest_finbert_snapshot() -> dict[str, Any]:
    rows = _read_jsonl_rows(FINBERT_HISTORY_LOG_PATH, limit=300)
    if not rows:
        return {}
    row = rows[-1] if isinstance(rows[-1], dict) else {}
    if not isinstance(row, dict):
        return {}
    return {
        "timestamp": str(row.get("timestamp", "") or "").strip(),
        "ticker": str(row.get("ticker", "") or "").strip().upper(),
        "article_count": _safe_int(row.get("article_count"), 0),
        "sentiment_score": _safe_float_or_none(row.get("sentiment_score")),
        "top_headline": str(row.get("top_headline", "") or "").strip(),
    }


def _collect_gpu_status() -> dict[str, Any]:
    out: dict[str, Any] = {
        "detected": False,
        "cuda_available": False,
        "torch_installed": False,
        "device_count": 0,
        "devices": [],
        "nvidia_smi_ok": False,
        "nvidia_visible_devices": str(os.getenv("NVIDIA_VISIBLE_DEVICES", "") or "").strip(),
        "activity_now": False,
        "message": "GPU non detecte dans le conteneur",
    }

    try:
        import torch  # type: ignore

        out["torch_installed"] = True
        cuda_ok = bool(torch.cuda.is_available())
        out["cuda_available"] = cuda_ok
        if cuda_ok:
            count = int(torch.cuda.device_count() or 0)
            out["device_count"] = count
            devices = []
            for idx in range(count):
                try:
                    props = torch.cuda.get_device_properties(idx)
                    devices.append(
                        {
                            "index": idx,
                            "name": str(getattr(props, "name", f"GPU {idx}") or f"GPU {idx}"),
                            "total_memory_mb": int((float(getattr(props, "total_memory", 0.0)) / 1024.0 / 1024.0)),
                        }
                    )
                except Exception:
                    devices.append({"index": idx, "name": f"GPU {idx}", "total_memory_mb": 0})
            out["devices"] = devices
    except Exception:
        pass

    smi_bin = shutil.which("nvidia-smi")
    if smi_bin:
        try:
            cmd = [
                smi_bin,
                "--query-gpu=name,driver_version,memory.total,memory.used,utilization.gpu",
                "--format=csv,noheader,nounits",
            ]
            proc = subprocess.run(
                cmd,
                cwd=str(BASE_DIR),
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            if int(proc.returncode) == 0:
                out["nvidia_smi_ok"] = True
                parsed_devices: list[dict[str, Any]] = []
                for idx, raw_line in enumerate(str(proc.stdout or "").splitlines()):
                    parts = [p.strip() for p in raw_line.split(",")]
                    if len(parts) < 5:
                        continue
                    try:
                        used_mb = int(float(parts[3]))
                    except Exception:
                        used_mb = 0
                    try:
                        util_pct = int(float(parts[4]))
                    except Exception:
                        util_pct = 0
                    parsed_devices.append(
                        {
                            "index": idx,
                            "name": parts[0],
                            "driver_version": parts[1],
                            "total_memory_mb": _safe_int(parts[2], 0),
                            "used_memory_mb": used_mb,
                            "utilization_gpu_pct": util_pct,
                        }
                    )
                if parsed_devices:
                    out["devices"] = parsed_devices
                    out["device_count"] = len(parsed_devices)
                    out["detected"] = True
                    out["activity_now"] = any(
                        (_safe_int(d.get("utilization_gpu_pct"), 0) > 0)
                        or (_safe_int(d.get("used_memory_mb"), 0) > 512)
                        for d in parsed_devices
                    )
        except Exception:
            pass

    if out["cuda_available"] and _safe_int(out.get("device_count"), 0) > 0:
        out["detected"] = True
    if out["detected"]:
        if out.get("activity_now"):
            out["message"] = "GPU detecte et active maintenant"
        else:
            out["message"] = "GPU detecte (peut etre idle entre deux analyses)"
    return out


def _collect_finbert_status(running: bool) -> dict[str, Any]:
    cfg = get_default_config()
    cfg_enabled = not bool(cfg.get("no_finbert", True))
    runtime_enabled = _is_finbert_enabled_runtime()
    latest = _latest_finbert_snapshot()
    ts = latest.get("timestamp", "")
    parsed_ts = _parse_ts(ts)
    age_s = None
    if parsed_ts is not None:
        try:
            age_s = max(0, int((datetime.now(parsed_ts.tzinfo) - parsed_ts).total_seconds()))
        except Exception:
            age_s = None

    enabled_effective = bool(runtime_enabled) if runtime_enabled is not None else bool(cfg_enabled)
    has_recent_activity = bool(age_s is not None and age_s <= 7200)
    if not enabled_effective:
        state = "disabled"
        message = "FinBERT desactive"
    elif has_recent_activity:
        state = "active"
        message = "FinBERT actif"
    elif running:
        state = "idle"
        message = "FinBERT actif mais en attente de cycle"
    else:
        state = "ready"
        message = "FinBERT pret (moteur arrete)"

    return {
        "state": state,
        "message": message,
        "enabled_config": bool(cfg_enabled),
        "enabled_runtime": runtime_enabled,
        "has_recent_activity": has_recent_activity,
        "last_event_at": str(ts or ""),
        "last_event_age_s": age_s,
        "last_ticker": str(latest.get("ticker", "") or ""),
        "last_article_count": _safe_int(latest.get("article_count"), 0),
        "last_sentiment_score": _safe_float_or_none(latest.get("sentiment_score")),
        "history_file": "data/logs/finbert_history.jsonl",
    }


def _get_system_status(running: bool, force: bool = False) -> dict[str, Any]:
    now = time.time()
    with _SYSTEM_STATUS_CACHE_LOCK:
        cached = _SYSTEM_STATUS_CACHE.get("status")
        cached_ts = float(_SYSTEM_STATUS_CACHE.get("timestamp") or 0.0)
        if (
            (not force)
            and isinstance(cached, dict)
            and (now - cached_ts) < float(SYSTEM_STATUS_CACHE_TTL_S)
        ):
            return cached

    status = {
        "generated_at": _now_text(),
        "gpu": _collect_gpu_status(),
        "finbert": _collect_finbert_status(running=running),
    }
    with _SYSTEM_STATUS_CACHE_LOCK:
        _SYSTEM_STATUS_CACHE["timestamp"] = now
        _SYSTEM_STATUS_CACHE["status"] = status
    return status


def _paper_report_paths_from_state(state_path: Path) -> dict[str, Path]:
    out_dir = state_path.parent if state_path.parent else Path(".")
    return {
        "trades_csv": out_dir / "paper_trades_latest.csv",
        "equity_csv": out_dir / "paper_equity_curve_latest.csv",
        "daily_csv": out_dir / "paper_daily_stats_latest.csv",
    }


def _build_bubo_transaction_history(state: dict[str, Any], positions: list[dict[str, Any]], limit: int = 300) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    trades = state.get("trades", [])
    if isinstance(trades, list):
        for trade in trades:
            if not isinstance(trade, dict):
                continue
            ticker = str(trade.get("ticker", "") or "").strip().upper()
            if not ticker:
                continue
            display_name = str(trade.get("name", "") or "").strip() or ticker

            shares = _safe_int(trade.get("shares"), 0)
            qty = abs(shares)
            is_short = shares < 0
            entry_side = "SHORT SELL" if is_short else "BUY"
            exit_side = "BUY_TO_COVER" if is_short else "SELL"
            entry_reason = "signal_short" if is_short else "signal_buy"
            entry_price = _safe_float_or_none(trade.get("entry_price"))
            exit_price = _safe_float_or_none(trade.get("exit_price"))
            entry_fee = _safe_float_or_none(trade.get("entry_fee"))
            exit_fee = _safe_float_or_none(trade.get("exit_fee"))
            realized = _safe_float_or_none(trade.get("pnl"))
            entry_signal = trade.get("entry_signal") if isinstance(trade.get("entry_signal"), dict) else {}
            exit_signal = trade.get("exit_signal") if isinstance(trade.get("exit_signal"), dict) else {}

            entry_ts = str(trade.get("entry_ts", "") or trade.get("entry_date", "") or "").strip()
            if entry_ts:
                events.append(
                    {
                        "source": "bubo",
                        "timestamp": entry_ts,
                        "name": display_name,
                        "ticker": ticker,
                        "side": entry_side,
                        "quantity": int(qty),
                        "price": entry_price,
                        "commission": entry_fee,
                        "reason": entry_reason,
                        "realized_pnl": None,
                        "llm_snapshot": entry_signal,
                        "event": "entry",
                    }
                )

            exit_ts = str(trade.get("exit_ts", "") or trade.get("exit_date", "") or "").strip()
            if exit_ts:
                events.append(
                    {
                        "source": "bubo",
                        "timestamp": exit_ts,
                        "name": display_name,
                        "ticker": ticker,
                        "side": exit_side,
                        "quantity": int(qty),
                        "price": exit_price,
                        "commission": exit_fee,
                        "reason": str(trade.get("exit_reason", "") or ""),
                        "realized_pnl": realized,
                        "llm_snapshot": exit_signal,
                        "event": "exit",
                    }
                )

    # Ensure open positions are visible in history even when not closed yet.
    for pos in positions:
        ticker = str(pos.get("ticker", "") or "").strip().upper()
        if not ticker:
            continue
        shares = _safe_int(pos.get("shares"), 0)
        if shares == 0:
            continue
        is_short = shares < 0
        events.append(
            {
                "source": "bubo",
                "timestamp": str(pos.get("entry_ts", "") or pos.get("entry_date", "") or ""),
                "name": str(pos.get("name", "") or "").strip() or ticker,
                "ticker": ticker,
                "side": "SHORT SELL" if is_short else "BUY",
                "quantity": abs(shares),
                "price": _safe_float_or_none(pos.get("entry_price")),
                "commission": _safe_float_or_none(pos.get("entry_fee")),
                "reason": "position_open",
                "realized_pnl": None,
                "llm_snapshot": (pos.get("entry_signal") if isinstance(pos.get("entry_signal"), dict) else {}),
                "event": "open",
            }
        )

    events.sort(key=lambda row: str(row.get("timestamp", "")), reverse=True)
    if limit > 0:
        events = events[:limit]
    return events


def _build_paper_snapshot(cfg: dict[str, Any]) -> dict[str, Any]:
    state_path = Path(str(cfg.get("paper_state") or "data/paper_portfolio_state.json"))
    state = _read_json_file(state_path)
    broker = str(state.get("paper_broker", cfg.get("paper_broker", "local")) or "local")
    positions_raw = state.get("positions", {})
    positions: list[dict[str, Any]] = []
    if isinstance(positions_raw, dict):
        for ticker, pos in positions_raw.items():
            if not isinstance(pos, dict):
                continue
            positions.append(
                {
                    "ticker": str(ticker),
                    "name": str(pos.get("name", "") or "").strip() or str(ticker),
                    "shares": _safe_int(pos.get("shares"), 0),
                    "entry_price": _safe_float(pos.get("entry_price"), 0.0),
                    "last_price": _safe_float(pos.get("last_price"), 0.0),
                    "market_value": _safe_float(pos.get("market_value"), 0.0),
                    "unrealized_pnl": _safe_float(pos.get("unrealized_pnl"), 0.0),
                    "entry_fee": _safe_float(pos.get("entry_fee"), 0.0),
                    "entry_date": str(pos.get("entry_date", "")),
                    "entry_ts": str(pos.get("entry_ts", "") or ""),
                    "entry_signal": pos.get("entry_signal") if isinstance(pos.get("entry_signal"), dict) else {},
                }
            )
    positions.sort(key=lambda r: abs(_safe_float(r.get("market_value"), 0.0)), reverse=True)

    trades = state.get("trades", [])
    if isinstance(trades, list):
        closed_trades = [t for t in trades if isinstance(t, dict)][-100:]
    else:
        closed_trades = []
    closed_trades.reverse()

    actions = state.get("action_log", [])
    if isinstance(actions, list):
        recent_actions = [a for a in actions if isinstance(a, dict)][-100:]
    else:
        recent_actions = []
    recent_actions.reverse()

    report_paths = _paper_report_paths_from_state(state_path)
    trades_csv_rows = _read_csv_rows(report_paths["trades_csv"], limit=200)
    trades_csv_rows.reverse()

    daily_csv_rows = _read_csv_rows(report_paths["daily_csv"], limit=30)
    latest_daily = daily_csv_rows[-1] if daily_csv_rows else {}
    open_unrealized = sum(_safe_float(p.get("unrealized_pnl"), 0.0) for p in positions)
    realized = _safe_float(state.get("realized_pnl"), 0.0)
    total_pnl = realized + open_unrealized
    transactions = _build_bubo_transaction_history(state, positions, limit=300)

    return {
        "ok": True,
        "state_path": str(state_path),
        "broker": broker,
        "cash": _safe_float(state.get("cash"), 0.0),
        "equity": _safe_float(state.get("equity"), 0.0),
        "realized_pnl": realized,
        "open_unrealized_pnl": open_unrealized,
        "total_pnl": total_pnl,
        "positions_count": len(positions),
        "closed_trades_count": len([t for t in closed_trades if t.get("exit_date")]),
        "cycles": _safe_int(state.get("cycles"), 0),
        "updated_at": str(state.get("updated_at", "")),
        "positions": positions,
        "closed_trades": closed_trades,
        "closed_trades_csv": trades_csv_rows,
        "recent_actions": recent_actions,
        "transactions": transactions,
        "transactions_count": len(transactions),
        "daily_latest": latest_daily,
        "files": {k: str(v) for k, v in report_paths.items()},
    }


def _ensure_asyncio_event_loop():
    # ib_insync requires an asyncio loop bound to the current thread.
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


def _ibkr_snapshot_signature(cfg: dict[str, Any]) -> str:
    payload = {
        "paper_enabled": bool(cfg.get("paper_enabled")),
        "paper_broker": str(cfg.get("paper_broker", "")),
        "ibkr_host": str(cfg.get("ibkr_host", "")),
        "ibkr_port": int(cfg.get("ibkr_port", 0) or 0),
        "ibkr_client_id": int(cfg.get("ibkr_client_id", 0) or 0),
        "ibkr_account": str(cfg.get("ibkr_account", "")),
    }
    return json.dumps(payload, sort_keys=True)


def _ibkr_cfg_is_complete(cfg: dict[str, Any]) -> bool:
    host = str(cfg.get("ibkr_host") or "").strip()
    port = _coerce_int(cfg.get("ibkr_port"), 0, minimum=0)
    return bool(host) and int(port) > 0


def _cache_last_portfolio_cfg(cfg: dict[str, Any]):
    if not isinstance(cfg, dict):
        return
    payload = {
        "paper_enabled": bool(cfg.get("paper_enabled")),
        "paper_broker": str(cfg.get("paper_broker", "")),
        "ibkr_host": str(cfg.get("ibkr_host", "")),
        "ibkr_port": _coerce_int(cfg.get("ibkr_port"), 0, minimum=0),
        "ibkr_client_id": _coerce_int(cfg.get("ibkr_client_id"), 42, minimum=1),
        "ibkr_account": str(cfg.get("ibkr_account", "")),
        "ibkr_exchange": str(cfg.get("ibkr_exchange", "")),
        "ibkr_currency": str(cfg.get("ibkr_currency", "")),
        "ibkr_capital_limit": _coerce_float(cfg.get("ibkr_capital_limit"), 10000.0, minimum=1.0),
        "ibkr_existing_positions_policy": str(cfg.get("ibkr_existing_positions_policy", "include")),
    }
    with _PORTFOLIO_CFG_CACHE_LOCK:
        _PORTFOLIO_CFG_CACHE["cfg"] = payload
        _PORTFOLIO_CFG_CACHE["updated_at"] = time.time()


def _get_cached_portfolio_cfg() -> dict[str, Any] | None:
    with _PORTFOLIO_CFG_CACHE_LOCK:
        cached = _PORTFOLIO_CFG_CACHE.get("cfg")
        if isinstance(cached, dict):
            return dict(cached)
    return None


def _fetch_ibkr_snapshot_uncached(cfg: dict[str, Any]) -> dict[str, Any]:
    enabled = bool(cfg.get("paper_enabled")) and str(cfg.get("paper_broker")) == "ibkr"
    if not enabled:
        return {"enabled": False, "ok": False, "message": "Broker paper local (IBKR non actif)"}

    host = str(cfg.get("ibkr_host") or "").strip()
    port = _coerce_int(cfg.get("ibkr_port"), 0, minimum=0)
    client_id = _coerce_int(cfg.get("ibkr_client_id"), 42, minimum=1)
    account_hint = str(cfg.get("ibkr_account") or "").strip()
    if not host or port <= 0:
        return {"enabled": True, "ok": False, "message": "Host/port IBKR invalides"}

    try:
        from ib_insync import IB  # type: ignore
    except Exception as e:
        return {"enabled": True, "ok": False, "message": f"ib_insync indisponible: {e}"}

    _ensure_asyncio_event_loop()
    ib = IB()
    started = time.perf_counter()
    try:
        connect_kwargs: dict[str, Any] = {
            "host": host,
            "port": int(port),
            "clientId": int(max(1, client_id + 2000)),
            "timeout": 8,
            "readonly": True,
        }
        if account_hint:
            connect_kwargs["account"] = account_hint
        try:
            ib.connect(**connect_kwargs)
        except TypeError:
            connect_kwargs.pop("readonly", None)
            connect_kwargs.pop("account", None)
            ib.connect(**connect_kwargs)

        if not ib.isConnected():
            return {"enabled": True, "ok": False, "message": "Connexion IBKR echouee"}

        latency_ms = int((time.perf_counter() - started) * 1000)
        managed_accounts: list[str] = []
        try:
            managed_accounts = [str(a) for a in (ib.managedAccounts() or []) if str(a).strip()]
        except Exception:
            managed_accounts = []
        account = account_hint or (managed_accounts[0] if managed_accounts else "")

        summary_tags = {
            "NetLiquidation",
            "TotalCashValue",
            "BuyingPower",
            "AvailableFunds",
            "ExcessLiquidity",
            "GrossPositionValue",
            "UnrealizedPnL",
            "RealizedPnL",
        }
        account_summary: dict[str, Any] = {}
        currency = ""
        try:
            summary_rows = ib.accountSummary(account=account) if account else ib.accountSummary()
        except TypeError:
            summary_rows = ib.accountSummary()
        for row in summary_rows or []:
            row_account = str(getattr(row, "account", "") or "")
            if account and row_account and row_account != account:
                continue
            tag = str(getattr(row, "tag", "") or "")
            if tag not in summary_tags:
                continue
            raw_val = getattr(row, "value", "")
            parsed = _safe_float_or_none(raw_val)
            account_summary[tag] = parsed if parsed is not None else str(raw_val)
            if not currency:
                currency = str(getattr(row, "currency", "") or "")

        positions = []
        contracts_for_details: dict[int, Any] = {}
        names_by_con_id: dict[int, str] = {}
        names_by_symbol: dict[str, str] = {}
        contracts_to_quote: list[Any] = []
        quoted_contract_ids: set[int] = set()
        try:
            portfolio_rows = ib.portfolio(account=account) if account else ib.portfolio()
        except TypeError:
            portfolio_rows = ib.portfolio()
        except Exception:
            portfolio_rows = []
        if portfolio_rows:
            for p in portfolio_rows or []:
                row_account = str(getattr(p, "account", "") or "")
                if account and row_account and row_account != account:
                    continue
                contract = getattr(p, "contract", None)
                symbol = str(getattr(contract, "symbol", "") or "")
                if not symbol:
                    continue
                qty = _safe_float(getattr(p, "position", 0.0), 0.0)
                if abs(qty) < 1e-12:
                    continue
                con_id = _safe_int(getattr(contract, "conId", 0) if contract is not None else 0, 0)
                if contract is not None and con_id > 0 and con_id not in contracts_for_details:
                    contracts_for_details[con_id] = contract
                market_price = _safe_float_or_none(getattr(p, "marketPrice", None))
                if (
                    contract is not None
                    and con_id > 0
                    and (market_price is None or market_price <= 0.0)
                    and con_id not in quoted_contract_ids
                ):
                    quoted_contract_ids.add(con_id)
                    contracts_to_quote.append(contract)
                positions.append(
                    {
                        "account": row_account,
                        "symbol": symbol,
                        "name": _clean_symbol_name(getattr(contract, "description", "") if contract is not None else "", symbol),
                        "exchange": str(getattr(contract, "exchange", "") or ""),
                        "currency": str(getattr(contract, "currency", "") or ""),
                        "quantity": qty,
                        "avg_cost": _safe_float(getattr(p, "averageCost", 0.0), 0.0),
                        "market_price": market_price,
                        "market_value": _safe_float_or_none(getattr(p, "marketValue", None)),
                        "unrealized_pnl": _safe_float_or_none(getattr(p, "unrealizedPNL", None)),
                        "realized_pnl": _safe_float_or_none(getattr(p, "realizedPNL", None)),
                        "_con_id": con_id,
                    }
                )
        else:
            try:
                pos_rows = ib.positions(account=account) if account else ib.positions()
            except TypeError:
                pos_rows = ib.positions()
            for p in pos_rows or []:
                row_account = str(getattr(p, "account", "") or "")
                if account and row_account and row_account != account:
                    continue
                contract = getattr(p, "contract", None)
                symbol = str(getattr(contract, "symbol", "") or "")
                if not symbol:
                    continue
                qty = _safe_float(getattr(p, "position", 0.0), 0.0)
                if abs(qty) < 1e-12:
                    continue
                con_id = _safe_int(getattr(contract, "conId", 0) if contract is not None else 0, 0)
                if contract is not None and con_id > 0 and con_id not in contracts_for_details:
                    contracts_for_details[con_id] = contract
                market_price = _safe_float_or_none(getattr(p, "marketPrice", None))
                if (
                    contract is not None
                    and con_id > 0
                    and (market_price is None or market_price <= 0.0)
                    and con_id not in quoted_contract_ids
                ):
                    quoted_contract_ids.add(con_id)
                    contracts_to_quote.append(contract)
                positions.append(
                    {
                        "account": row_account,
                        "symbol": symbol,
                        "name": _clean_symbol_name(getattr(contract, "description", "") if contract is not None else "", symbol),
                        "exchange": str(getattr(contract, "exchange", "") or ""),
                        "currency": str(getattr(contract, "currency", "") or ""),
                        "quantity": qty,
                        "avg_cost": _safe_float(getattr(p, "avgCost", 0.0), 0.0),
                        "market_price": market_price,
                        "market_value": _safe_float_or_none(getattr(p, "marketValue", None)),
                        "unrealized_pnl": _safe_float_or_none(getattr(p, "unrealizedPNL", None)),
                        "realized_pnl": _safe_float_or_none(getattr(p, "realizedPNL", None)),
                        "_con_id": con_id,
                    }
                )

        if contracts_for_details:
            for con_id, contract in list(contracts_for_details.items())[:80]:
                try:
                    details = ib.reqContractDetails(contract) or []
                except Exception:
                    details = []
                if not details:
                    continue
                detail = details[0]
                long_name = str(
                    getattr(detail, "longName", "")
                    or getattr(detail, "marketName", "")
                    or ""
                ).strip()
                if long_name:
                    names_by_con_id[con_id] = long_name

        for pos in positions:
            con_id = _safe_int(pos.get("_con_id"), 0)
            symbol = str(pos.get("symbol", "") or "").strip().upper()
            name = ""
            if con_id > 0:
                name = str(names_by_con_id.get(con_id, "") or "").strip()
            if not name:
                name = str(pos.get("name", "") or "").strip()
            name = _clean_symbol_name(name, symbol or "N/A")
            pos["name"] = name
            if symbol:
                names_by_symbol[symbol] = name

        if contracts_to_quote:
            try:
                quote_rows = ib.reqTickers(*contracts_to_quote) or []
            except Exception:
                quote_rows = []
            quote_by_con_id: dict[int, float] = {}
            for quote in quote_rows:
                contract = getattr(quote, "contract", None)
                con_id = _safe_int(getattr(contract, "conId", 0) if contract is not None else 0, 0)
                if con_id <= 0:
                    continue
                price = None
                try:
                    market_price_fn = getattr(quote, "marketPrice", None)
                    if callable(market_price_fn):
                        price = _safe_float_or_none(market_price_fn())
                except Exception:
                    price = None
                if price is None or price <= 0.0:
                    for attr in ("last", "close", "midpoint"):
                        cand = _safe_float_or_none(getattr(quote, attr, None))
                        if cand is not None and cand > 0.0:
                            price = cand
                            break
                if price is not None and price > 0.0:
                    quote_by_con_id[con_id] = price

            if quote_by_con_id:
                for pos in positions:
                    con_id = _safe_int(pos.get("_con_id"), 0)
                    if con_id <= 0:
                        continue
                    market_price = _safe_float_or_none(pos.get("market_price"))
                    if market_price is None or market_price <= 0.0:
                        quote_px = quote_by_con_id.get(con_id)
                        if quote_px is not None and quote_px > 0.0:
                            pos["market_price"] = quote_px

        for pos in positions:
            qty = _safe_float(pos.get("quantity"), 0.0)
            if abs(qty) < 1e-12:
                continue
            avg_cost = _safe_float_or_none(pos.get("avg_cost"))
            market_price = _safe_float_or_none(pos.get("market_price"))
            market_value = _safe_float_or_none(pos.get("market_value"))
            if market_value is None or abs(market_value) < 1e-9:
                if market_price is not None and market_price > 0.0:
                    market_value = market_price * qty
                elif avg_cost is not None and avg_cost > 0.0:
                    market_value = avg_cost * qty
                pos["market_value"] = market_value
            unrealized_pnl = _safe_float_or_none(pos.get("unrealized_pnl"))
            if unrealized_pnl is None and market_price is not None and avg_cost is not None and market_price > 0.0:
                pos["unrealized_pnl"] = (market_price - avg_cost) * qty

        for pos in positions:
            pos.pop("_con_id", None)
        positions.sort(key=lambda r: abs(_safe_float(r.get("market_value"), 0.0)), reverse=True)

        executions = []
        total_commission = 0.0
        try:
            fills = ib.reqExecutions() or []
        except Exception:
            fills = []
        for fill in fills[-250:]:
            contract = getattr(fill, "contract", None)
            execution = getattr(fill, "execution", None)
            report = getattr(fill, "commissionReport", None)
            symbol = str(getattr(contract, "symbol", "") or "")
            if not symbol:
                continue
            con_id = _safe_int(getattr(contract, "conId", 0) if contract is not None else 0, 0)
            if con_id > 0 and contract is not None and not names_by_con_id.get(con_id):
                try:
                    details = ib.reqContractDetails(contract) or []
                except Exception:
                    details = []
                if details:
                    detail = details[0]
                    long_name = str(
                        getattr(detail, "longName", "")
                        or getattr(detail, "marketName", "")
                        or ""
                    ).strip()
                    if long_name:
                        names_by_con_id[con_id] = long_name
            exec_account = str(getattr(execution, "acctNumber", "") or "")
            if account and exec_account and exec_account != account:
                continue
            exec_time = getattr(execution, "time", None)
            if hasattr(exec_time, "isoformat"):
                ts = exec_time.isoformat()
            else:
                ts = str(exec_time or "")
            commission = _safe_float(getattr(report, "commission", 0.0) if report is not None else 0.0, 0.0)
            total_commission += commission
            executions.append(
                {
                    "time": ts,
                    "account": exec_account,
                    "symbol": symbol,
                    "name": _clean_symbol_name(
                        names_by_con_id.get(con_id, "")
                        or names_by_symbol.get(symbol.upper(), "")
                        or (getattr(contract, "description", "") if contract is not None else ""),
                        symbol,
                    ),
                    "side": str(getattr(execution, "side", "") or ""),
                    "shares": _safe_float(getattr(execution, "shares", 0.0), 0.0),
                    "price": _safe_float(getattr(execution, "price", 0.0), 0.0),
                    "order_id": _safe_int(getattr(execution, "orderId", 0), 0),
                    "perm_id": _safe_int(getattr(execution, "permId", 0), 0),
                    "exec_id": str(getattr(execution, "execId", "") or ""),
                    "commission": commission,
                    "commission_currency": str(getattr(report, "currency", "") or "") if report is not None else "",
                    "realized_pnl": _safe_float_or_none(getattr(report, "realizedPNL", None) if report is not None else None),
                    "_con_id": con_id,
                }
            )
        for ex in executions:
            ex.pop("_con_id", None)

        executions.sort(key=lambda r: str(r.get("time", "")), reverse=True)
        executions = executions[:100]

        return {
            "enabled": True,
            "ok": True,
            "message": "Snapshot IBKR OK",
            "latency_ms": latency_ms,
            "host": host,
            "port": int(port),
            "account": account,
            "managed_accounts": managed_accounts,
            "currency": currency,
            "account_summary": account_summary,
            "positions": positions,
            "positions_count": len(positions),
            "executions": executions,
            "executions_count": len(executions),
            "total_commission": total_commission,
            "total_commission_currency": (executions[0].get("commission_currency") if executions else currency),
            "generated_at": _now_text(),
        }
    except Exception as e:
        return {"enabled": True, "ok": False, "message": str(e)}
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass


def get_ibkr_snapshot(cfg: dict[str, Any], force: bool = False) -> dict[str, Any]:
    signature = _ibkr_snapshot_signature(cfg)
    now = time.time()
    with _BROKER_CACHE_LOCK:
        cached = _BROKER_CACHE.get("report")
        cached_sig = _BROKER_CACHE.get("signature")
        cached_ts = float(_BROKER_CACHE.get("timestamp") or 0.0)
        last_ok = _BROKER_CACHE.get("last_ok_report")
        last_ok_sig = _BROKER_CACHE.get("last_ok_signature")
        last_ok_ts = float(_BROKER_CACHE.get("last_ok_timestamp") or 0.0)
        age_s = int(now - cached_ts) if cached_ts else 0
        cache_valid = (
            not force
            and cached is not None
            and cached_sig == signature
            and age_s < BROKER_SNAPSHOT_CACHE_TTL_S
        )
        if cache_valid:
            return {**cached, "cached": True, "cache_age_s": age_s, "ttl_s": BROKER_SNAPSHOT_CACHE_TTL_S}

    report = _fetch_ibkr_snapshot_uncached(cfg)
    with _BROKER_CACHE_LOCK:
        _BROKER_CACHE["signature"] = signature
        _BROKER_CACHE["timestamp"] = now
        _BROKER_CACHE["report"] = report
        if bool(report.get("ok")):
            _BROKER_CACHE["last_ok_signature"] = signature
            _BROKER_CACHE["last_ok_timestamp"] = now
            _BROKER_CACHE["last_ok_report"] = report
        else:
            last_ok = _BROKER_CACHE.get("last_ok_report")
            last_ok_sig = _BROKER_CACHE.get("last_ok_signature")
            last_ok_ts = float(_BROKER_CACHE.get("last_ok_timestamp") or 0.0)

    if bool(report.get("ok")):
        return {**report, "cached": False, "cache_age_s": 0, "ttl_s": BROKER_SNAPSHOT_CACHE_TTL_S}

    # Keep last known-good IBKR snapshot to avoid UI "value disappearing" on transient failures.
    if (
        isinstance(last_ok, dict)
        and last_ok
        and str(last_ok_sig or "") == signature
        and bool(report.get("enabled", True))
    ):
        stale_age = int(now - last_ok_ts) if last_ok_ts else 0
        stale_reason = str(report.get("message", "") or "").strip() or "refresh failed"
        return {
            **last_ok,
            "cached": True,
            "cache_age_s": stale_age,
            "ttl_s": BROKER_SNAPSHOT_CACHE_TTL_S,
            "stale": True,
            "stale_reason": stale_reason,
            "message": f"Snapshot IBKR stale ({stale_reason})",
        }

    return {**report, "cached": False, "cache_age_s": 0, "ttl_s": BROKER_SNAPSHOT_CACHE_TTL_S}


def get_portfolio_snapshot(overrides: dict[str, Any] | None = None, force: bool = False) -> dict[str, Any]:
    cfg = _sanitize_config(overrides)
    paper = _build_paper_snapshot(cfg)
    ibkr = get_ibkr_snapshot(cfg, force=force)
    source = "request"

    # Robust fallback path:
    # if UI sends an incomplete/invalid IBKR config transiently, reuse last known
    # valid config (or defaults) so portfolio refresh works without requiring any
    # prior manual connectivity action.
    needs_fallback = (
        bool(ibkr.get("enabled", False))
        and not bool(ibkr.get("ok", False))
        and (
            ("host/port" in str(ibkr.get("message", "") or "").strip().lower())
            or (not _ibkr_cfg_is_complete(cfg))
        )
    )
    if needs_fallback:
        fallback_candidates: list[tuple[str, dict[str, Any]]] = []
        cached_cfg = _get_cached_portfolio_cfg()
        if isinstance(cached_cfg, dict):
            merged_cached = dict(cfg)
            merged_cached.update(cached_cfg)
            fallback_candidates.append(("cache", merged_cached))
        defaults_cfg = _sanitize_config(None)
        fallback_candidates.append(("defaults", defaults_cfg))

        primary_sig = _ibkr_snapshot_signature(cfg)
        for src, candidate_cfg in fallback_candidates:
            if _ibkr_snapshot_signature(candidate_cfg) == primary_sig:
                continue
            if not _ibkr_cfg_is_complete(candidate_cfg):
                continue
            candidate_ibkr = get_ibkr_snapshot(candidate_cfg, force=force)
            if bool(candidate_ibkr.get("ok", False)):
                ibkr = candidate_ibkr
                cfg = candidate_cfg
                source = src
                break

    if bool(ibkr.get("ok", False)):
        _cache_last_portfolio_cfg(cfg)

    return {
        "generated_at": _now_text(),
        "paper": paper,
        "ibkr": ibkr,
        "config_source": source,
        "config": {
            "paper_enabled": cfg.get("paper_enabled"),
            "paper_broker": cfg.get("paper_broker"),
            "allow_short": cfg.get("allow_short"),
            "paper_state": cfg.get("paper_state"),
            "ibkr_host": cfg.get("ibkr_host"),
            "ibkr_port": cfg.get("ibkr_port"),
            "ibkr_account": cfg.get("ibkr_account"),
            "ibkr_capital_limit": cfg.get("ibkr_capital_limit"),
            "ibkr_existing_positions_policy": cfg.get("ibkr_existing_positions_policy"),
        },
    }


def _extract_log_day(timestamp_value: Any) -> str:
    raw = str(timestamp_value or "").strip()
    if not raw:
        return datetime.now(_WEB_TZ).strftime("%Y-%m-%d")
    if len(raw) >= 10 and raw[4:5] == "-" and raw[7:8] == "-":
        return raw[:10]
    try:
        cleaned = raw.replace("Z", "+00:00")
        return datetime.fromisoformat(cleaned).date().isoformat()
    except Exception:
        return datetime.now(_WEB_TZ).strftime("%Y-%m-%d")


def _normalize_llm_error(err: Any) -> str:
    text = str(err or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    if lowered.startswith("parse_failed"):
        return "parse_failed"
    if "json invalide" in lowered or "json tronque" in lowered or "json truncated" in lowered:
        return "json_invalid_or_truncated"
    if "timeout" in lowered:
        return "timeout"
    if "rate limit" in lowered or "quota" in lowered or "429" in lowered:
        return "rate_limited"
    if "503" in lowered:
        return "api_503"
    if "502" in lowered:
        return "api_502"
    if "500" in lowered:
        return "api_500"
    return text[:72]


def _is_api_error_message(err: Any) -> bool:
    lowered = str(err or "").strip().lower()
    if not lowered:
        return False
    markers = ("503", "502", "500", "429", "api", "quota", "rate limit", "service unavailable", "timeout")
    return any(marker in lowered for marker in markers)


def get_llm_health_report(days: int = 14, limit: int = 20000) -> dict[str, Any]:
    rows = _read_jsonl_rows(LLM_CALLS_LOG_PATH, limit=max(1000, int(limit)))
    by_day: dict[str, dict[str, Any]] = {}

    for row in rows:
        day = _extract_log_day(row.get("timestamp"))
        bucket = by_day.setdefault(
            day,
            {
                "day": day,
                "total": 0,
                "ok": 0,
                "error": 0,
                "api_error": 0,
                "no_decision": 0,
                "models": Counter(),
                "errors": Counter(),
            },
        )
        bucket["total"] += 1

        decision = str(row.get("decision", "") or "").strip().upper()
        if decision in {"NO_DECISION", ""}:
            bucket["no_decision"] += 1

        status = str(row.get("llm_status", "") or "").strip().lower()
        model = str(row.get("llm_model", "") or "").strip()
        if model:
            bucket["models"][model] += 1

        if status in {"ok", "success"}:
            bucket["ok"] += 1
            continue

        bucket["error"] += 1
        err_key = _normalize_llm_error(row.get("llm_error"))
        if err_key:
            bucket["errors"][err_key] += 1
        if status in {"api_error", "http_error"} or _is_api_error_message(row.get("llm_error")):
            bucket["api_error"] += 1

    ordered_days = sorted(by_day.keys(), reverse=True)[: max(1, int(days))]
    day_rows: list[dict[str, Any]] = []
    totals = {"total": 0, "ok": 0, "error": 0, "api_error": 0, "no_decision": 0}
    for day in ordered_days:
        bucket = by_day[day]
        total = int(bucket.get("total", 0) or 0)
        error = int(bucket.get("error", 0) or 0)
        no_decision = int(bucket.get("no_decision", 0) or 0)
        top_errors = [
            {"label": key, "count": int(count)}
            for key, count in bucket.get("errors", Counter()).most_common(3)
        ]
        top_model = ""
        models_counter = bucket.get("models", Counter())
        if hasattr(models_counter, "most_common"):
            pairs = models_counter.most_common(1)
            if pairs:
                top_model = str(pairs[0][0])
        day_row = {
            "day": day,
            "total": total,
            "ok": int(bucket.get("ok", 0) or 0),
            "error": error,
            "api_error": int(bucket.get("api_error", 0) or 0),
            "no_decision": no_decision,
            "error_rate_pct": round((error / total) * 100.0, 2) if total > 0 else 0.0,
            "no_decision_rate_pct": round((no_decision / total) * 100.0, 2) if total > 0 else 0.0,
            "top_errors": top_errors,
            "top_model": top_model,
        }
        day_rows.append(day_row)
        for key in totals.keys():
            totals[key] += int(day_row.get(key, 0) or 0)

    totals["error_rate_pct"] = round((totals["error"] / totals["total"]) * 100.0, 2) if totals["total"] > 0 else 0.0
    totals["no_decision_rate_pct"] = (
        round((totals["no_decision"] / totals["total"]) * 100.0, 2) if totals["total"] > 0 else 0.0
    )

    return {
        "generated_at": _now_text(),
        "available": bool(rows),
        "path": str(LLM_CALLS_LOG_PATH),
        "days_requested": max(1, int(days)),
        "rows_loaded": len(rows),
        "rows": day_rows,
        "totals": totals,
    }


def _first_env(*names: str) -> str:
    for name in names:
        raw = os.getenv(name, "")
        val = str(raw or "").strip()
        if val:
            return val
    return ""


def _load_gemini_key_no_side_effect() -> str:
    key = _first_env("GEMINI_API_KEY")
    if key:
        return key

    cfg_path = BASE_DIR / "gemini_config.json"
    if not cfg_path.exists():
        return ""

    try:
        payload = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    return str(payload.get("api_key", "") or "").strip()


def _service_row(
    service_id: str,
    label: str,
    state: str,
    message: str,
    *,
    required: bool = False,
    latency_ms: int | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": service_id,
        "label": label,
        "state": state,
        "required": bool(required),
        "message": str(message),
        "latency_ms": latency_ms,
        "details": details or {},
    }


def _http_get_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout_s: float = 4.0,
) -> tuple[int, dict[str, Any] | None, str | None, int]:
    started = time.perf_counter()
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=timeout_s)
    except Exception as e:
        elapsed = int((time.perf_counter() - started) * 1000)
        return 0, None, str(e), elapsed

    elapsed = int((time.perf_counter() - started) * 1000)
    try:
        payload = resp.json()
    except Exception:
        payload = None
    return int(resp.status_code), payload, None, elapsed


def _check_gemini(decision_engine: str) -> dict[str, Any]:
    required = decision_engine == "llm"
    key = _load_gemini_key_no_side_effect()
    if not key:
        state = "error" if required else "warning"
        msg = "GEMINI_API_KEY absent (LLM indisponible)" if required else "Cle Gemini non configuree"
        return _service_row("gemini", "Gemini LLM", state, msg, required=required)

    try:
        from google import genai  # noqa: F401
    except Exception as e:
        state = "error" if required else "warning"
        return _service_row("gemini", "Gemini LLM", state, f"google-genai indisponible: {e}", required=required)

    status, payload, err, latency = _http_get_json(
        "https://generativelanguage.googleapis.com/v1beta/models",
        params={"key": key},
        timeout_s=4.0,
    )
    if err:
        return _service_row("gemini", "Gemini LLM", "error", f"Erreur reseau: {err}", required=required, latency_ms=latency)

    if status == 200 and isinstance(payload, dict):
        models = payload.get("models", []) if isinstance(payload.get("models", []), list) else []
        return _service_row(
            "gemini",
            "Gemini LLM",
            "ok",
            f"Connexion OK ({len(models)} modeles visibles)",
            required=required,
            latency_ms=latency,
        )

    message = ""
    if isinstance(payload, dict):
        message = (
            str(payload.get("error", {}).get("message", "")).strip()
            or str(payload.get("message", "")).strip()
        )
    if not message:
        message = f"HTTP {status}"
    return _service_row("gemini", "Gemini LLM", "error", message, required=required, latency_ms=latency)


def _check_newsapi() -> dict[str, Any]:
    key = _first_env("NEWSAPI_KEY", "BUBO_NEWSAPI_KEY")
    if not key:
        return _service_row("newsapi", "NewsAPI", "warning", "Cle non configuree", required=False)

    status, payload, err, latency = _http_get_json(
        "https://newsapi.org/v2/everything",
        params={"q": "market", "pageSize": 1, "language": "en", "apiKey": key},
        timeout_s=4.0,
    )
    if err:
        return _service_row("newsapi", "NewsAPI", "error", f"Erreur reseau: {err}", latency_ms=latency)

    if status == 200 and isinstance(payload, dict) and str(payload.get("status", "")).lower() == "ok":
        total = payload.get("totalResults")
        msg = f"Connexion OK (totalResults={total})" if total is not None else "Connexion OK"
        return _service_row("newsapi", "NewsAPI", "ok", msg, latency_ms=latency)

    message = ""
    if isinstance(payload, dict):
        message = str(payload.get("message", "")).strip()
    if not message:
        message = f"HTTP {status}"
    return _service_row("newsapi", "NewsAPI", "error", message, latency_ms=latency)


def _check_finnhub() -> dict[str, Any]:
    key = _first_env("FINNHUB_KEY", "BUBO_FINNHUB_KEY")
    if not key:
        return _service_row("finnhub", "Finnhub", "warning", "Cle non configuree", required=False)

    status, payload, err, latency = _http_get_json(
        "https://finnhub.io/api/v1/quote",
        params={"symbol": "AAPL", "token": key},
        timeout_s=4.0,
    )
    if err:
        return _service_row("finnhub", "Finnhub", "error", f"Erreur reseau: {err}", latency_ms=latency)

    if status == 200 and isinstance(payload, dict) and "c" in payload:
        return _service_row("finnhub", "Finnhub", "ok", "Connexion OK", latency_ms=latency)

    message = ""
    if isinstance(payload, dict):
        message = str(payload.get("error", "")).strip()
    if not message:
        message = f"HTTP {status}"
    return _service_row("finnhub", "Finnhub", "error", message, latency_ms=latency)


def _check_stocktwits() -> dict[str, Any]:
    probe_url = f"{STOCKTWITS_BASE_URL}/streams/symbol/{STOCKTWITS_TEST_SYMBOL}.json"
    started = time.perf_counter()
    req = urllib.request.Request(
        probe_url,
        headers={
            "User-Agent": "Bubo/1.0 connectivity-check",
            "Accept": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=4.0) as resp:
            status = int(getattr(resp, "status", 200) or 200)
            body = resp.read()
    except urllib.error.HTTPError as e:
        latency = int((time.perf_counter() - started) * 1000)
        status = int(getattr(e, "code", 0) or 0)
        if status == 429:
            return _service_row(
                "stocktwits",
                "Stocktwits",
                "warning",
                f"Rate limit (HTTP 429) sur {STOCKTWITS_TEST_SYMBOL}",
                latency_ms=latency,
                details={"base_url": STOCKTWITS_BASE_URL, "symbol": STOCKTWITS_TEST_SYMBOL},
            )
        return _service_row(
            "stocktwits",
            "Stocktwits",
            "warning",
            f"HTTP {status} ({STOCKTWITS_TEST_SYMBOL})",
            latency_ms=latency,
            details={"base_url": STOCKTWITS_BASE_URL, "symbol": STOCKTWITS_TEST_SYMBOL},
        )
    except Exception as e:
        latency = int((time.perf_counter() - started) * 1000)
        return _service_row("stocktwits", "Stocktwits", "warning", f"Erreur reseau: {e}", latency_ms=latency)

    latency = int((time.perf_counter() - started) * 1000)
    payload: dict[str, Any] | None = None
    try:
        decoded = json.loads(body.decode("utf-8"))
        if isinstance(decoded, dict):
            payload = decoded
    except Exception:
        payload = None

    if status == 200 and isinstance(payload, dict):
        count = payload.get("messages", [])
        messages_count = len(count) if isinstance(count, list) else 0
        return _service_row(
            "stocktwits",
            "Stocktwits",
            "ok",
            f"Flux public OK ({STOCKTWITS_TEST_SYMBOL}, {messages_count} msgs)",
            latency_ms=latency,
            details={"base_url": STOCKTWITS_BASE_URL, "symbol": STOCKTWITS_TEST_SYMBOL},
        )

    return _service_row(
        "stocktwits",
        "Stocktwits",
        "warning",
        f"Reponse non-JSON (HTTP {status}) sur {STOCKTWITS_TEST_SYMBOL}",
        latency_ms=latency,
        details={"base_url": STOCKTWITS_BASE_URL, "symbol": STOCKTWITS_TEST_SYMBOL},
    )


def _check_reddit_public() -> dict[str, Any]:
    subreddit = REDDIT_TEST_SUBREDDIT
    query = REDDIT_TEST_QUERY
    encoded_query = urllib.parse.quote(query, safe="")
    probe_url = (
        f"https://www.reddit.com/r/{subreddit}/search.json"
        f"?q={encoded_query}&sort=new&restrict_sr=1&limit=5&t=week"
    )
    started = time.perf_counter()
    req = urllib.request.Request(
        probe_url,
        headers={
            "User-Agent": "Bubo/1.0 connectivity-check",
            "Accept": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=4.0) as resp:
            status = int(getattr(resp, "status", 200) or 200)
            body = resp.read()
    except urllib.error.HTTPError as e:
        latency = int((time.perf_counter() - started) * 1000)
        status = int(getattr(e, "code", 0) or 0)
        if status == 429:
            return _service_row(
                "reddit_public",
                "Reddit (fallback public)",
                "warning",
                f"Rate limit (HTTP 429) sur r/{subreddit}",
                required=False,
                latency_ms=latency,
                details={"subreddit": subreddit, "query": query},
            )
        if status == 403:
            return _service_row(
                "reddit_public",
                "Reddit (fallback public)",
                "warning",
                f"HTTP 403 sur r/{subreddit} (blocage temporaire probable)",
                required=False,
                latency_ms=latency,
                details={"subreddit": subreddit, "query": query},
            )
        return _service_row(
            "reddit_public",
            "Reddit (fallback public)",
            "warning",
            f"HTTP {status} sur r/{subreddit}",
            required=False,
            latency_ms=latency,
            details={"subreddit": subreddit, "query": query},
        )
    except Exception as e:
        latency = int((time.perf_counter() - started) * 1000)
        return _service_row(
            "reddit_public",
            "Reddit (fallback public)",
            "warning",
            f"Erreur reseau: {e}",
            required=False,
            latency_ms=latency,
            details={"subreddit": subreddit, "query": query},
        )

    latency = int((time.perf_counter() - started) * 1000)
    payload: dict[str, Any] | None = None
    try:
        decoded = json.loads(body.decode("utf-8"))
        if isinstance(decoded, dict):
            payload = decoded
    except Exception:
        payload = None

    if status == 200 and isinstance(payload, dict):
        children = payload.get("data", {}).get("children", [])
        posts_count = len(children) if isinstance(children, list) else 0
        return _service_row(
            "reddit_public",
            "Reddit (fallback public)",
            "ok",
            f"Fallback public OK (r/{subreddit}, {posts_count} posts)",
            required=False,
            latency_ms=latency,
            details={"subreddit": subreddit, "query": query},
        )

    return _service_row(
        "reddit_public",
        "Reddit (fallback public)",
        "warning",
        f"Reponse non-JSON (HTTP {status}) sur r/{subreddit}",
        required=False,
        latency_ms=latency,
        details={"subreddit": subreddit, "query": query},
    )


def _ib_connect_probe(host: str, port: int, client_id: int) -> tuple[bool, str]:
    _ensure_asyncio_event_loop()

    try:
        from ib_insync import IB  # type: ignore
    except Exception as e:
        return False, f"ib_insync indisponible: {e}"

    ib = IB()
    try:
        probe_client_id = max(1, int(client_id) + 1000)
        try:
            ib.connect(host, port, clientId=probe_client_id, timeout=3, readonly=True)
        except TypeError:
            ib.connect(host, port, clientId=probe_client_id, timeout=3)
        if not ib.isConnected():
            return False, "Connexion etablie mais session IB non connectee"
        try:
            accounts = ib.managedAccounts() or []
        except Exception:
            accounts = []
        if accounts:
            return True, f"Session OK ({len(accounts)} compte(s))"
        return True, "Session OK"
    except Exception as e:
        return False, str(e)
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass


def _check_ib_gateway(cfg: dict[str, Any]) -> dict[str, Any]:
    required = bool(cfg.get("paper_enabled")) and str(cfg.get("paper_broker")) == "ibkr"
    host = str(cfg.get("ibkr_host") or "").strip()
    port = _coerce_int(cfg.get("ibkr_port"), 0, minimum=0)
    client_id = _coerce_int(cfg.get("ibkr_client_id"), 42, minimum=1)

    if not required:
        return _service_row("ib_gateway", "IB Gateway", "disabled", "Broker paper local (non requis)", required=False)
    if not host or port <= 0:
        return _service_row("ib_gateway", "IB Gateway", "error", "Host/port IBKR invalides", required=True)

    started = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=3.0):
            pass
    except Exception as e:
        latency = int((time.perf_counter() - started) * 1000)
        return _service_row(
            "ib_gateway",
            "IB Gateway",
            "error",
            f"Socket KO sur {host}:{port} ({e})",
            required=True,
            latency_ms=latency,
        )

    socket_latency = int((time.perf_counter() - started) * 1000)
    ok, message = _ib_connect_probe(host, port, client_id)
    state = "ok" if ok else "error"
    return _service_row(
        "ib_gateway",
        "IB Gateway",
        state,
        message,
        required=True,
        latency_ms=socket_latency,
        details={"host": host, "port": port},
    )


def _connectivity_signature(cfg: dict[str, Any]) -> str:
    payload = {
        "decision_engine": str(cfg.get("decision_engine", "")),
        "paper_enabled": bool(cfg.get("paper_enabled")),
        "paper_broker": str(cfg.get("paper_broker", "")),
        "ibkr_host": str(cfg.get("ibkr_host", "")),
        "ibkr_port": int(cfg.get("ibkr_port", 0) or 0),
        "ibkr_client_id": int(cfg.get("ibkr_client_id", 0) or 0),
        "reddit_test_subreddit": REDDIT_TEST_SUBREDDIT,
        "reddit_test_query": REDDIT_TEST_QUERY,
        "stocktwits_base_url": STOCKTWITS_BASE_URL,
        "stocktwits_test_symbol": STOCKTWITS_TEST_SYMBOL,
    }
    return json.dumps(payload, sort_keys=True)


def _compute_connectivity_report(cfg: dict[str, Any]) -> dict[str, Any]:
    services = [
        _check_gemini(str(cfg.get("decision_engine", "llm"))),
        _check_newsapi(),
        _check_finnhub(),
        _check_reddit_public(),
        _check_stocktwits(),
        _check_ib_gateway(cfg),
    ]

    summary = {"ok": 0, "warning": 0, "error": 0, "disabled": 0}
    for row in services:
        state = str(row.get("state", "warning"))
        if state not in summary:
            state = "warning"
        summary[state] += 1

    return {
        "generated_at": _now_text(),
        "ttl_s": CONNECTIVITY_CACHE_TTL_S,
        "services": services,
        "summary": summary,
    }


def get_connectivity_report(overrides: dict[str, Any] | None = None, force: bool = False) -> dict[str, Any]:
    cfg = _sanitize_config(overrides)
    signature = _connectivity_signature(cfg)
    now = time.time()

    with _CONNECTIVITY_CACHE_LOCK:
        cached = _CONNECTIVITY_CACHE.get("report")
        cached_sig = _CONNECTIVITY_CACHE.get("signature")
        cached_ts = float(_CONNECTIVITY_CACHE.get("timestamp") or 0.0)
        age_s = int(now - cached_ts) if cached_ts else 0
        cache_valid = (
            not force
            and cached is not None
            and cached_sig == signature
            and age_s < CONNECTIVITY_CACHE_TTL_S
        )
        if cache_valid:
            return {
                **cached,
                "cached": True,
                "cache_age_s": age_s,
                "config": {
                    "decision_engine": cfg.get("decision_engine"),
                    "paper_enabled": cfg.get("paper_enabled"),
                    "paper_broker": cfg.get("paper_broker"),
                    "allow_short": cfg.get("allow_short"),
                    "ibkr_host": cfg.get("ibkr_host"),
                    "ibkr_port": cfg.get("ibkr_port"),
                    "reddit_test_subreddit": REDDIT_TEST_SUBREDDIT,
                    "reddit_test_query": REDDIT_TEST_QUERY,
                    "stocktwits_base_url": STOCKTWITS_BASE_URL,
                    "stocktwits_test_symbol": STOCKTWITS_TEST_SYMBOL,
                },
            }

    report = _compute_connectivity_report(cfg)
    with _CONNECTIVITY_CACHE_LOCK:
        _CONNECTIVITY_CACHE["signature"] = signature
        _CONNECTIVITY_CACHE["timestamp"] = now
        _CONNECTIVITY_CACHE["report"] = report

    return {
        **report,
        "cached": False,
        "cache_age_s": 0,
        "config": {
            "decision_engine": cfg.get("decision_engine"),
            "paper_enabled": cfg.get("paper_enabled"),
            "paper_broker": cfg.get("paper_broker"),
            "allow_short": cfg.get("allow_short"),
            "ibkr_host": cfg.get("ibkr_host"),
            "ibkr_port": cfg.get("ibkr_port"),
            "reddit_test_subreddit": REDDIT_TEST_SUBREDDIT,
            "reddit_test_query": REDDIT_TEST_QUERY,
            "stocktwits_base_url": STOCKTWITS_BASE_URL,
            "stocktwits_test_symbol": STOCKTWITS_TEST_SYMBOL,
        },
    }


def get_default_config() -> dict[str, Any]:
    return {
        "decision_engine": os.getenv("BUBO_DECISION_ENGINE", "llm"),
        "universe_file": os.getenv("BUBO_UNIVERSE_FILE", "data/universe_us_1000_v1.txt"),
        "budget_mode": _normalize_budget_mode(os.getenv("BUBO_BUDGET_MODE", BUDGET_MODE_CUSTOM)),
        "preselect_top": _coerce_int(os.getenv("BUBO_PRESELECT_TOP", "60"), 60, minimum=1),
        "max_deep": _coerce_int(os.getenv("BUBO_MAX_DEEP", "8"), 8, minimum=1),
        "watch_interval_min": _coerce_int(os.getenv("BUBO_WATCH_INTERVAL_MIN", "30"), 30, minimum=1),
        "us_market_only": _env_bool("BUBO_US_MARKET_ONLY", True),
        "analyze_when_us_closed": _env_bool("BUBO_ANALYZE_WHEN_US_CLOSED", True),
        "capital": _coerce_float(os.getenv("BUBO_CAPITAL", "10000"), 10000.0, minimum=1.0),
        "allow_short": _env_bool("BUBO_ALLOW_SHORT", False),
        "paper_enabled": _env_bool("BUBO_PAPER_ENABLED", True),
        "paper_state": os.getenv("BUBO_PAPER_STATE", "data/paper_portfolio_state.json"),
        "paper_webhook": os.getenv("BUBO_PAPER_WEBHOOK", ""),
        "paper_broker": "ibkr",
        "ibkr_host": os.getenv("BUBO_IBKR_HOST", "127.0.0.1"),
        "ibkr_port": _coerce_int(os.getenv("BUBO_IBKR_PORT", "7497"), 7497, minimum=1),
        "ibkr_client_id": _coerce_int(os.getenv("BUBO_IBKR_CLIENT_ID", "42"), 42, minimum=1),
        "ibkr_account": os.getenv("BUBO_IBKR_ACCOUNT", ""),
        "ibkr_exchange": os.getenv("BUBO_IBKR_EXCHANGE", "SMART"),
        "ibkr_currency": os.getenv("BUBO_IBKR_CURRENCY", "USD"),
        "ibkr_capital_limit": _coerce_float(
            os.getenv("BUBO_IBKR_CAPITAL_LIMIT", os.getenv("BUBO_CAPITAL", "10000")),
            10000.0,
            minimum=1.0,
        ),
        "ibkr_existing_positions_policy": os.getenv("BUBO_IBKR_EXISTING_POSITIONS_POLICY", "include"),
        "no_finbert": _env_bool("BUBO_NO_FINBERT", True),
        "no_budget_gate": _env_bool("BUBO_NO_BUDGET_GATE", False),
        "gemini_model_chain": os.getenv("BUBO_GEMINI_MODEL_CHAIN", "gemini-2.5-flash"),
        "gemini_max_output_tokens": _coerce_int(os.getenv("BUBO_GEMINI_MAX_OUTPUT_TOKENS", "700"), 700, minimum=256),
        "gemini_thinking_budget": _coerce_int(os.getenv("BUBO_GEMINI_THINKING_BUDGET", "0"), 0, minimum=0),
        "gemini_prompt_max_events": _coerce_int(os.getenv("BUBO_GEMINI_PROMPT_MAX_EVENTS", "4"), 4, minimum=0),
        "gemini_prompt_max_headlines": _coerce_int(
            os.getenv("BUBO_GEMINI_PROMPT_MAX_HEADLINES", "3"),
            3,
            minimum=0,
        ),
        "gemini_prompt_max_posts": _coerce_int(os.getenv("BUBO_GEMINI_PROMPT_MAX_POSTS", "2"), 2, minimum=0),
        "gemini_prompt_max_post_chars": _coerce_int(
            os.getenv("BUBO_GEMINI_PROMPT_MAX_POST_CHARS", "80"),
            80,
            minimum=20,
        ),
    }


def _sanitize_config(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = get_default_config()
    payload = overrides or {}

    if "universe_file" in payload:
        cfg["universe_file"] = str(payload.get("universe_file") or "").strip()
    if "decision_engine" in payload:
        cfg["decision_engine"] = str(payload.get("decision_engine") or "").strip().lower()
    if "budget_mode" in payload:
        cfg["budget_mode"] = _normalize_budget_mode(payload.get("budget_mode"))
    if "paper_state" in payload:
        cfg["paper_state"] = str(payload.get("paper_state") or "").strip()
    if "paper_webhook" in payload:
        cfg["paper_webhook"] = str(payload.get("paper_webhook") or "").strip()
    if "ibkr_host" in payload:
        cfg["ibkr_host"] = str(payload.get("ibkr_host") or "").strip()
    if "ibkr_account" in payload:
        cfg["ibkr_account"] = str(payload.get("ibkr_account") or "").strip()
    if "ibkr_exchange" in payload:
        cfg["ibkr_exchange"] = str(payload.get("ibkr_exchange") or "").strip().upper()
    if "ibkr_currency" in payload:
        cfg["ibkr_currency"] = str(payload.get("ibkr_currency") or "").strip().upper()
    if "ibkr_existing_positions_policy" in payload:
        cfg["ibkr_existing_positions_policy"] = str(payload.get("ibkr_existing_positions_policy") or "").strip().lower()

    cfg["preselect_top"] = _coerce_int(payload.get("preselect_top", cfg["preselect_top"]), cfg["preselect_top"], minimum=1)
    cfg["max_deep"] = _coerce_int(payload.get("max_deep", cfg["max_deep"]), cfg["max_deep"], minimum=1)
    cfg["watch_interval_min"] = _coerce_int(
        payload.get("watch_interval_min", cfg["watch_interval_min"]),
        cfg["watch_interval_min"],
        minimum=1,
    )
    cfg["us_market_only"] = _coerce_bool(payload.get("us_market_only"), cfg["us_market_only"])
    cfg["analyze_when_us_closed"] = _coerce_bool(
        payload.get("analyze_when_us_closed"),
        cfg["analyze_when_us_closed"],
    )
    cfg["capital"] = _coerce_float(payload.get("capital", cfg["capital"]), cfg["capital"], minimum=1.0)
    cfg["allow_short"] = _coerce_bool(payload.get("allow_short"), cfg["allow_short"])
    cfg["ibkr_port"] = _coerce_int(payload.get("ibkr_port", cfg["ibkr_port"]), cfg["ibkr_port"], minimum=1)
    cfg["ibkr_client_id"] = _coerce_int(payload.get("ibkr_client_id", cfg["ibkr_client_id"]), cfg["ibkr_client_id"], minimum=1)
    cfg["ibkr_capital_limit"] = _coerce_float(
        payload.get("ibkr_capital_limit", cfg["ibkr_capital_limit"]),
        cfg["ibkr_capital_limit"],
        minimum=1.0,
    )
    cfg["paper_enabled"] = _coerce_bool(payload.get("paper_enabled"), cfg["paper_enabled"])
    cfg["no_finbert"] = _coerce_bool(payload.get("no_finbert"), cfg["no_finbert"])
    cfg["no_budget_gate"] = _coerce_bool(payload.get("no_budget_gate"), cfg["no_budget_gate"])
    cfg["gemini_max_output_tokens"] = _coerce_int(
        payload.get("gemini_max_output_tokens", cfg["gemini_max_output_tokens"]),
        cfg["gemini_max_output_tokens"],
        minimum=256,
    )
    cfg["gemini_thinking_budget"] = _coerce_int(
        payload.get("gemini_thinking_budget", cfg["gemini_thinking_budget"]),
        cfg["gemini_thinking_budget"],
        minimum=0,
    )
    cfg["gemini_prompt_max_events"] = _coerce_int(
        payload.get("gemini_prompt_max_events", cfg["gemini_prompt_max_events"]),
        cfg["gemini_prompt_max_events"],
        minimum=0,
    )
    cfg["gemini_prompt_max_headlines"] = _coerce_int(
        payload.get("gemini_prompt_max_headlines", cfg["gemini_prompt_max_headlines"]),
        cfg["gemini_prompt_max_headlines"],
        minimum=0,
    )
    cfg["gemini_prompt_max_posts"] = _coerce_int(
        payload.get("gemini_prompt_max_posts", cfg["gemini_prompt_max_posts"]),
        cfg["gemini_prompt_max_posts"],
        minimum=0,
    )
    cfg["gemini_prompt_max_post_chars"] = _coerce_int(
        payload.get("gemini_prompt_max_post_chars", cfg["gemini_prompt_max_post_chars"]),
        cfg["gemini_prompt_max_post_chars"],
        minimum=20,
    )
    if "gemini_model_chain" in payload:
        cfg["gemini_model_chain"] = str(payload.get("gemini_model_chain") or "").strip() or cfg["gemini_model_chain"]
    if cfg["decision_engine"] not in {"llm", "rules"}:
        cfg["decision_engine"] = "llm"
    cfg["paper_broker"] = "ibkr"
    if cfg["ibkr_existing_positions_policy"] not in {"include", "ignore"}:
        cfg["ibkr_existing_positions_policy"] = "include"
    return _apply_budget_mode(cfg)


def build_engine_command(mode: str, overrides: dict[str, Any] | None = None) -> tuple[list[str], dict[str, Any]]:
    if mode not in {"once", "watch", "screen"}:
        raise ValueError(f"Unsupported mode: {mode}")

    cfg = _sanitize_config(overrides)
    cmd = [sys.executable, "bubo_engine.py"]

    if mode == "watch":
        cmd.append("--watch")
    elif mode == "screen":
        cmd.append("--screen-only")

    if cfg["universe_file"]:
        cmd.extend(["--universe-file", cfg["universe_file"]])
        cmd.extend(["--preselect-top", str(cfg["preselect_top"])])
        cmd.extend(["--max-deep", str(cfg["max_deep"])])
    cmd.extend(["--watch-interval-min", str(cfg["watch_interval_min"])])

    cmd.extend(["--decision-engine", str(cfg["decision_engine"])])

    if cfg["no_budget_gate"]:
        cmd.append("--no-budget-gate")
    if cfg["us_market_only"]:
        cmd.append("--us-market-only")
    else:
        cmd.append("--no-us-market-only")
    if cfg["analyze_when_us_closed"]:
        cmd.append("--analyze-when-us-closed")
    else:
        cmd.append("--no-analyze-when-us-closed")

    cmd.extend(["--capital", str(cfg["capital"])])
    if cfg["allow_short"]:
        cmd.append("--allow-short")
    else:
        cmd.append("--no-allow-short")

    if cfg["paper_enabled"]:
        cmd.append("--paper")
    if cfg["paper_state"]:
        cmd.extend(["--paper-state", cfg["paper_state"]])
    if cfg["paper_webhook"]:
        cmd.extend(["--paper-webhook", cfg["paper_webhook"]])
    cmd.extend(["--paper-broker", "ibkr"])
    cmd.extend(["--ibkr-host", str(cfg["ibkr_host"])])
    cmd.extend(["--ibkr-port", str(cfg["ibkr_port"])])
    cmd.extend(["--ibkr-client-id", str(cfg["ibkr_client_id"])])
    if cfg["ibkr_account"]:
        cmd.extend(["--ibkr-account", str(cfg["ibkr_account"])])
    cmd.extend(["--ibkr-exchange", str(cfg["ibkr_exchange"])])
    cmd.extend(["--ibkr-currency", str(cfg["ibkr_currency"])])
    cmd.extend(["--ibkr-capital-limit", str(cfg["ibkr_capital_limit"])])
    cmd.extend(["--ibkr-existing-positions-policy", str(cfg["ibkr_existing_positions_policy"])])
    if cfg["no_finbert"]:
        cmd.append("--no-finbert")

    return cmd, cfg


def _stream_process_output(proc: subprocess.Popen[str], mode: str):
    try:
        if proc.stdout is not None:
            for line in proc.stdout:
                _append_log(line)
    finally:
        if proc.stdout is not None:
            try:
                proc.stdout.close()
            except Exception:
                pass
        rc = proc.wait()
        _append_log(f"Process mode={mode} finished with exit code {rc}")
        with _STATE_LOCK:
            if _RUN_STATE.get("process") is proc:
                _RUN_STATE["process"] = None
                _RUN_STATE["mode"] = None
                _RUN_STATE["command"] = None
                _RUN_STATE["started_epoch"] = None
                _RUN_STATE["started_at"] = None
                _RUN_STATE["last_exit_code"] = rc
                _RUN_STATE["last_finished_at"] = _now_text()
        with _SYSTEM_STATUS_CACHE_LOCK:
            _SYSTEM_STATUS_CACHE["timestamp"] = 0.0
            _SYSTEM_STATUS_CACHE["status"] = None


def start_process(mode: str, overrides: dict[str, Any] | None = None) -> tuple[bool, str, list[str] | None]:
    cmd, cfg = build_engine_command(mode, overrides)
    proc_env = _build_process_env(cfg)

    with _STATE_LOCK:
        current = _RUN_STATE.get("process")
        if current is not None and current.poll() is None:
            return False, "Un processus est deja en cours. Arrete-le avant d'en lancer un autre.", None

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(BASE_DIR),
                env=proc_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except Exception as e:
            return False, f"Echec du lancement: {e}", None

        _RUN_STATE["process"] = proc
        _RUN_STATE["mode"] = mode
        _RUN_STATE["command"] = cmd
        _RUN_STATE["started_epoch"] = time.time()
        _RUN_STATE["started_at"] = _now_text()
        _RUN_STATE["last_exit_code"] = None

        _append_log(f"Started mode={mode} with config: {cfg}")
        _append_log("Command: " + " ".join(cmd))
        with _SYSTEM_STATUS_CACHE_LOCK:
            _SYSTEM_STATUS_CACHE["timestamp"] = 0.0
            _SYSTEM_STATUS_CACHE["status"] = None
        t = threading.Thread(target=_stream_process_output, args=(proc, mode), daemon=True)
        t.start()
        return True, "Processus lance.", cmd


def stop_process() -> tuple[bool, str]:
    with _STATE_LOCK:
        proc = _RUN_STATE.get("process")
        if proc is None or proc.poll() is not None:
            return False, "Aucun processus actif."

    try:
        proc.terminate()
        proc.wait(timeout=10)
        _append_log("Process terminated by user request.")
        with _SYSTEM_STATUS_CACHE_LOCK:
            _SYSTEM_STATUS_CACHE["timestamp"] = 0.0
            _SYSTEM_STATUS_CACHE["status"] = None
        return True, "Processus arrete."
    except Exception:
        try:
            proc.kill()
            _append_log("Process killed after terminate timeout.")
            with _SYSTEM_STATUS_CACHE_LOCK:
                _SYSTEM_STATUS_CACHE["timestamp"] = 0.0
                _SYSTEM_STATUS_CACHE["status"] = None
            return True, "Processus force a s'arreter."
        except Exception as e:
            return False, f"Impossible d'arreter le processus: {e}"


def get_runtime_status() -> dict[str, Any]:
    with _STATE_LOCK:
        proc = _RUN_STATE.get("process")
        running = proc is not None and proc.poll() is None
        started_epoch = _RUN_STATE.get("started_epoch")
        uptime_s = int(time.time() - started_epoch) if running and started_epoch else 0
        payload = {
            "running": running,
            "mode": _RUN_STATE.get("mode"),
            "pid": proc.pid if running else None,
            "command": _RUN_STATE.get("command"),
            "started_at": _RUN_STATE.get("started_at"),
            "uptime_s": uptime_s,
            "last_exit_code": _RUN_STATE.get("last_exit_code"),
            "last_finished_at": _RUN_STATE.get("last_finished_at"),
            "us_market": get_us_market_clock(),
        }
    system_status = _get_system_status(running=running, force=False)
    payload["finbert"] = system_status.get("finbert", {})
    payload["gpu"] = system_status.get("gpu", {})
    payload["system_status_generated_at"] = system_status.get("generated_at")
    return payload


def list_output_files(limit: int = 40) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    allowed = {".csv", ".json", ".jsonl", ".md", ".png", ".log"}
    scan_roots = [("data", DATA_DIR), ("charts", CHARTS_DIR)]

    for scope, root in scan_roots:
        if not root.exists():
            continue
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            if p.suffix.lower() not in allowed:
                continue
            if "cache" in p.parts:
                continue
            rel = p.relative_to(root).as_posix()
            rows.append(
                {
                    "scope": scope,
                    "name": rel,
                    "size_kb": round(p.stat().st_size / 1024, 1),
                    "modified": datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                    "url": f"/api/download/{scope}/{rel}",
                    "mtime": p.stat().st_mtime,
                }
            )

    rows.sort(key=lambda r: r["mtime"], reverse=True)
    trimmed = rows[: max(1, limit)]
    for row in trimmed:
        row.pop("mtime", None)
    return trimmed


def _parse_payload() -> dict[str, Any]:
    payload = request.get_json(silent=True)
    return payload if isinstance(payload, dict) else {}


def _unauthorized_response():
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "message": "Authentication required"}), 401
    return redirect(url_for("login", next=request.path))


@app.before_request
def enforce_auth():
    if not AUTH_ENABLED:
        return None

    open_paths = {"/health", "/login"}
    if request.path in open_paths or request.path.startswith("/static/"):
        return None

    if _is_authenticated():
        return None

    return _unauthorized_response()


@app.route("/login", methods=["GET", "POST"])
def login():
    if not AUTH_ENABLED:
        return redirect(url_for("index"))

    error = ""
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if _check_credentials(username, password):
            session["auth_ok"] = True
            session["auth_user"] = AUTH_USER
            target = request.args.get("next") or "/"
            if not str(target).startswith("/"):
                target = "/"
            _append_log(f"User '{AUTH_USER}' logged in")
            return redirect(target)
        error = "Identifiants invalides."

    return render_template("login.html", error=error)


@app.post("/logout")
def logout():
    session.pop("auth_ok", None)
    session.pop("auth_user", None)
    return redirect(url_for("login"))


@app.get("/")
def index():
    return render_template(
        "index.html",
        defaults=get_default_config(),
        timezone=os.getenv("TZ", "Europe/Paris"),
        auth_enabled=AUTH_ENABLED,
        auth_user=session.get("auth_user", AUTH_USER if AUTH_ENABLED else ""),
    )


@app.get("/health")
def health():
    return jsonify({"status": "ok", "time": _now_text()})


@app.get("/api/status")
def api_status():
    return jsonify(get_runtime_status())


@app.get("/api/logs")
def api_logs():
    tail = _coerce_int(request.args.get("tail", 1000), 1000, minimum=10)
    tail = min(20000, tail)
    memory_lines = list(_LOGS)
    if memory_lines and tail <= len(memory_lines):
        lines = memory_lines[-tail:]
        source = "memory"
    else:
        lines = _tail_file_lines(RUNTIME_LOG_PATH, tail)
        source = "file"
        if not lines and memory_lines:
            lines = memory_lines[-tail:]
            source = "memory"
    return jsonify({"lines": lines, "count": len(lines), "tail": tail, "source": source})


@app.get("/api/files")
def api_files():
    limit = _coerce_int(request.args.get("limit", 40), 40, minimum=5)
    return jsonify({"files": list_output_files(limit=limit)})


@app.route("/api/connectivity", methods=["GET", "POST"])
def api_connectivity():
    payload = _parse_payload() if request.method == "POST" else {}
    force = _coerce_bool(request.args.get("force"), False) or _coerce_bool(payload.get("force"), False)
    report = get_connectivity_report(payload, force=force)
    return jsonify(report)


@app.route("/api/portfolio", methods=["GET", "POST"])
def api_portfolio():
    payload = _parse_payload() if request.method == "POST" else {}
    force = _coerce_bool(request.args.get("force"), False) or _coerce_bool(payload.get("force"), False)
    report = get_portfolio_snapshot(payload, force=force)
    return jsonify(report)


@app.get("/api/llm-health")
def api_llm_health():
    days = _coerce_int(request.args.get("days", 14), 14, minimum=1)
    limit = _coerce_int(request.args.get("limit", 20000), 20000, minimum=1000)
    report = get_llm_health_report(days=days, limit=limit)
    return jsonify(report)


@app.get("/api/download/<scope>/<path:rel_path>")
def api_download(scope: str, rel_path: str):
    if scope == "data":
        root = DATA_DIR
    elif scope == "charts":
        root = CHARTS_DIR
    else:
        abort(404)

    target = (root / rel_path).resolve()
    root_resolved = root.resolve()
    if not str(target).startswith(str(root_resolved)):
        abort(400)
    if not target.exists() or not target.is_file():
        abort(404)
    return send_file(target, as_attachment=True)


@app.post("/api/run-once")
def api_run_once():
    ok, msg, cmd = start_process("once", _parse_payload())
    code = 200 if ok else 409
    return jsonify({"ok": ok, "message": msg, "command": cmd}), code


@app.post("/api/start-watch")
def api_start_watch():
    ok, msg, cmd = start_process("watch", _parse_payload())
    code = 200 if ok else 409
    return jsonify({"ok": ok, "message": msg, "command": cmd}), code


@app.post("/api/screen-only")
def api_screen_only():
    ok, msg, cmd = start_process("screen", _parse_payload())
    code = 200 if ok else 409
    return jsonify({"ok": ok, "message": msg, "command": cmd}), code


@app.post("/api/stop")
def api_stop():
    ok, msg = stop_process()
    code = 200 if ok else 409
    return jsonify({"ok": ok, "message": msg}), code


def main():
    parser = argparse.ArgumentParser(description="BUBO Web Interface")
    parser.add_argument("--host", type=str, default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.getenv("BUBO_WEB_PORT", "7654")))
    args = parser.parse_args()

    _append_log("BUBO web interface started.")
    if AUTH_ENABLED and AUTH_USER == "admin" and AUTH_PASSWORD == "change-me":
        _append_log("WARNING: default web credentials are active (admin/change-me). Update .env.")
    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
