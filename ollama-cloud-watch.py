#!/usr/bin/env python3
"""
ollama-cloud-watch.py — Standalone Ollama Cloud usage monitor.

Works with or without Hermes Agent. All you need is a cookie file.

USAGE
  # Print current usage once and exit
  python ollama-cloud-watch.py

  # Poll every 30 minutes, record history, fire threshold alerts
  python ollama-cloud-watch.py --watch

  # Record a history snapshot and exit (good for cron)
  python ollama-cloud-watch.py --history

  # Print threshold alert if crossed, else stay silent (cron watchdog)
  python ollama-cloud-watch.py --alert

  # Generate a full stats MD report from recorded history
  python ollama-cloud-watch.py --report

  # Open the report in your default app
  python ollama-cloud-watch.py --report --open

COOKIE
  The script reads your Ollama Cloud session cookie from either:
    1. macOS Keychain:  security add-generic-password -s ollama-cloud-watch -a ollama -w '<cookie>' -U
    2. Or a plain file:  echo '__Secure-session=<value>' > ~/.ollama-cloud-cookie.txt
  Set OLLAMA_COOKIE_SOURCE=auto|keychain|file (default: auto).

  Get the cookie from your browser: ollama.com/settings (logged in) →
  DevTools → Application → Cookies → __Secure-session.

HISTORY
  Snapshots are written to ~/.ollama-cloud-history.jsonl (one per ISO week,
  deduped — keeps the latest). Also records 5h session snapshots to
  ~/.ollama-cloud-sessions.jsonl.

OPTIONS
  --watch         Poll every N seconds (default 1800 = 30min), record history + alerts
  --interval N    Poll interval in seconds (use with --watch)
  --history       Record one snapshot and exit
  --alert         Print alert line if threshold crossed, else silent
  --warn PCT      Warning threshold (default 75)
  --crit PCT      Critical threshold (default 90)
  --report        Generate MD report from history, print path
  --open          Open the report in default app (use with --report)
  --html          Generate standalone HTML dashboard file, print path
  --serve         Start HTTP server with live dashboard at http://localhost:PORT
  --api           Start HTTP server serving JSON REST endpoints only
  --port PORT     HTTP server port (default 8642 for --serve, 8643 for --api)
  --cookie PATH   Custom cookie file path
  --json          Output raw JSON instead of formatted text
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

SETTINGS_URL = "https://ollama.com/settings"
TIMEOUT = 15
DEFAULT_INTERVAL = 1800  # 30 minutes
WARN_PCT = 75.0
CRIT_PCT = 90.0

# Storage paths — use ~/.ollama-cloud-* so it works without Hermes
HISTORY_FILE = Path.home() / ".ollama-cloud-history.jsonl"
SESSION_FILE = Path.home() / ".ollama-cloud-sessions.jsonl"
REPORT_FILE = Path.home() / ".ollama-cloud-report.md"
STATE_FILE = Path.home() / ".ollama-cloud-alert-state.json"
DEFAULT_COOKIE_FILE = Path.home() / ".ollama-cloud-cookie.txt"
KEYCHAIN_SERVICE = "ollama-cloud-watch"
KEYCHAIN_ACCOUNT = "ollama"
HISTORY_MAX_WEEKS = 52  # keep a full year
SESSION_LOG_CAP = 1000


# ── cookie ──────────────────────────────────────────────────────────────────

def _cookie_source() -> str:
    return os.environ.get("OLLAMA_COOKIE_SOURCE", "auto").strip().lower()


def _keychain_cookie() -> str | None:
    if _cookie_source() == "file":
        return None
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE,
             "-a", KEYCHAIN_ACCOUNT, "-w"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass
    return None


def _load_cookie(cookie_path: Path) -> str:
    source = _cookie_source()
    if source in ("auto", "keychain"):
        kc = _keychain_cookie()
        if kc:
            return kc
        if source == "keychain":
            raise FileNotFoundError(
                f"No cookie in macOS Keychain ({KEYCHAIN_SERVICE}/{KEYCHAIN_ACCOUNT})."
            )
    if source in ("auto", "file"):
        if not cookie_path.exists():
            raise FileNotFoundError(
                f"Cookie file not found at {cookie_path}\n"
                f"Run: echo '__Secure-session=<value>' > {cookie_path}"
            )
        cookie = cookie_path.read_text().strip()
        if not cookie:
            raise ValueError("Cookie file is empty")
        return cookie
    raise ValueError(f"Unknown OLLAMA_COOKIE_SOURCE: {source}")


# ── fetch + parse ───────────────────────────────────────────────────────────

def _fetch_settings(cookie: str) -> str:
    req = urllib.request.Request(
        SETTINGS_URL,
        headers={
            "Cookie": cookie,
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:136.0) Gecko/20100101 Firefox/136.0",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _relative_reset(iso_str: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        total_s = int((dt - datetime.now(timezone.utc)).total_seconds())
        if total_s <= 0:
            return "now"
        minutes = total_s // 60
        if minutes < 60:
            return f"{minutes} min"
        hours = minutes // 60
        if hours < 24:
            return f"{hours}h"
        days = hours // 24
        return f"{days} day" if days == 1 else f"{days} days"
    except (ValueError, TypeError):
        return iso_str


def _parse_usage(html: str) -> dict:
    result = {
        "plan": None,
        "session_used_pct": None,
        "session_reset": None,
        "session_reset_iso": None,
        "weekly_used_pct": None,
        "weekly_reset": None,
        "weekly_reset_iso": None,
        "session_models": [],
        "weekly_models": [],
    }

    plan_match = re.search(
        r'Cloud usage</span>\s*\n?\s*<span[^>]*>\s*(pro|free|max)\s*<',
        html, re.IGNORECASE
    )
    if plan_match:
        result["plan"] = plan_match.group(1).capitalize()
    else:
        fallback = re.search(r'(Pro|Free|Max)[^<]*Cloud usage', html, re.IGNORECASE)
        if fallback:
            result["plan"] = fallback.group(1).capitalize()
        else:
            plans = re.findall(r'\b(Pro|Free|Max)\b', html)
            if plans:
                result["plan"] = max(set(plans), key=plans.count)

    session_pct = re.search(r'Session usage\s+([0-9.]+)%', html)
    if session_pct:
        result["session_used_pct"] = float(session_pct.group(1))

    weekly_pct = re.search(r'Weekly usage\s+([0-9.]+)%', html)
    if weekly_pct:
        result["weekly_used_pct"] = float(weekly_pct.group(1))

    resets = re.findall(r'data-time="([^"]*)"', html)
    if len(resets) >= 1:
        result["session_reset"] = _relative_reset(resets[0])
        result["session_reset_iso"] = resets[0]
    if len(resets) >= 2:
        result["weekly_reset"] = _relative_reset(resets[1])
        result["weekly_reset_iso"] = resets[1]

    weekly_pos = html.find("Weekly usage")
    for m in re.finditer(r'<button[^>]*data-usage-segment[^>]*>', html):
        tag = m.group(0)
        model_m = re.search(r'data-model="([^"]+)"', tag)
        req_m = re.search(r'data-requests="(\d+)"', tag)
        width_m = re.search(r'width:\s*([0-9.]+)%', tag)
        if not model_m or not req_m:
            continue
        seg = {
            "model": model_m.group(1),
            "requests": int(req_m.group(1)),
            "share_pct": float(width_m.group(1)) if width_m else None,
        }
        if weekly_pos != -1 and m.start() > weekly_pos:
            result["weekly_models"].append(seg)
        else:
            result["session_models"].append(seg)

    # Cost estimates
    weekly_budgets = {"pro": 20.0 / 4.33, "max": 100.0 / 4.33, "free": 0.0}
    weekly_budget = weekly_budgets.get((result.get("plan") or "").lower(), 20.0 / 4.33)
    weekly_used = result.get("weekly_used_pct") or 0.0
    cost_consumed = weekly_budget * (weekly_used / 100.0)

    for seg in result["weekly_models"]:
        share = seg.get("share_pct") or 0.0
        seg["est_cost"] = round(cost_consumed * (share / 100.0), 4)
        if seg["requests"] > 0:
            seg["est_cost_per_req"] = round(seg["est_cost"] / seg["requests"], 4)
            seg["est_cost_per_req_pct"] = round(seg["est_cost_per_req"] / weekly_budget * 100.0, 4)
        else:
            seg["est_cost_per_req"] = 0.0
            seg["est_cost_per_req_pct"] = 0.0

    result["est_weekly_budget"] = round(weekly_budget, 2)
    result["est_cost_consumed"] = round(cost_consumed, 2)
    total_reqs = sum(s["requests"] for s in result["weekly_models"])
    result["est_avg_cost_per_req"] = round(cost_consumed / total_reqs, 4) if total_reqs else 0.0

    # ── API equivalent cost comparison (mirrors the Hermes plugin) ────────
    # Official API list prices per 1M tokens (in/out/cache-hit), USD, Aug 2026.
    API_PRICES = {
        "glm-5.2": (1.40, 4.40, 0.26),
        "glm-5.2:cloud": (1.40, 4.40, 0.26),
        "glm-5": (1.40, 4.40, 0.26),
        "deepseek-v4-flash:0731": (0.14, 0.28, 0.0028),
        "deepseek-v4-flash": (0.14, 0.28, 0.0028),
        "deepseek-v4-pro": (1.74, 3.48, 0.0036),
        "minimax-m3": (0.24, 0.96, 0.06),
        "gemma4:31b": (0.30, 0.90, 0.30),
        "kimi-k2.7-code": (0.95, 4.00, 0.19),
        "kimi-k2.6": (0.95, 4.00, 0.16),
        "gpt-5.5": (1.25, 10.00, 1.25),
    }

    token_avgs = _real_token_averages()

    fallback_avg = None
    if token_avgs:
        vals = list(token_avgs.values())
        fallback_avg = (
            round(sum(v[0] for v in vals) / len(vals)),
            round(sum(v[1] for v in vals) / len(vals)),
            round(sum(v[2] for v in vals) / len(vals)),
        )

    api_weekly_total = 0.0
    for seg in result["weekly_models"]:
        prices = API_PRICES.get(seg["model"])
        if prices:
            p_in, p_out, p_cache = prices
            avg = token_avgs.get(seg["model"]) or fallback_avg
            if avg:
                in_t, out_t, cache_t = avg
                miss_in = max(in_t - cache_t, 0)
                per_req = (miss_in / 1e6) * p_in + (cache_t / 1e6) * p_cache + (out_t / 1e6) * p_out
            else:
                per_req = (1000 / 1e6) * p_in + (500 / 1e6) * p_out
                in_t, out_t, cache_t, miss_in = 1000, 500, 0, 1000
            seg["api_cost_per_req"] = round(per_req, 6)
            seg["api_weekly_cost"] = round(per_req * seg["requests"], 4)
            api_weekly_total += seg["api_weekly_cost"]
            if per_req > 0:
                seg["api_cost_pct"] = round(seg["est_cost_per_req"] / per_req * 100.0, 1)
            else:
                seg["api_cost_pct"] = None
            seg["avg_in_tokens"] = in_t
            seg["avg_cache_tokens"] = cache_t
            seg["avg_out_tokens"] = out_t
            seg["cache_hit_pct"] = round(cache_t / in_t * 100.0, 1) if in_t > 0 else None
            seg["total_in_tokens"] = round(in_t * seg["requests"])
            seg["total_out_tokens"] = round(out_t * seg["requests"])
            seg["api_input_cost"] = round((miss_in / 1e6) * p_in * seg["requests"], 4)
            seg["api_cache_cost"] = round((cache_t / 1e6) * p_cache * seg["requests"], 4)
            seg["api_output_cost"] = round((out_t / 1e6) * p_out * seg["requests"], 4)
        else:
            seg["api_cost_per_req"] = None
            seg["api_weekly_cost"] = None
            seg["api_cost_pct"] = None
            seg["cache_hit_pct"] = None
            seg["total_in_tokens"] = None
            seg["total_out_tokens"] = None

    result["api_weekly_total"] = round(api_weekly_total, 4)
    result["api_assumption"] = _api_assumption_text(token_avgs)
    result["api_total_pct"] = (
        round(cost_consumed / api_weekly_total * 100.0, 1) if api_weekly_total > 0 else None
    )
    result["api_savings"] = round(api_weekly_total - cost_consumed, 2) if api_weekly_total > 0 else None
    result["api_monthly_proj"] = round(api_weekly_total * 4.33, 2) if api_weekly_total > 0 else None
    result["ollama_monthly"] = 20.0

    return result


def _real_token_averages() -> dict:
    """Real per-model avg tokens/request from Hermes state.db (all-time).

    Returns {model: (avg_input, avg_output, avg_cache_read)}. Empty dict when
    the DB is unavailable (standalone user without Hermes) — callers fall back.
    """
    try:
        import sqlite3
        STATE_DB = Path.home() / ".hermes" / "state.db"
        if not STATE_DB.exists():
            return {}
        conn = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True, timeout=5)
        try:
            rows = conn.execute(
                """
                SELECT model,
                       ROUND(SUM(input_tokens)*1.0/SUM(api_call_count), 0),
                       ROUND(SUM(output_tokens)*1.0/SUM(api_call_count), 0),
                       ROUND(SUM(cache_read_tokens)*1.0/SUM(api_call_count), 0)
                FROM sessions
                WHERE billing_provider = 'ollama-cloud'
                  AND api_call_count > 0
                  AND input_tokens > 0
                GROUP BY model
                """
            ).fetchall()
            return {r[0]: (r[1], r[2], r[3]) for r in rows}
        finally:
            conn.close()
    except Exception:
        return {}


def _api_assumption_text(token_avgs: dict) -> str:
    if not token_avgs:
        return "~1000 in + 500 out tokens/req (fallback — no Hermes state.db data)"
    n = len(token_avgs)
    return f"real token averages from Hermes state.db (all-time, {n} models), cache-aware pricing"


def _fetch_usage(cookie_path: Path) -> dict:
    cookie = _load_cookie(cookie_path)
    html = _fetch_settings(cookie)
    data = _parse_usage(html)
    data["ok"] = True
    data["fetched_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return data


# ── history ─────────────────────────────────────────────────────────────────

def _week_key(iso_str: str | None) -> str | None:
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        monday = dt - timedelta(days=dt.weekday())
        return monday.date().isoformat()
    except (ValueError, TypeError):
        return None


def _record_history(data: dict, history_file: Path) -> None:
    if not data.get("ok") or data.get("weekly_used_pct") is None:
        return
    week = _week_key(data.get("weekly_reset_iso"))
    if not week:
        return
    try:
        history_file.parent.mkdir(parents=True, exist_ok=True)
        kept = []
        if history_file.exists():
            for line in history_file.read_text().splitlines():
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("week") != week:
                    kept.append(line)
        record = {
            "week": week,
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "weekly_used_pct": data.get("weekly_used_pct"),
            "session_used_pct": data.get("session_used_pct"),
            "est_cost_consumed": data.get("est_cost_consumed"),
            "est_weekly_budget": data.get("est_weekly_budget"),
            "models": [
                {"model": m.get("model"), "requests": m.get("requests"),
                 "share_pct": m.get("share_pct"),
                 "est_cost_per_req": m.get("est_cost_per_req"),
                 "est_cost_per_req_pct": m.get("est_cost_per_req_pct")}
                for m in (data.get("weekly_models") or [])
            ],
        }
        kept.append(json.dumps(record))
        history_file.write_text("\n".join(kept) + "\n")
    except OSError as e:
        print(f"⚠️  Could not write history: {e}", file=sys.stderr)


def _record_session(data: dict, session_file: Path) -> None:
    if not data.get("ok") or data.get("session_used_pct") is None:
        return
    win = data.get("session_reset_iso")
    if not win:
        return
    try:
        session_file.parent.mkdir(parents=True, exist_ok=True)
        kept = []
        if session_file.exists():
            for line in session_file.read_text().splitlines():
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("window") != win:
                    kept.append(line)
        record = {
            "window": win,
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "session_used_pct": data.get("session_used_pct"),
            "weekly_used_pct": data.get("weekly_used_pct"),
            "session_models": [
                {"model": m.get("model"), "requests": m.get("requests"),
                 "share_pct": m.get("share_pct")}
                for m in (data.get("session_models") or [])
            ],
            "weekly_models": [
                {"model": m.get("model"), "requests": m.get("requests"),
                 "share_pct": m.get("share_pct")}
                for m in (data.get("weekly_models") or [])
            ],
        }
        kept.append(json.dumps(record))
        session_file.write_text("\n".join(kept) + "\n")
        lines = session_file.read_text().splitlines()
        if len(lines) > SESSION_LOG_CAP:
            session_file.write_text("\n".join(lines[-SESSION_LOG_CAP:]) + "\n")
    except OSError as e:
        print(f"⚠️  Could not write session log: {e}", file=sys.stderr)


def _load_history(history_file: Path) -> list:
    if not history_file.exists():
        return []
    weeks = []
    for line in history_file.read_text().splitlines():
        try:
            weeks.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return weeks


# ── alert ────────────────────────────────────────────────────────────────────

def _notify(title: str, message: str) -> None:
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{message}" with title "{title}"'],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass  # non-macOS — stdout line still delivers


def _alert_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(st: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(st))
    except OSError:
        pass


def check_alert(data: dict, warn: float, crit: float) -> str | None:
    """Return alert line if threshold just crossed, else None (silent)."""
    weekly = data.get("weekly_used_pct")
    if weekly is None:
        return None

    now = datetime.now(timezone.utc)
    monday = (now - timedelta(days=now.weekday())).date()
    week = monday.isoformat()

    st = _alert_state()
    fired_crit = st.get("crit") == week
    fired_warn = st.get("warn") == week

    if weekly >= crit and not fired_crit:
        _notify("Ollama Cloud CRITICAL", f"Weekly usage at {weekly:.1f}%!")
        st["crit"] = week
        _save_state(st)
        return f"⚠️  Ollama Cloud: weekly {weekly:.1f}% — CRITICAL (>{crit:.0f}%)"
    if weekly >= warn and not fired_warn:
        _notify("Ollama Cloud warning", f"Weekly usage at {weekly:.1f}%")
        st["warn"] = week
        _save_state(st)
        return f"⚠️  Ollama Cloud: weekly {weekly:.1f}% — warning (>{warn:.0f}%)"
    return None


# ── report ──────────────────────────────────────────────────────────────────

def generate_report(history_file: Path, report_file: Path) -> str:
    weeks = _load_history(history_file)
    weeks.sort(key=lambda w: w.get("week") or "")

    sessions = []
    if session_file_exists():
        for line in SESSION_FILE.read_text().splitlines():
            try:
                sessions.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    sessions.sort(key=lambda s: s.get("ts") or "")

    lines = [
        "# Ollama Cloud Usage Stats",
        "",
        f"_Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_",
        "",
        "## Weekly overview",
        "",
    ]
    if weeks:
        lines.append("| Week | Weekly used | Est. cost | Top model | Requests |")
        lines.append("|------|-------------|-----------|-----------|----------|")
        for w in weeks:
            models = w.get("models") or []
            top = max(models, key=lambda m: m.get("share_pct") or 0) if models else {}
            total = sum(m.get("requests") or 0 for m in models)
            cost = w.get("est_cost_consumed")
            cost_s = f"${cost:.2f}" if cost is not None else "?"
            lines.append(
                f"| {w.get('week')} | {w.get('weekly_used_pct')}% | {cost_s} | "
                f"{top.get('model') or '-'} | {total} |"
            )
    else:
        lines.append("_No weekly snapshots yet._")
    lines.append("")

    lines.append("## All 5h sessions")
    lines.append("")
    if sessions:
        for s in reversed(sessions[-50:]):
            ts = s.get("ts", "")[:16].replace("T", " ")
            lines.append(f"### {ts} — session {s.get('session_used_pct')}% · weekly {s.get('weekly_used_pct')}%")
            lines.append("")
            for label, key in [("Session", "session_models"), ("Weekly", "weekly_models")]:
                mods = s.get(key) or []
                if mods:
                    lines.append(f"{label} per model:")
                    lines.append("")
                    lines.append("| Model | Requests | Share |")
                    lines.append("|-------|----------|-------|")
                    for m in mods:
                        lines.append(f"| {m.get('model')} | {m.get('requests')} | {m.get('share_pct')}% |")
                    lines.append("")
    else:
        lines.append("_No session snapshots yet._")
        lines.append("")

    report_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.write_text("\n".join(lines))
    return str(report_file)


def session_file_exists() -> bool:
    return SESSION_FILE.exists()


# ── output ──────────────────────────────────────────────────────────────────

def print_usage(data: dict, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(data, indent=2, default=str))
        return

    if not data.get("ok"):
        print(f"❌ Error: {data.get('error', 'unknown')}")
        return

    plan = data.get("plan") or "Unknown"
    s = data.get("session_used_pct")
    w = data.get("weekly_used_pct")
    print(f"📊 Ollama Cloud — {plan} plan")
    print(f"   Session:  {s:.1f}% used · resets in {data.get('session_reset', '?')}" if s is not None else "   Session:  n/a")
    print(f"   Weekly:   {w:.1f}% used · resets in {data.get('weekly_reset', '?')}" if w is not None else "   Weekly:   n/a")
    print(f"   Est. cost consumed: ${data.get('est_cost_consumed', 0):.2f} / ${data.get('est_weekly_budget', 0):.2f}/wk")
    print()

    sm = data.get("session_models") or []
    wm = data.get("weekly_models") or []
    if sm:
        print("   Session per model:")
        for m in sm:
            print(f"     {m['model']:<25} {m['requests']:>5} req · {m.get('share_pct', 0):.1f}%")
        print()

    if wm:
        print("   Weekly per model:")
        for m in wm:
            cr = m.get("est_cost_per_req", 0)
            print(f"     {m['model']:<25} {m['requests']:>5} req · {m.get('share_pct', 0):.1f}% · ${cr:.4f}/req")
        print(f"     Avg: ${data.get('est_avg_cost_per_req', 0):.4f}/req across all models")


# ── HTML dashboard ──────────────────────────────────────────────────────────

def _pct_color(p):
    if p is None:
        return "var(--quaternary)"
    if p >= 90:
        return "#ef4444"
    if p >= 75:
        return "#f59e0b"
    return "var(--secondary)"


def _fmt_tokens(n):
    if n is None:
        return "?"
    if n >= 1e6:
        return f"{n/1e6:.1f}M"
    if n >= 1e3:
        return f"{n/1e3:.0f}K"
    return str(int(n))


def _lifetime_from_history(weeks: list) -> dict:
    """Aggregate lifetime stats from history records (same as Hermes plugin)."""
    if not weeks:
        return {"ok": True, "weeks_count": 0, "models": []}
    model_reqs: dict = {}
    model_cost_wsum: dict = {}
    for rec in weeks:
        for m in rec.get("models") or []:
            model = m.get("model")
            reqs = m.get("requests") or 0
            cpr = m.get("est_cost_per_req")
            if not model or reqs <= 0:
                continue
            model_reqs[model] = model_reqs.get(model, 0) + reqs
            if cpr is not None:
                model_cost_wsum[model] = model_cost_wsum.get(model, 0.0) + cpr * reqs
    total_reqs = sum(model_reqs.values())
    total_cost = sum(model_cost_wsum.values())
    weekly_budget = 20.0 / 4.33
    models = []
    for model in model_reqs:
        reqs = model_reqs[model]
        cpr = model_cost_wsum.get(model, 0.0) / reqs if reqs else 0.0
        models.append({
            "model": model, "requests": reqs,
            "est_cost_per_req": round(cpr, 4),
            "est_cost_per_req_pct": round(cpr / weekly_budget * 100.0, 4),
            "est_cost": round(model_cost_wsum.get(model, 0.0), 4),
        })
    models.sort(key=lambda m: m.get("requests") or 0, reverse=True)
    return {
        "ok": True, "weeks_count": len(weeks),
        "total_requests": total_reqs,
        "est_total_cost": round(total_cost, 4),
        "est_avg_cost_per_req": round(total_cost / total_reqs, 4) if total_reqs else 0.0,
        "est_avg_cost_per_req_pct": round((total_cost / total_reqs) / weekly_budget * 100.0, 4) if total_reqs else 0.0,
        "models": models,
    }


def _generate_html(data: dict, history: list, sessions: list) -> str:
    """Self-contained HTML dashboard — mirrors the Hermes pane (all sections,
    collapsible via native <details>/<summary> — zero JS required)."""
    plan = data.get("plan") or "Unknown"
    s = data.get("session_used_pct")
    w = data.get("weekly_used_pct")
    s_reset = data.get("session_reset") or "?"
    w_reset = data.get("weekly_reset") or "?"
    budget = data.get("est_weekly_budget", 0)
    consumed = data.get("est_cost_consumed", 0)
    avg_req = data.get("est_avg_cost_per_req", 0)

    sm = data.get("session_models") or []
    wm = data.get("weekly_models") or []
    lt = _lifetime_from_history(history)

    api_total = data.get("api_weekly_total")
    savings = data.get("api_savings")
    api_monthly = data.get("api_monthly_proj")
    ollama_monthly = data.get("ollama_monthly", 20.0)
    api_pct = data.get("api_total_pct")
    assumption = data.get("api_assumption") or ""

    def model_rows(models, with_cost=False, with_cache=False, with_tokens=False):
        if not models:
            return "<tr><td colspan='6' class='muted'>No data</td></tr>"
        rows = []
        for m in models:
            reqs = m.get("requests", 0)
            share = m.get("share_pct", 0) or 0
            cr = m.get("est_cost_per_req", 0)
            extra = f"<td class='num'>${cr:.4f}/req</td>" if with_cost else ""
            cache = ""
            if with_cache and m.get("cache_hit_pct") is not None:
                cache = f"<td class='num'>{m['cache_hit_pct']:.0f}%</td>"
            toks = ""
            if with_tokens and m.get("total_in_tokens") is not None:
                toks = f"<td class='num'>{_fmt_tokens(m['total_in_tokens'])} · {_fmt_tokens(m['total_out_tokens'])}</td>"
            rows.append(
                f"<tr><td>{m['model']}</td><td class='num'>{reqs}</td><td class='num'>{share:.1f}%</td>"
                f"{extra}{cache}{toks}</tr>"
            )
        return "\n".join(rows)

    def history_rows(weeks):
        if not weeks:
            return "<tr><td colspan='5' class='muted'>No history yet</td></tr>"
        rows = []
        for wk in weeks[-8:]:
            w_pct = wk.get("weekly_used_pct", 0) or 0
            cost = wk.get("est_cost_consumed")
            models = wk.get("models") or []
            top = max(models, key=lambda m: m.get("share_pct") or 0).get("model") if models else "-"
            total = sum(m.get("requests") or 0 for m in models)
            cost_s = f"${cost:.2f}" if cost is not None else "?"
            rows.append(f"<tr><td>{wk.get('week')}</td><td class='num'>{w_pct:.1f}%</td><td class='num'>{cost_s}</td><td>{top}</td><td class='num'>{total}</td></tr>")
        return "\n".join(rows)

    def session_blocks(sess):
        if not sess:
            return "<p class='muted'>No session snapshots yet — recorded once per 5h window.</p>"
        out = []
        for sn in reversed(sess[-20:]):
            ts = sn.get("ts", "")[:16].replace("T", " ")
            out.append(f"<h3>{ts} — session {sn.get('session_used_pct')}% · weekly {sn.get('weekly_used_pct')}%</h3>")
            for label, key in [("Session", "session_models"), ("Weekly", "weekly_models")]:
                mods = sn.get(key) or []
                if mods:
                    out.append(f"<table><thead><tr><th>Model</th><th class='num'>Requests</th><th class='num'>Share</th></tr></thead><tbody>")
                    for m in mods:
                        out.append(f"<tr><td>{m.get('model')}</td><td class='num'>{m.get('requests')}</td><td class='num'>{m.get('share_pct')}%</td></tr>")
                    out.append("</tbody></table>")
        return "\n".join(out)

    # Collapsible helpers (native HTML, no JS)
    def collapsible(title, body, open_=False):
        return (
            f"<details{' open' if open_ else ''} class='section'><summary>{title}</summary>"
            f"{body}</details>"
        )

    # ── savings headline ──
    savings_html = ""
    if savings is not None and savings > 0:
        savings_html = (
            "<div class='savings'>"
            f"<div class='savings-big'>💰 You saved <b>${savings:.0f}</b> this week</div>"
            f"<div class='savings-sub'>API ${api_total:.0f} · Ollama est. ${consumed:.2f}</div>"
            "</div>"
        )

    # ── limits ──
    limits_html = collapsible(
        "Limits",
        f"<div class='limit-row'><span>Session <span class='dim'>· resets in {s_reset}</span></span>"
        f"<span class='num' style='color:{_pct_color(s)}'>{s:.1f}% used</span></div>"
        f"<div class='limit-row'><span>Weekly <span class='dim'>· resets in {w_reset}</span></span>"
        f"<span class='num' style='color:{_pct_color(w)}'>{w:.1f}% used</span></div>",
        open_=True,
    )

    # ── session / weekly per model ──
    session_table = (
        "<table><thead><tr><th>Model</th><th class='num'>Requests</th><th class='num'>Share</th></tr></thead>"
        f"<tbody>{model_rows(sm)}</tbody></table>"
    )
    weekly_table = (
        "<table><thead><tr><th>Model</th><th class='num'>Requests</th><th class='num'>Share</th>"
        "<th class='num'>Cost</th></tr></thead>"
        f"<tbody>{model_rows(wm, with_cost=True)}</tbody></table>"
        f"<div class='cost'>Budget: ${budget:.2f}/wk · consumed: ${consumed:.2f} · avg ${avg_req:.4f}/req</div>"
    )

    # ── avg cost this week ──
    cost_week_rows = "".join(
        f"<div class='limit-row'><span>{m['model']}</span><span class='num'>${m.get('est_cost_per_req',0):.4f} · {m.get('est_cost_per_req_pct',0):.3f}%</span></div>"
        for m in wm
    ) or "<p class='muted'>No data</p>"
    cost_week_html = collapsible("Avg cost per request (this week)", cost_week_rows)

    # ── avg cost lifetime ──
    lt_rows = "".join(
        f"<div class='limit-row'><span>{m['model']}</span><span class='num'>${m['est_cost_per_req']:.4f} · {m['est_cost_per_req_pct']:.3f}%</span></div>"
        for m in (lt.get("models") or [])
    ) or "<p class='muted'>No data yet</p>"
    lt_summary = (
        f"<div class='cost'>{lt.get('weeks_count',0)} weeks · {lt.get('total_requests',0)} requests · "
        f"total ${lt.get('est_total_cost',0):.2f} · avg ${lt.get('est_avg_cost_per_req',0):.4f}/req</div>"
    )
    cost_lifetime_html = collapsible("Avg cost per request (lifetime)", lt_rows + lt_summary)

    # ── cache hit % ──
    cache_known = [m for m in wm if m.get("cache_hit_pct") is not None]
    if cache_known:
        cache_rows = "".join(
            f"<div class='limit-row'><span>{m['model']}</span>"
            f"<span class='num'>{m['cache_hit_pct']:.0f}% · {_fmt_tokens(m.get('avg_in_tokens'))} in · {_fmt_tokens(m.get('avg_cache_tokens'))} cached</span></div>"
            for m in cache_known
        )
        cache_html = collapsible(
            "Cache hit % per model",
            cache_rows + "<div class='cost'>Higher cache = cheaper on raw APIs (cached input billed at 80-98% discount)</div>",
        )
    else:
        cache_html = ""

    # ── token volume ──
    tok_known = [m for m in wm if m.get("total_in_tokens") is not None]
    if tok_known:
        tok_rows = "".join(
            f"<div class='limit-row'><span>{m['model']}</span>"
            f"<span class='num'>{_fmt_tokens(m.get('total_in_tokens'))} in · {_fmt_tokens(m.get('total_out_tokens'))} out</span></div>"
            for m in tok_known
        )
        tok_html = collapsible(
            "Token volume per model",
            tok_rows + "<div class='cost'>Total tokens processed per model this week (avg tokens per request × requests)</div>",
        )
    else:
        tok_html = ""

    # ── API equivalent cost ──
    if api_total is not None:
        api_known = [m for m in wm if m.get("api_cost_per_req") is not None]
        api_rows = "".join(
            f"<div class='limit-row'><span>{m['model']}</span>"
            f"<span class='num'>${m.get('api_weekly_cost',0):.2f}/wk · ${m.get('api_cost_per_req',0):.4f}/req</span></div>"
            for m in api_known
        ) or "<p class='muted'>No price data</p>"
        api_total_row = (
            "<div class='limit-row total-row'><span>API total this week</span>"
            f"<span class='num'>${api_total:.2f} · Ollama ${consumed:.2f}</span></div>"
        )
        api_html = collapsible(
            "API equivalent cost",
            "<div class='cost'>What the same usage would cost on official pay-per-token APIs</div>"
            + api_rows + api_total_row + f"<div class='cost'>Assumes {assumption}</div>",
        )
    else:
        api_html = ""

    # ── cost efficiency ──
    if api_pct is not None:
        eff_known = [m for m in wm if m.get("api_cost_pct") is not None]
        eff_rows = "".join(
            f"<div class='limit-row'><span>{m['model']}</span><span class='num'>{m['api_cost_pct']:.0f}% of API</span></div>"
            for m in eff_known
        )
        eff_total = (
            "<div class='limit-row total-row'><span>Ollama overall</span>"
            f"<span class='num'>{api_pct:.0f}% of API total</span></div>"
        )
        eff_html = collapsible(
            "Cost efficiency",
            "<div class='cost'>Ollama cost as % of API price — lower = better deal</div>" + eff_rows + eff_total,
        )
    else:
        eff_html = ""

    # ── break-even ──
    if api_total is not None:
        ratio = api_total / (consumed or 1)
        break_html = collapsible(
            "Break-even comparison",
            f"<div class='limit-row'><span>API cost / week</span><span class='num'>${api_total:.2f}</span></div>"
            f"<div class='limit-row'><span>Ollama cost / week</span><span class='num'>${consumed:.2f}</span></div>"
            f"<div class='cost'>Ollama is {ratio:.0f}× cheaper than pay-per-token this week</div>",
        )
    else:
        break_html = ""

    # ── monthly projection ──
    if api_monthly is not None:
        saved_m = api_monthly - ollama_monthly
        monthly_html = collapsible(
            "Monthly projection",
            f"<div class='limit-row'><span>API projected / month</span><span class='num'>${api_monthly:.0f}</span></div>"
            f"<div class='limit-row'><span>Ollama Pro / month</span><span class='num'>${ollama_monthly:.0f}</span></div>"
            f"<div class='limit-row total-row'><span>You save / month</span><span class='num'><b>${saved_m:.0f}</b></span></div>",
        )
    else:
        monthly_html = ""

    # ── weekly history ──
    history_html = collapsible(
        "Weekly history",
        "<table><thead><tr><th>Week</th><th class='num'>Used</th><th class='num'>Cost</th><th>Top model</th>"
        "<th class='num'>Requests</th></tr></thead>"
        f"<tbody>{history_rows(history)}</tbody></table>"
        "<div class='cost'>Weekly snapshots — kept locally, survives Ollama resets</div>",
    )

    # ── sessions ──
    sessions_html = collapsible("All 5h sessions", session_blocks(sessions))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ollama Cloud Usage Stats</title>
<style>
  :root {{
    --bg: #0a0a0a; --card: #161616; --border: #2a2a2a;
    --text: #e5e5e5; --secondary: #a1a1aa; --quaternary: #52525b;
    --accent: #3b82f6; --green: #22c55e; --yellow: #f59e0b; --red: #ef4444;
  }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ background: var(--bg); color: var(--text); font: 14px/1.5 -apple-system,BlinkMacSystemFont,system-ui,sans-serif; padding: 20px; max-width: 800px; margin: 0 auto; }}
  h1 {{ font-size: 1.4rem; margin-bottom: 4px; }}
  .sub {{ color: var(--quaternary); font-size: 0.8rem; margin-bottom: 20px; }}
  .cards {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 16px; }}
  .card {{ background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }}
  .card h2 {{ font-size: 0.75rem; color: var(--secondary); margin-bottom: 8px; font-weight: 500; }}
  .big {{ font-size: 1.8rem; font-weight: 700; }}
  .reset {{ color: var(--quaternary); font-size: 0.8rem; margin-top: 4px; }}
  .bar {{ height: 6px; border-radius: 3px; background: var(--border); margin-top: 8px; overflow: hidden; }}
  .bar-fill {{ height: 100%; border-radius: 3px; transition: width 0.5s; }}
  .savings {{ background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 14px 16px; margin-bottom: 12px; }}
  .savings-big {{ font-size: 1.05rem; }}
  .savings-sub {{ color: var(--quaternary); font-size: 0.8rem; margin-top: 2px; }}
  .section {{ background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 10px 14px; margin-bottom: 10px; }}
  summary {{ font-size: 0.85rem; font-weight: 600; color: var(--secondary); cursor: pointer; padding: 4px 0; user-select: none; }}
  summary:hover {{ color: var(--text); }}
  details[open] summary {{ margin-bottom: 8px; }}
  .limit-row {{ display: flex; justify-content: space-between; align-items: center; gap: 8px; padding: 4px 0; font-size: 0.85rem; }}
  .limit-row .num {{ color: var(--secondary); font-variant-numeric: tabular-nums; }}
  .total-row {{ border-top: 1px solid var(--border); margin-top: 4px; padding-top: 6px; font-weight: 600; }}
  .dim {{ color: var(--quaternary); }}
  table {{ width: 100%; border-collapse: collapse; margin: 4px 0; }}
  th {{ text-align: left; font-size: 0.7rem; color: var(--quaternary); font-weight: 500; padding: 4px 8px; border-bottom: 1px solid var(--border); }}
  td {{ padding: 5px 8px; border-bottom: 1px solid var(--border); font-size: 0.82rem; }}
  .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .muted {{ color: var(--quaternary); text-align: center; padding: 10px; font-size: 0.8rem; }}
  .cost {{ color: var(--quaternary); font-size: 0.75rem; margin-top: 6px; }}
  h3 {{ font-size: 0.8rem; color: var(--secondary); margin: 10px 0 4px; }}
  footer {{ color: var(--quaternary); font-size: 0.7rem; text-align: center; margin-top: 20px; }}
</style>
</head>
<body>
  <h1>📊 Ollama Cloud Usage Stats</h1>
  <div class="sub">{plan} plan · generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</div>

  <div class="cards">
    <div class="card">
      <h2>Session</h2>
      <div class="big" style="color: {_pct_color(s)}">{s:.1f}%</div>
      <div class="reset">resets in {s_reset}</div>
      <div class="bar"><div class="bar-fill" style="width:{s or 0}%;background:{_pct_color(s)}"></div></div>
    </div>
    <div class="card">
      <h2>Weekly</h2>
      <div class="big" style="color: {_pct_color(w)}">{w:.1f}%</div>
      <div class="reset">resets in {w_reset}</div>
      <div class="bar"><div class="bar-fill" style="width:{w or 0}%;background:{_pct_color(w)}"></div></div>
    </div>
  </div>

  {savings_html}
  {limits_html}

  {collapsible("Session — per model", session_table, open_=True)}
  {collapsible("Weekly — per model", weekly_table, open_=True)}

  {cost_week_html}
  {cost_lifetime_html}
  {cache_html}
  {tok_html}
  {api_html}
  {eff_html}
  {break_html}
  {monthly_html}
  {history_html}
  {sessions_html}

  <footer>Ollama Cloud Usage Stats · <a href="https://github.com/Kosello/ollama-cloud-watch">github.com/Kosello/ollama-cloud-watch</a></footer>
</body>
</html>"""


# ── HTTP server (serve + api) ───────────────────────────────────────────────

def _build_api_data(cookie_path: Path) -> dict:
    """Fetch fresh usage + load history + sessions for API/dashboard."""
    data = _fetch_usage(cookie_path)
    _record_history(data, HISTORY_FILE)
    _record_session(data, SESSION_FILE)
    history = _load_history(HISTORY_FILE)
    sessions = _load_history(SESSION_FILE)  # reuse loader
    return {"usage": data, "history": history[-52:], "sessions": sessions[-50:]}


class _APIHandler:
    """Minimal request handler supporting both --serve and --api modes."""

    def __init__(self, cookie_path: Path, mode: str = "api"):
        self.cookie_path = cookie_path
        self.mode = mode
        self._cache: dict = {"ts": 0, "data": None}
        self._cache_ttl = 30  # 30s cache for HTTP requests

    def _cached_data(self) -> dict:
        now = time.time()
        if self._cache["data"] and (now - self._cache["ts"]) < self._cache_ttl:
            return self._cache["data"]
        try:
            data = _build_api_data(self.cookie_path)
            self._cache["ts"] = now
            self._cache["data"] = data
            return data
        except Exception as e:
            return {"error": str(e)}

    def handle(self, method: str, path: str) -> tuple[int, str, str]:
        """Return (status_code, content_type, body)."""
        if method != "GET":
            return 405, "text/plain", "Method not allowed"

        if self.mode == "serve" and path == "/":
            d = self._cached_data()
            html = _generate_html(d.get("usage", {}), d.get("history", []), d.get("sessions", []))
            return 200, "text/html; charset=utf-8", html

        if self.mode == "serve" and path == "/health":
            return 200, "application/json", json.dumps({"ok": True})

        # API endpoints (available in both modes)
        if path == "/api/usage":
            d = self._cached_data()
            return 200, "application/json", json.dumps(d.get("usage", d), indent=2, default=str)
        if path == "/api/history":
            d = self._cached_data()
            return 200, "application/json", json.dumps(d.get("history", []), indent=2, default=str)
        if path == "/api/sessions":
            d = self._cached_data()
            return 200, "application/json", json.dumps(d.get("sessions", []), indent=2, default=str)
        if path == "/api/lifetime":
            d = self._cached_data()
            weeks = d.get("history", [])
            lt = _lifetime_from_history(weeks)
            return 200, "application/json", json.dumps(lt, indent=2, default=str)
        if path == "/health":
            return 200, "application/json", json.dumps({"ok": True})

        if self.mode == "serve":
            return 404, "text/plain", "Not found"
        return 404, "application/json", json.dumps({"error": "not found"})


def _run_server(cookie_path: Path, mode: str, port: int) -> int:
    """Start a stdlib HTTP server (no dependencies)."""
    from http.server import HTTPServer, BaseHTTPRequestHandler

    handler_ctx = _APIHandler(cookie_path, mode)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            status, ctype, body = handler_ctx.handle(self.command, self.path)
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))

        def log_message(self, fmt, *args):
            pass  # quiet

    label = "dashboard" if mode == "serve" else "API"
    print(f"🚀 Ollama Cloud {label} server on http://localhost:{port}")
    if mode == "serve":
        print(f"   Dashboard: http://localhost:{port}/")
    print(f"   Endpoints: /api/usage  /api/history  /api/sessions  /api/lifetime  /health")
    print(f"   Ctrl+C to stop")
    try:
        server = HTTPServer(("127.0.0.1", port), Handler)
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0


def _generate_html_file(cookie_path: Path) -> str:
    """Generate a standalone HTML file from current data + history."""
    data = _fetch_usage(cookie_path)
    _record_history(data, HISTORY_FILE)
    _record_session(data, SESSION_FILE)
    history = _load_history(HISTORY_FILE)
    sessions = _load_history(SESSION_FILE)
    html_file = Path.home() / ".ollama-cloud-dashboard.html"
    html_file.write_text(_generate_html(data, history, sessions))
    return str(html_file)


# ── main ────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Standalone Ollama Cloud usage monitor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--watch", action="store_true", help="Poll continuously")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL, help="Poll interval (seconds)")
    parser.add_argument("--history", action="store_true", help="Record snapshot and exit")
    parser.add_argument("--alert", action="store_true", help="Print alert if threshold crossed, else silent")
    parser.add_argument("--warn", type=float, default=WARN_PCT, help="Warning threshold %%")
    parser.add_argument("--crit", type=float, default=CRIT_PCT, help="Critical threshold %%")
    parser.add_argument("--report", action="store_true", help="Generate MD report from history")
    parser.add_argument("--open", action="store_true", help="Open report in default app")
    parser.add_argument("--html", action="store_true", help="Generate standalone HTML dashboard file")
    parser.add_argument("--serve", action="store_true", help="Start HTTP server with live dashboard")
    parser.add_argument("--api", action="store_true", help="Start HTTP server with JSON API only")
    parser.add_argument("--port", type=int, default=0, help="HTTP server port (default: 8642 serve, 8643 api)")
    parser.add_argument("--cookie", type=Path, default=DEFAULT_COOKIE_FILE, help="Cookie file path")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    args = parser.parse_args()

    # --report: generate and optionally open, then exit
    if args.report:
        path = generate_report(HISTORY_FILE, REPORT_FILE)
        if args.open:
            try:
                subprocess.Popen(["open", path])
                print(f"Report opened: {path}")
            except (OSError, FileNotFoundError):
                print(f"Report: {path}")
        else:
            print(path)
        return 0

    # --alert: fetch, record, check thresholds, print only if crossed
    if args.alert:
        try:
            data = _fetch_usage(args.cookie)
            _record_history(data, HISTORY_FILE)
            _record_session(data, SESSION_FILE)
            alert = check_alert(data, args.warn, args.crit)
            if alert:
                print(alert)
            return 0
        except Exception as e:
            print(f"❌ Ollama usage alert error: {e}", file=sys.stderr)
            return 1

    # --history: fetch + record, print summary, exit
    if args.history:
        try:
            data = _fetch_usage(args.cookie)
            _record_history(data, HISTORY_FILE)
            _record_session(data, SESSION_FILE)
            print_usage(data, as_json=args.json)
            return 0
        except Exception as e:
            print(f"❌ Error: {e}", file=sys.stderr)
            return 1

    # --watch: poll loop
    if args.watch:
        print(f"Watching Ollama Cloud usage every {args.interval}s… (Ctrl+C to stop)")
        while True:
            try:
                data = _fetch_usage(args.cookie)
                _record_history(data, HISTORY_FILE)
                _record_session(data, SESSION_FILE)
                alert = check_alert(data, args.warn, args.crit)
                ts = datetime.now().strftime("%H:%M:%S")
                s = data.get("session_used_pct", 0)
                w = data.get("weekly_used_pct", 0)
                print(f"[{ts}] S{s:.1f}% W{w:.1f}%{' · ' + alert if alert else ''}")
            except Exception as e:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Error: {e}", file=sys.stderr)
            time.sleep(args.interval)

    # --html: generate standalone HTML file
    if args.html:
        try:
            path = _generate_html_file(args.cookie)
            if args.open:
                try:
                    subprocess.Popen(["open", path])
                    print(f"Dashboard opened: {path}")
                except (OSError, FileNotFoundError):
                    print(path)
            else:
                print(path)
            return 0
        except Exception as e:
            print(f"❌ Error: {e}", file=sys.stderr)
            return 1

    # --serve: live HTTP dashboard
    if args.serve:
        port = args.port or 8642
        return _run_server(args.cookie, "serve", port)

    # --api: JSON API server
    if args.api:
        port = args.port or 8643
        return _run_server(args.cookie, "api", port)

    # Default: one-shot print
    try:
        data = _fetch_usage(args.cookie)
        print_usage(data, as_json=args.json)
        return 0
    except Exception as e:
        print(f"❌ Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())