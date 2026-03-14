import argparse
import hmac
import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, session, url_for


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
CHARTS_DIR = BASE_DIR / "charts"

DATA_DIR.mkdir(parents=True, exist_ok=True)
CHARTS_DIR.mkdir(parents=True, exist_ok=True)

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

_CONNECTIVITY_CACHE_LOCK = threading.Lock()
_CONNECTIVITY_CACHE: dict[str, Any] = {
    "signature": "",
    "timestamp": 0.0,
    "report": None,
}


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _append_log(line: str):
    msg = line.rstrip("\n")
    if not msg:
        return
    _LOGS.append(f"[{_now_text()}] {msg}")


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


def _check_reddit() -> dict[str, Any]:
    client_id = _first_env("BUBO_REDDIT_CLIENT_ID", "REDDIT_CLIENT_ID")
    client_secret = _first_env("BUBO_REDDIT_CLIENT_SECRET", "REDDIT_CLIENT_SECRET")
    user_agent = _first_env("BUBO_REDDIT_USER_AGENT", "REDDIT_USER_AGENT") or "Bubo/1.0 connectivity-check"

    if client_id and client_secret:
        try:
            import praw  # type: ignore
        except Exception as e:
            return _service_row("reddit", "Reddit", "error", f"praw indisponible: {e}", required=False)

        started = time.perf_counter()
        try:
            reddit = praw.Reddit(
                client_id=client_id,
                client_secret=client_secret,
                user_agent=user_agent,
            )
            _ = reddit.subreddit("stocks").display_name
            latency = int((time.perf_counter() - started) * 1000)
            return _service_row("reddit", "Reddit", "ok", "OAuth Reddit OK", latency_ms=latency)
        except Exception as e:
            latency = int((time.perf_counter() - started) * 1000)
            return _service_row("reddit", "Reddit", "error", f"OAuth echec: {e}", latency_ms=latency)

    status, _payload, err, latency = _http_get_json(
        "https://www.reddit.com/r/stocks/about.json",
        headers={"User-Agent": user_agent},
        timeout_s=4.0,
    )
    if err:
        return _service_row("reddit", "Reddit", "warning", f"Mode public indisponible: {err}", latency_ms=latency)
    if status == 200:
        return _service_row("reddit", "Reddit", "warning", "OAuth non configuree (fallback public actif)", latency_ms=latency)
    if status == 429:
        return _service_row("reddit", "Reddit", "warning", "Rate limit Reddit (HTTP 429)", latency_ms=latency)
    return _service_row("reddit", "Reddit", "warning", f"OAuth non configuree, fallback HTTP {status}", latency_ms=latency)


def _check_stocktwits() -> dict[str, Any]:
    status, payload, err, latency = _http_get_json(
        "https://api.stocktwits.com/api/2/streams/symbol/AAPL.json",
        timeout_s=4.0,
    )
    if err:
        return _service_row("stocktwits", "Stocktwits", "warning", f"Erreur reseau: {err}", latency_ms=latency)
    if status == 200 and isinstance(payload, dict):
        return _service_row("stocktwits", "Stocktwits", "ok", "Flux public OK", latency_ms=latency)
    if status == 429:
        return _service_row("stocktwits", "Stocktwits", "warning", "Rate limit (HTTP 429)", latency_ms=latency)
    return _service_row("stocktwits", "Stocktwits", "warning", f"HTTP {status}", latency_ms=latency)


def _ib_connect_probe(host: str, port: int, client_id: int) -> tuple[bool, str]:
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
    }
    return json.dumps(payload, sort_keys=True)


def _compute_connectivity_report(cfg: dict[str, Any]) -> dict[str, Any]:
    services = [
        _check_gemini(str(cfg.get("decision_engine", "llm"))),
        _check_newsapi(),
        _check_finnhub(),
        _check_reddit(),
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
                    "ibkr_host": cfg.get("ibkr_host"),
                    "ibkr_port": cfg.get("ibkr_port"),
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
            "ibkr_host": cfg.get("ibkr_host"),
            "ibkr_port": cfg.get("ibkr_port"),
        },
    }


def get_default_config() -> dict[str, Any]:
    return {
        "decision_engine": os.getenv("BUBO_DECISION_ENGINE", "llm"),
        "universe_file": os.getenv("BUBO_UNIVERSE_FILE", "data/universe_global_v1.txt"),
        "preselect_top": _coerce_int(os.getenv("BUBO_PRESELECT_TOP", "60"), 60, minimum=1),
        "max_deep": _coerce_int(os.getenv("BUBO_MAX_DEEP", "20"), 20, minimum=1),
        "capital": _coerce_float(os.getenv("BUBO_CAPITAL", "10000"), 10000.0, minimum=1.0),
        "paper_enabled": _env_bool("BUBO_PAPER_ENABLED", True),
        "paper_state": os.getenv("BUBO_PAPER_STATE", "data/paper_portfolio_state.json"),
        "paper_webhook": os.getenv("BUBO_PAPER_WEBHOOK", ""),
        "paper_broker": os.getenv("BUBO_PAPER_BROKER", "local"),
        "ibkr_host": os.getenv("BUBO_IBKR_HOST", "127.0.0.1"),
        "ibkr_port": _coerce_int(os.getenv("BUBO_IBKR_PORT", "7497"), 7497, minimum=1),
        "ibkr_client_id": _coerce_int(os.getenv("BUBO_IBKR_CLIENT_ID", "42"), 42, minimum=1),
        "ibkr_account": os.getenv("BUBO_IBKR_ACCOUNT", ""),
        "ibkr_exchange": os.getenv("BUBO_IBKR_EXCHANGE", "SMART"),
        "ibkr_currency": os.getenv("BUBO_IBKR_CURRENCY", "USD"),
        "no_finbert": _env_bool("BUBO_NO_FINBERT", True),
        "no_budget_gate": _env_bool("BUBO_NO_BUDGET_GATE", False),
    }


def _sanitize_config(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = get_default_config()
    payload = overrides or {}

    if "universe_file" in payload:
        cfg["universe_file"] = str(payload.get("universe_file") or "").strip()
    if "decision_engine" in payload:
        cfg["decision_engine"] = str(payload.get("decision_engine") or "").strip().lower()
    if "paper_state" in payload:
        cfg["paper_state"] = str(payload.get("paper_state") or "").strip()
    if "paper_webhook" in payload:
        cfg["paper_webhook"] = str(payload.get("paper_webhook") or "").strip()
    if "paper_broker" in payload:
        cfg["paper_broker"] = str(payload.get("paper_broker") or "").strip().lower()
    if "ibkr_host" in payload:
        cfg["ibkr_host"] = str(payload.get("ibkr_host") or "").strip()
    if "ibkr_account" in payload:
        cfg["ibkr_account"] = str(payload.get("ibkr_account") or "").strip()
    if "ibkr_exchange" in payload:
        cfg["ibkr_exchange"] = str(payload.get("ibkr_exchange") or "").strip().upper()
    if "ibkr_currency" in payload:
        cfg["ibkr_currency"] = str(payload.get("ibkr_currency") or "").strip().upper()

    cfg["preselect_top"] = _coerce_int(payload.get("preselect_top", cfg["preselect_top"]), cfg["preselect_top"], minimum=1)
    cfg["max_deep"] = _coerce_int(payload.get("max_deep", cfg["max_deep"]), cfg["max_deep"], minimum=1)
    cfg["capital"] = _coerce_float(payload.get("capital", cfg["capital"]), cfg["capital"], minimum=1.0)
    cfg["ibkr_port"] = _coerce_int(payload.get("ibkr_port", cfg["ibkr_port"]), cfg["ibkr_port"], minimum=1)
    cfg["ibkr_client_id"] = _coerce_int(payload.get("ibkr_client_id", cfg["ibkr_client_id"]), cfg["ibkr_client_id"], minimum=1)
    cfg["paper_enabled"] = _coerce_bool(payload.get("paper_enabled"), cfg["paper_enabled"])
    cfg["no_finbert"] = _coerce_bool(payload.get("no_finbert"), cfg["no_finbert"])
    cfg["no_budget_gate"] = _coerce_bool(payload.get("no_budget_gate"), cfg["no_budget_gate"])
    if cfg["decision_engine"] not in {"llm", "rules"}:
        cfg["decision_engine"] = "llm"
    if cfg["paper_broker"] not in {"local", "ibkr"}:
        cfg["paper_broker"] = "local"
    return cfg


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

    cmd.extend(["--decision-engine", str(cfg["decision_engine"])])

    if cfg["no_budget_gate"]:
        cmd.append("--no-budget-gate")

    cmd.extend(["--capital", str(cfg["capital"])])

    if cfg["paper_enabled"]:
        cmd.append("--paper")
    if cfg["paper_state"]:
        cmd.extend(["--paper-state", cfg["paper_state"]])
    if cfg["paper_webhook"]:
        cmd.extend(["--paper-webhook", cfg["paper_webhook"]])
    cmd.extend(["--paper-broker", str(cfg["paper_broker"])])
    cmd.extend(["--ibkr-host", str(cfg["ibkr_host"])])
    cmd.extend(["--ibkr-port", str(cfg["ibkr_port"])])
    cmd.extend(["--ibkr-client-id", str(cfg["ibkr_client_id"])])
    if cfg["ibkr_account"]:
        cmd.extend(["--ibkr-account", str(cfg["ibkr_account"])])
    cmd.extend(["--ibkr-exchange", str(cfg["ibkr_exchange"])])
    cmd.extend(["--ibkr-currency", str(cfg["ibkr_currency"])])
    if cfg["no_finbert"]:
        cmd.append("--no-finbert")

    return cmd, cfg


def _stream_process_output(proc: subprocess.Popen[str], mode: str):
    try:
        if proc.stdout is not None:
            for line in proc.stdout:
                _append_log(line)
    finally:
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


def start_process(mode: str, overrides: dict[str, Any] | None = None) -> tuple[bool, str, list[str] | None]:
    cmd, cfg = build_engine_command(mode, overrides)

    with _STATE_LOCK:
        current = _RUN_STATE.get("process")
        if current is not None and current.poll() is None:
            return False, "Un processus est deja en cours. Arrete-le avant d'en lancer un autre.", None

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(BASE_DIR),
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
        return True, "Processus arrete."
    except Exception:
        try:
            proc.kill()
            _append_log("Process killed after terminate timeout.")
            return True, "Processus force a s'arreter."
        except Exception as e:
            return False, f"Impossible d'arreter le processus: {e}"


def get_runtime_status() -> dict[str, Any]:
    with _STATE_LOCK:
        proc = _RUN_STATE.get("process")
        running = proc is not None and proc.poll() is None
        started_epoch = _RUN_STATE.get("started_epoch")
        uptime_s = int(time.time() - started_epoch) if running and started_epoch else 0
        return {
            "running": running,
            "mode": _RUN_STATE.get("mode"),
            "pid": proc.pid if running else None,
            "command": _RUN_STATE.get("command"),
            "started_at": _RUN_STATE.get("started_at"),
            "uptime_s": uptime_s,
            "last_exit_code": _RUN_STATE.get("last_exit_code"),
            "last_finished_at": _RUN_STATE.get("last_finished_at"),
        }


def list_output_files(limit: int = 40) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    allowed = {".csv", ".json", ".md", ".png"}
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
    tail = _coerce_int(request.args.get("tail", 250), 250, minimum=10)
    lines = list(_LOGS)[-tail:]
    return jsonify({"lines": lines, "count": len(lines)})


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
