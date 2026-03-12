import argparse
import hmac
import os
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

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


def get_default_config() -> dict[str, Any]:
    return {
        "universe_file": os.getenv("BUBO_UNIVERSE_FILE", "data/universe_global_v1.txt"),
        "preselect_top": _coerce_int(os.getenv("BUBO_PRESELECT_TOP", "60"), 60, minimum=1),
        "max_deep": _coerce_int(os.getenv("BUBO_MAX_DEEP", "20"), 20, minimum=1),
        "capital": _coerce_float(os.getenv("BUBO_CAPITAL", "10000"), 10000.0, minimum=1.0),
        "paper_enabled": _env_bool("BUBO_PAPER_ENABLED", True),
        "paper_state": os.getenv("BUBO_PAPER_STATE", "data/paper_portfolio_state.json"),
        "paper_webhook": os.getenv("BUBO_PAPER_WEBHOOK", ""),
        "no_finbert": _env_bool("BUBO_NO_FINBERT", True),
        "no_budget_gate": _env_bool("BUBO_NO_BUDGET_GATE", False),
    }


def _sanitize_config(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = get_default_config()
    payload = overrides or {}

    if "universe_file" in payload:
        cfg["universe_file"] = str(payload.get("universe_file") or "").strip()
    if "paper_state" in payload:
        cfg["paper_state"] = str(payload.get("paper_state") or "").strip()
    if "paper_webhook" in payload:
        cfg["paper_webhook"] = str(payload.get("paper_webhook") or "").strip()

    cfg["preselect_top"] = _coerce_int(payload.get("preselect_top", cfg["preselect_top"]), cfg["preselect_top"], minimum=1)
    cfg["max_deep"] = _coerce_int(payload.get("max_deep", cfg["max_deep"]), cfg["max_deep"], minimum=1)
    cfg["capital"] = _coerce_float(payload.get("capital", cfg["capital"]), cfg["capital"], minimum=1.0)
    cfg["paper_enabled"] = _coerce_bool(payload.get("paper_enabled"), cfg["paper_enabled"])
    cfg["no_finbert"] = _coerce_bool(payload.get("no_finbert"), cfg["no_finbert"])
    cfg["no_budget_gate"] = _coerce_bool(payload.get("no_budget_gate"), cfg["no_budget_gate"])
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

    if cfg["no_budget_gate"]:
        cmd.append("--no-budget-gate")

    cmd.extend(["--capital", str(cfg["capital"])])

    if cfg["paper_enabled"]:
        cmd.append("--paper")
    if cfg["paper_state"]:
        cmd.extend(["--paper-state", cfg["paper_state"]])
    if cfg["paper_webhook"]:
        cmd.extend(["--paper-webhook", cfg["paper_webhook"]])
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
