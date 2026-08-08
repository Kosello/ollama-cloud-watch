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

    return result


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


def _generate_html(data: dict, history: list, sessions: list) -> str:
    """Self-contained HTML dashboard from current data + history."""
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

    def model_rows(models, with_cost=False):
        if not models:
            return "<tr><td colspan='4' class='muted'>No data</td></tr>"
        rows = []
        for m in models:
            reqs = m.get("requests", 0)
            share = m.get("share_pct", 0) or 0
            cr = m.get("est_cost_per_req", 0)
            extra = f"<td class='num'>${cr:.4f}/req</td>" if with_cost else ""
            rows.append(f"<tr><td>{m['model']}</td><td class='num'>{reqs}</td><td class='num'>{share:.1f}%</td>{extra}</tr>")
        return "\n".join(rows)

    def history_rows(weeks):
        if not weeks:
            return "<tr><td colspan='4' class='muted'>No history yet</td></tr>"
        rows = []
        for wk in weeks[-8:]:
            w_pct = wk.get("weekly_used_pct", 0) or 0
            cost = wk.get("est_cost_consumed")
            models = wk.get("models") or []
            top = max(models, key=lambda m: m.get("share_pct") or 0).get("model") if models else "-"
            total = sum(m.get("requests") or 0 for m in models)
            cost_s = f"${cost:.2f}" if cost is not None else "?"
            rows.append(f"<tr><td>{wk.get('week')}</td><td class='num'>{w_pct:.1f}%</td><td class='num'>{cost_s}</td><td>{top} ({total})</td></tr>")
        return "\n".join(rows)

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
  .cards {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 20px; }}
  .card {{ background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }}
  .card h2 {{ font-size: 0.75rem; color: var(--secondary); margin-bottom: 8px; font-weight: 500; }}
  .big {{ font-size: 1.8rem; font-weight: 700; }}
  .reset {{ color: var(--quaternary); font-size: 0.8rem; margin-top: 4px; }}
  .section {{ background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 16px; margin-bottom: 12px; }}
  .section h2 {{ font-size: 0.8rem; color: var(--secondary); margin-bottom: 10px; font-weight: 600; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th {{ text-align: left; font-size: 0.7rem; color: var(--quaternary); font-weight: 500; padding: 4px 8px; border-bottom: 1px solid var(--border); }}
  td {{ padding: 6px 8px; border-bottom: 1px solid var(--border); font-size: 0.85rem; }}
  .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .muted {{ color: var(--quaternary); text-align: center; padding: 12px; }}
  .cost {{ color: var(--quaternary); font-size: 0.8rem; margin-top: 8px; }}
  .bar {{ height: 6px; border-radius: 3px; background: var(--border); margin-top: 8px; overflow: hidden; }}
  .bar-fill {{ height: 100%; border-radius: 3px; transition: width 0.5s; }}
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

  <div class="section">
    <h2>Session — per model</h2>
    <table><thead><tr><th>Model</th><th class="num">Requests</th><th class="num">Share</th></tr></thead><tbody>{model_rows(sm)}</tbody></table>
  </div>

  <div class="section">
    <h2>Weekly — per model</h2>
    <table><thead><tr><th>Model</th><th class="num">Requests</th><th class="num">Share</th><th class="num">Cost</th></tr></thead><tbody>{model_rows(wm, with_cost=True)}</tbody></table>
    <div class="cost">Budget: ${budget:.2f}/wk · consumed: ${consumed:.2f} · avg ${avg_req:.4f}/req</div>
  </div>

  <div class="section">
    <h2>Weekly history</h2>
    <table><thead><tr><th>Week</th><th class="num">Used</th><th class="num">Cost</th><th>Top model</th></tr></thead><tbody>{history_rows(history)}</tbody></table>
  </div>

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


def _lifetime_from_history(weeks: list) -> dict:
    """Aggregate lifetime stats from history records."""
    if not weeks:
        return {"ok": True, "weeks_count": 0, "models": []}
    model_reqs: dict[str, int] = {}
    model_cost_wsum: dict[str, float] = {}
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