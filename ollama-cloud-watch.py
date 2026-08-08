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


# ── main ────────────────────────────────────────────────────────────────────

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