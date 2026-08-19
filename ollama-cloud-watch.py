#!/usr/bin/env python3
"""
ollama-cloud-watch.py — Standalone Ollama Cloud usage monitor.

Works with or without Hermes Agent. The authenticated settings page is the
primary source because it exposes per-model usage-bar shares; the official
usage API is a limited fallback.

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
  --host HOST     HTTP bind host (default 127.0.0.1; use ::1 for IPv6 loopback)
  --port PORT     HTTP server port (default 8642 for --serve, 8643 for --api)
  --cookie PATH   Custom cookie file path
  --json          Output raw JSON instead of formatted text

UPDATE
  The dashboard checks the GitHub repo for newer commits and shows an
  update button in the footer when one is available. Clicking it runs
  git pull --ff-only and restarts the server (requires a clean checkout
  at the repo root).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
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
DEFAULT_API_KEY_FILE = Path.home() / ".ollama-cloud-api-key.txt"
PLAN_FILE = Path.home() / ".ollama-cloud-plan.txt"
HERMES_PLAN_FILE = Path.home() / ".hermes" / "ollama-usage-plan.txt"
VALID_PLANS = ("free", "pro", "max")
PLAN_MONTHLY_USD = {"free": 0.0, "pro": 20.0, "max": 100.0}
WEEKS_PER_MONTH = 365.2425 / 12 / 7
KEYCHAIN_SERVICE = "ollama-cloud-watch"
KEYCHAIN_ACCOUNT = "ollama"
HISTORY_MAX_WEEKS = 52  # keep a full year
SESSION_LOG_CAP = 1000

# ── Self-update (dashboard update button) ─────────────────────────────────
REPO_DIR = Path(__file__).resolve().parent
GITHUB_REPO = "Kosello/ollama-cloud-watch"
UPDATE_CHECK_TTL = 3600  # re-check GitHub at most once per hour
_update_state: dict = {"ts": 0.0, "result": None}


def _git(cmd: list[str], timeout: int = 15) -> str:
    """Run git in the repo dir; raise on failure."""
    out = subprocess.run(
        ["git", "-C", str(REPO_DIR), *cmd],
        capture_output=True, text=True, timeout=timeout,
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or out.stdout.strip() or f"git {' '.join(cmd)} failed")
    return out.stdout.strip()


def _check_for_update(force: bool = False) -> dict:
    """Compare local HEAD with origin/main. Cached for UPDATE_CHECK_TTL."""
    now = time.time()
    if not force and _update_state["result"] and (now - _update_state["ts"]) < UPDATE_CHECK_TTL:
        return _update_state["result"]
    result = {"ok": True, "update_available": False, "local": None, "remote": None, "error": None}
    try:
        local = _git(["rev-parse", "HEAD"])
        _git(["fetch", "origin", "main", "--quiet"], timeout=60)
        remote = _git(["rev-parse", "origin/main"])
        result["local"] = local[:7]
        result["remote"] = remote[:7]
        result["update_available"] = local != remote
    except Exception as e:
        result["ok"] = False
        result["error"] = str(e)
    _update_state["ts"] = now
    _update_state["result"] = result
    return result


def _apply_update() -> dict:
    """git pull --ff-only, then restart the server process.

    The LaunchAgent has KeepAlive=true, so exiting is enough to come back
    with the new code. The restart is deferred ~1s so the HTTP response
    can be delivered first.
    """
    result = {"ok": True, "updated": False, "error": None}
    try:
        status = _git(["status", "--porcelain"])
        if status.strip():
            result["ok"] = False
            result["error"] = "Local changes present — update requires a clean checkout"
            return result
        _git(["pull", "--ff-only", "origin", "main"], timeout=120)
        result["updated"] = True
        threading.Timer(1.0, lambda: os._exit(0)).start()
    except Exception as e:
        result["ok"] = False
        result["error"] = str(e)
    return result

# ── Price / token fallback chain (mirrors the Hermes plugin) ──────────────
# Priority: manual override file → live OpenRouter (24h cache) → builtin.
PRICE_OVERRIDE_FILE = Path.home() / ".ollama-cloud-prices.json"
PRICE_CACHE_FILE = Path.home() / ".ollama-cloud-price-cache.json"
STATE_DB = Path.home() / ".hermes" / "state.db"
PRICE_CACHE_TTL_SECONDS = 24 * 3600
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"

_prices_cache: dict = {"ts": 0, "prices": None, "source": None}


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

API_USAGE_URL = "https://ollama.com/api/usage"
API_KEY_FILE = DEFAULT_API_KEY_FILE
HERMES_API_KEY_FILE = Path.home() / ".hermes" / "ollama_api_key.txt"
API_KEY_SOURCE = os.environ.get("OLLAMA_API_KEY_SOURCE", "file")


def _api_key() -> str | None:
    """Read the official API key from file (or env)."""
    env_key = os.environ.get("OLLAMA_API_KEY")
    if env_key:
        return env_key
    if API_KEY_SOURCE == "file":
        for f in (API_KEY_FILE, HERMES_API_KEY_FILE):
            if f.exists():
                key = f.read_text().strip()
                if key and not key.startswith("__Secure-session"):
                    return key
    return None


def _fetch_usage_api(api_key: str) -> dict:
    """Fetch usage from the official API (no cookie needed)."""
    req = urllib.request.Request(
        API_USAGE_URL,
        headers={"Authorization": api_key, "User-Agent": "ollama-cloud-watch/1.0"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _configured_plan() -> str:
    """Resolve plan from standalone file, Hermes file, env, then Pro default."""
    for path in (PLAN_FILE, HERMES_PLAN_FILE):
        try:
            if path.exists():
                value = path.read_text().strip().lower()
                if value in VALID_PLANS:
                    return value.capitalize()
        except OSError:
            pass
    value = os.environ.get("OLLAMA_PLAN", "").strip().lower()
    return value.capitalize() if value in VALID_PLANS else "Pro"


def _api_to_usage(api_data: dict) -> dict:
    """Normalize official API data and apply the shared enrichment."""
    limits = api_data.get("limits", {})
    session = limits.get("session", {})
    weekly = limits.get("weekly", {})

    def _models(block: dict) -> list:
        models = [
            {"model": m.get("name", "?"),
             "requests": m.get("request_count", 0), "share_pct": None}
            for m in block.get("models", [])
        ]
        total = sum(m["requests"] for m in models)
        if total:
            for model in models:
                model["share_pct"] = round(model["requests"] / total * 100.0, 1)
        return models

    session_usage = float(session.get("usage", 0) or 0)
    weekly_usage = float(weekly.get("usage", 0) or 0)
    data = {
        "plan": _configured_plan(),
        "session_used_pct": round(session_usage * 100.0, 1),
        "weekly_used_pct": round(weekly_usage * 100.0, 1),
        "session_reset": None,
        "weekly_reset": None,
        "session_reset_iso": None,
        "weekly_reset_iso": None,
        "session_models": _models(session),
        "weekly_models": _models(weekly),
        "source": "api",
        "share_basis": "requests",
        "reset_estimated": False,
        "reset_unavailable": True,
    }
    _enrich_with_costs(data)
    return data


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


# ── price / token fallback chain (mirrors the Hermes plugin) ────────────────

def _load_manual_price_overrides() -> dict:
    """Read manual price/token overrides from ~/.ollama-cloud-prices.json.

    Format (all optional):
    {
      "models": {
        "glm-5.2": {"input": 1.40, "output": 4.40, "cache_read": 0.26}
      },
      "tokens_per_request": {
        "glm-5.2": [100000, 3000, 20000]   # [in, out, cache_read]
      }
    }
    Returns {"prices": {...}, "tokens": {...}}.
    """
    result = {"prices": {}, "tokens": {}}
    try:
        if not PRICE_OVERRIDE_FILE.exists():
            return result
        data = json.loads(PRICE_OVERRIDE_FILE.read_text())
        for model, value in (data.get("models", {}) or {}).items():
            try:
                if isinstance(value, dict):
                    cache = value.get("cache_read")
                    result["prices"][model] = (
                        float(value["input"]), float(value["output"]),
                        float(cache) if cache is not None else None,
                    )
                else:
                    result["prices"][model] = (
                        float(value[0]), float(value[1]),
                        float(value[2]) if value[2] is not None else None,
                    )
            except (KeyError, TypeError, ValueError, IndexError):
                print(f"warning: invalid price override for {model}", file=sys.stderr)
        result["tokens"] = data.get("tokens_per_request", {}) or {}
    except (json.JSONDecodeError, OSError) as e:
        print(f"warning: price override file unreadable: {e}", file=sys.stderr)
    return result


def _fetch_openrouter_prices() -> dict:
    """Fetch official API list prices from OpenRouter's public model list.

    Returns {model_id: (input_per_1M, output_per_1M, cache_read_per_1M)}.
    Raises on network/parse failure so callers fall through the chain.
    """
    req = urllib.request.Request(
        OPENROUTER_MODELS_URL,
        headers={"User-Agent": "ollama-cloud-watch/1.0"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        payload = json.loads(resp.read().decode())
    prices = {}
    for m in payload.get("data", []):
        pid = m.get("id", "")
        pricing = m.get("pricing", {}) or {}
        inp = pricing.get("prompt")
        out = pricing.get("completion")
        cache = pricing.get("input_cache_read") or pricing.get("cache_read")
        try:
            # OpenRouter reports per-token prices; our convention is USD per
            # 1M tokens (matches the builtin table) — normalize here.
            prices[pid] = (float(inp) * 1e6, float(out) * 1e6,
                           float(cache) * 1e6 if cache is not None else None)
        except (TypeError, ValueError):
            continue
    if not prices:
        raise RuntimeError("OpenRouter model list returned no pricing")
    return prices


def _resolve_api_prices() -> tuple[dict, str]:
    """Resolve live/cached prices and merge partial manual overrides."""
    manual = _load_manual_price_overrides()["prices"]

    def _with_manual(base: dict, source: str) -> tuple[dict, str]:
        if not manual:
            return base, source
        merged = dict(base)
        merged.update(manual)
        return merged, f"{source} + manual overrides"

    now = time.time()
    if _prices_cache["prices"] and (now - _prices_cache["ts"]) < PRICE_CACHE_TTL_SECONDS:
        return _with_manual(_prices_cache["prices"], _prices_cache["source"])
    if PRICE_CACHE_FILE.exists():
        try:
            cached = json.loads(PRICE_CACHE_FILE.read_text())
            age = now - cached.get("fetched_at", 0)
            if age < PRICE_CACHE_TTL_SECONDS and cached.get("prices"):
                _prices_cache.update(ts=now, prices=cached["prices"], source="OpenRouter (cached)")
                return _with_manual(cached["prices"], "OpenRouter (cached)")
        except (json.JSONDecodeError, OSError):
            pass
    try:
        live = _fetch_openrouter_prices()
        _prices_cache.update(ts=now, prices=live, source="OpenRouter (live)")
        try:
            PRICE_CACHE_FILE.write_text(json.dumps({"fetched_at": now, "prices": live}, indent=2))
        except OSError:
            pass
        return _with_manual(live, "OpenRouter (live)")
    except Exception as exc:
        print(f"warning: OpenRouter price fetch failed: {exc}", file=sys.stderr)
    return _with_manual(_BUILTIN_PRICES, "builtin defaults")


_BUILTIN_PRICES = {
    "glm-5.2": (0.07, 0.22, 0.013),
    "glm-5.2:cloud": (0.07, 0.22, 0.013),
    "glm-5": (1.40, 4.40, 0.26),
    "deepseek-v4-flash:0731": (0.09, 0.18, 0.018),
    "deepseek-v4-flash": (0.14, 0.28, 0.028),
    "deepseek-v4-pro": (0.435, 0.87, 0.003625),
    "minimax-m3": (0.30, 1.20, 0.06),
    "gemma4:31b": (0.10, 0.34, 0.10),
    "kimi-k2.7-code": (0.70, 3.50, 0.15),
    "kimi-k2.6": (0.95, 4.00, 0.16),
    "gpt-5.5": (5.00, 30.00, 0.50),
    "gpt-oss:120b": (0.037, 0.17, None),
    "nemotron-3-ultra": (0.60, 3.60, 0.20),
    "nemotron-3-super": (0.30, 0.90, None),
}


# ── Official DeepSeek-V4 API pricing (effective 2026-08-16 16:00 UTC) ──────
# Peak hours: 01:00–04:00 and 06:00–10:00 UTC; all other hours are off-peak.
# USD per 1M tokens: input (cache hit), input (cache miss), output.
_DEEPSEEK_OFFICIAL_PEAK = {
    "deepseek-v4-flash": (0.014, 0.44, 1.32),
    "deepseek-v4-pro": (0.044, 1.32, 3.96),
}
_DEEPSEEK_OFFICIAL_OFFPEAK = {
    "deepseek-v4-flash": (0.007, 0.22, 0.66),
    "deepseek-v4-pro": (0.022, 0.66, 1.98),
}
# Peak windows: (start_hour, end_hour) inclusive-exclusive, UTC.
_DEEPSEEK_PEAK_WINDOWS = ((1, 4), (6, 10))
_DEEPSEEK_PRICING_EFFECTIVE_TS = 1786896000  # 2026-08-16 16:00 UTC


def _is_deepseek_peak_now() -> bool:
    """True if the current UTC time falls in a DeepSeek peak window."""
    hour = datetime.now(timezone.utc).hour
    return any(start <= hour < end for start, end in _DEEPSEEK_PEAK_WINDOWS)


def _deepseek_official_price(model: str) -> tuple | None:
    """Official DeepSeek-V4 (cache-hit, cache-miss, output) rate per 1M tokens.

    Applies the new official pricing when the current UTC time is past the
    effective timestamp (2026-08-16 16:00 UTC) and the model is one of the
    two DeepSeek-V4 models. Returns a 3-tuple matching the plugin convention
    ``(input, output, cached_input)`` where input is the cache-miss rate.
    """
    base = model.split(":", 1)[0]
    if base not in _DEEPSEEK_OFFICIAL_PEAK:
        return None
    if time.time() < _DEEPSEEK_PRICING_EFFECTIVE_TS:
        return None
    rates = _DEEPSEEK_OFFICIAL_PEAK if _is_deepseek_peak_now() else _DEEPSEEK_OFFICIAL_OFFPEAK
    cache_hit, cache_miss, output = rates[base]
    return (cache_miss, output, cache_hit)


def _lookup_price(api_prices: dict, model: str) -> tuple | None:
    """Resolve Ollama names without stripping meaningful variants too early."""
    if model in api_prices:
        return api_prices[model]

    # Official DeepSeek-V4 rates beat stale OpenRouter snapshots, but never
    # manual overrides (those are already merged into api_prices above).
    official = _deepseek_official_price(model)
    if official:
        return official

    aliases = {
        "nemotron-3-ultra": "nvidia/nemotron-3-ultra-550b-a55b",
        "nemotron-3-super": "nvidia/nemotron-3-super-120b-a12b",
    }
    alias = aliases.get(model)
    if alias and alias in api_prices:
        return api_prices[alias]

    hyphen_model = model.replace(":", "-")
    for key, value in api_prices.items():
        if key.endswith(f"/{hyphen_model}"):
            return value

    def _norm(value: str) -> str:
        value = value.lower().replace("-", "").replace(":", "").replace(".", "").replace("_", "")
        for tag in ("it", "instruct", "free", "latest"):
            if value.endswith(tag):
                value = value[:-len(tag)]
        return value

    normalized = _norm(hyphen_model)
    for key, value in api_prices.items():
        key_base = key.rsplit("/", 1)[-1]
        if _norm(key_base) == normalized:
            return value

    base = model.split(":")[0]
    if base in api_prices:
        return api_prices[base]
    for key, value in api_prices.items():
        if key.endswith(f"/{base}"):
            return value
    return _BUILTIN_PRICES.get(model) or _BUILTIN_PRICES.get(base)


def _resolve_token_averages(overrides: dict) -> tuple[dict, str]:
    """Merge per-model manual token overrides over Hermes history."""
    from_db = _real_token_averages()
    merged = dict(from_db)
    manual = {}
    for model, value in (overrides.get("tokens") or {}).items():
        try:
            manual[model] = (float(value[0]), float(value[1]), float(value[2]))
        except (TypeError, ValueError, IndexError):
            print(f"warning: invalid token override for {model}", file=sys.stderr)
    merged.update(manual)
    if manual and from_db:
        return merged, "Hermes state.db + manual overrides"
    if manual:
        return merged, "manual overrides"
    if from_db:
        return merged, "Hermes state.db"
    return {}, "no token data"


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
        "source": "cookie",
        "share_basis": "ollama_usage_bar",
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

    result["share_basis"] = "ollama_usage_bar"
    result["reset_estimated"] = False
    if not result.get("plan"):
        result["plan"] = _configured_plan()
    _enrich_with_costs(result)
    return result


def _enrich_with_costs(data: dict) -> None:
    """Add honest subscription and pay-per-token estimates in place.

    Ollama quota percentage is not money spent. The fixed subscription is
    therefore represented only as a monthly price and its 7-day equivalent.
    API-equivalent values use request counts from Ollama, historical average
    token mix from Hermes, and per-token prices from the configured resolver.
    """
    weekly_segs = data.get("weekly_models") or []
    total_reqs = sum(max(int(s.get("requests") or 0), 0) for s in weekly_segs)

    # Fixed subscription economics. Never multiply the fee by quota usage:
    # 80% quota used does not mean 80% of the subscription fee was consumed.
    plan_key = (data.get("plan") or "pro").lower()
    monthly_cost = PLAN_MONTHLY_USD.get(plan_key, PLAN_MONTHLY_USD["pro"])
    weekly_equivalent = monthly_cost / WEEKS_PER_MONTH
    effective_sub_cpr = weekly_equivalent / total_reqs if total_reqs else 0.0

    data["subscription_monthly_cost"] = round(monthly_cost, 2)
    data["subscription_weekly_equivalent"] = round(weekly_equivalent, 4)
    data["effective_subscription_cost_per_req"] = round(effective_sub_cpr, 6)
    data["total_weekly_requests"] = total_reqs
    # Backward-compatible names for older frontends/history records.
    data["est_weekly_budget"] = round(weekly_equivalent, 2)
    data["est_cost_consumed"] = round(weekly_equivalent, 2)
    data["est_avg_cost_per_req"] = round(effective_sub_cpr, 6)
    data["quota_weighted_plan_value"] = round(
        weekly_equivalent * ((data.get("weekly_used_pct") or 0.0) / 100.0), 2
    )

    for seg in weekly_segs:
        reqs = max(int(seg.get("requests") or 0), 0)
        request_share = reqs / total_reqs if total_reqs else 0.0
        seg["request_share_pct"] = round(request_share * 100.0, 1)
        seg["est_cost"] = round(weekly_equivalent * request_share, 4)
        seg["est_cost_per_req"] = round(effective_sub_cpr, 6) if reqs else 0.0
        # Per-model quota cost is not exposed by Ollama; do not invent it.
        seg["est_cost_per_req_pct"] = None

    api_prices, price_source = _resolve_api_prices()
    overrides = _load_manual_price_overrides()
    token_avgs, token_source = _resolve_token_averages(overrides)

    fallback_avg = _real_global_token_average()
    fallback_basis = "request-weighted all-model history" if fallback_avg else None
    if fallback_avg is None and token_avgs:
        vals = list(token_avgs.values())
        fallback_avg = (
            round(sum(v[0] for v in vals) / len(vals)),
            round(sum(v[1] for v in vals) / len(vals)),
            round(sum(v[2] for v in vals) / len(vals)),
        )
        fallback_basis = "cross-model mean"

    api_total_raw = 0.0
    api_input_cost_raw = 0.0
    api_cache_cost_raw = 0.0
    api_output_cost_raw = 0.0
    priced_uncached_input_tokens = 0.0
    priced_cache_read_tokens = 0.0
    priced_output_tokens = 0.0
    priced_requests = 0
    unpriced_models = []
    token_bases = set()
    manual_token_models = set(overrides.get("tokens", {}))

    def _token_profile(model: str):
        avg = token_avgs.get(model)
        if avg and model in manual_token_models:
            basis = "manual model override"
        elif avg:
            basis = "model history"
        elif fallback_avg:
            avg = fallback_avg
            basis = fallback_basis
        else:
            avg = (1000.0, 500.0, 0.0)
            basis = "fixed fallback"
        return (
            max(float(avg[0] or 0), 0.0),
            max(float(avg[1] or 0), 0.0),
            max(float(avg[2] or 0), 0.0),
            basis,
        )

    for seg in weekly_segs:
        reqs = max(int(seg.get("requests") or 0), 0)
        in_t, out_t, cache_t, token_basis = _token_profile(seg["model"])
        prompt_t = in_t + cache_t
        token_bases.add(token_basis)
        seg["token_estimate_basis"] = token_basis
        seg["avg_uncached_input_tokens"] = round(in_t)
        seg["avg_cache_tokens"] = round(cache_t)
        seg["avg_prompt_tokens"] = round(prompt_t)
        seg["avg_in_tokens"] = round(prompt_t)
        seg["avg_out_tokens"] = round(out_t)
        seg["cache_hit_pct"] = round(cache_t / prompt_t * 100.0, 1) if prompt_t > 0 else None
        seg["total_uncached_input_tokens"] = round(in_t * reqs)
        seg["total_cache_read_tokens"] = round(cache_t * reqs)
        seg["total_prompt_tokens"] = round(prompt_t * reqs)
        seg["total_in_tokens"] = round(prompt_t * reqs)
        seg["total_out_tokens"] = round(out_t * reqs)

        prices = _lookup_price(api_prices, seg["model"])
        if not prices:
            unpriced_models.append(seg["model"])
            for field in (
                "api_cost_per_req", "api_effective_per_1m", "api_weekly_cost",
                "api_cost_pct", "api_input_cost", "api_cache_cost",
                "api_output_cost", "api_input_per_1m", "api_output_per_1m",
                "api_cache_per_1m",
            ):
                seg[field] = None
            continue

        p_in, p_out, p_cache_published = prices
        p_in = float(p_in)
        p_out = float(p_out)
        # Missing cache pricing means no published discount, not free input.
        p_cache = p_in if p_cache_published is None else float(p_cache_published)

        # Real provider cache rate (state.db, non-Ollama providers; largest
        # sample wins, OpenRouter last). Used for the cache-aware cost line.
        real_cache = _cache_rate_for(seg["model"], _real_provider_cache_hits())
        seg["api_real_cache_pct"] = real_cache

        per_req = (
            (in_t / 1e6) * p_in
            + (cache_t / 1e6) * p_cache
            + (out_t / 1e6) * p_out
        )
        processed_per_req = prompt_t + out_t
        effective_per_1m = (
            per_req * 1e6 / processed_per_req if processed_per_req > 0 else None
        )
        input_cost = (in_t / 1e6) * p_in * reqs
        cache_cost = (cache_t / 1e6) * p_cache * reqs
        output_cost = (out_t / 1e6) * p_out * reqs
        model_window_cost = input_cost + cache_cost + output_cost
        api_total_raw += model_window_cost
        api_input_cost_raw += input_cost
        api_cache_cost_raw += cache_cost
        api_output_cost_raw += output_cost
        priced_uncached_input_tokens += in_t * reqs
        priced_cache_read_tokens += cache_t * reqs
        priced_output_tokens += out_t * reqs
        priced_requests += reqs

        seg["api_cost_per_req"] = round(per_req, 6)
        seg["api_effective_per_1m"] = round(effective_per_1m, 6) if effective_per_1m is not None else None
        seg["api_weekly_cost"] = round(model_window_cost, 4)  # compatibility/internal totals
        seg["api_cost_pct"] = None  # per-model subscription comparison is unknowable
        seg["api_input_per_1m"] = round(p_in, 6)
        seg["api_output_per_1m"] = round(p_out, 6)
        seg["api_cache_per_1m"] = round(p_cache, 6)
        seg["api_cache_price_published"] = p_cache_published is not None
        seg["api_input_cost"] = round(input_cost, 4)
        seg["api_cache_cost"] = round(cache_cost, 4)
        seg["api_output_cost"] = round(output_cost, 4)

        # Cache-aware second line: same requests priced with the real provider
        # cache hit rate applied to the prompt tokens.
        if real_cache is not None and prompt_t > 0:
            uncached_t = prompt_t * (1.0 - real_cache / 100.0)
            cached_t = prompt_t * (real_cache / 100.0)
            per_req_cached = (
                (uncached_t / 1e6) * p_in
                + (cached_t / 1e6) * p_cache
                + (out_t / 1e6) * p_out
            )
            seg["api_cost_per_req_cached"] = round(per_req_cached, 6)
            seg["api_weekly_cost_cached"] = round(per_req_cached * reqs, 4)
        else:
            seg["api_cost_per_req_cached"] = None
            seg["api_weekly_cost_cached"] = None

    api_known_window_total = round(api_total_raw, 4) if priced_requests else None
    pricing_complete = bool(total_reqs) and priced_requests == total_reqs
    api_window_total = api_known_window_total if pricing_complete else None
    data["api_known_window_total"] = api_known_window_total
    data["api_window_total"] = api_window_total
    data["api_weekly_total"] = api_window_total  # backward compatibility: complete totals only
    data["api_price_coverage_pct"] = round(
        priced_requests / total_reqs * 100.0, 2
    ) if total_reqs else None

    def _component_rate(cost: float, tokens: float) -> float | None:
        return round(cost * 1e6 / tokens, 6) if tokens > 0 else None

    priced_prompt_tokens = priced_uncached_input_tokens + priced_cache_read_tokens
    priced_all_tokens = priced_prompt_tokens + priced_output_tokens
    api_prompt_cost_raw = api_input_cost_raw + api_cache_cost_raw
    api_blended_rates = {
        "uncached_input": _component_rate(api_input_cost_raw, priced_uncached_input_tokens),
        "cache": _component_rate(api_cache_cost_raw, priced_cache_read_tokens),
        "prompt": _component_rate(api_prompt_cost_raw, priced_prompt_tokens),
        "output": _component_rate(api_output_cost_raw, priced_output_tokens),
        "all": _component_rate(api_total_raw, priced_all_tokens),
    }
    covered_plan_equivalent = (
        weekly_equivalent * priced_requests / total_reqs if total_reqs else 0.0
    )
    allocation_factor = (
        covered_plan_equivalent / api_total_raw if api_total_raw > 0 else None
    )

    data["estimated_priced_uncached_input_tokens"] = round(priced_uncached_input_tokens)
    data["estimated_priced_cache_read_tokens"] = round(priced_cache_read_tokens)
    data["estimated_priced_prompt_tokens"] = round(priced_prompt_tokens)
    data["estimated_priced_output_tokens"] = round(priced_output_tokens)
    data["estimated_priced_all_tokens"] = round(priced_all_tokens)
    data["api_blended_uncached_input_per_1m"] = api_blended_rates["uncached_input"]
    data["api_blended_cache_per_1m"] = api_blended_rates["cache"]
    data["api_blended_prompt_per_1m"] = api_blended_rates["prompt"]
    data["api_blended_output_per_1m"] = api_blended_rates["output"]
    data["api_blended_effective_per_1m"] = api_blended_rates["all"]
    data["plan_priced_request_equivalent"] = round(covered_plan_equivalent, 4)
    data["plan_rate_allocation_factor"] = (
        round(allocation_factor, 8) if allocation_factor is not None else None
    )
    for bucket, api_rate in api_blended_rates.items():
        field = "plan_effective_all_per_1m" if bucket == "all" else f"plan_effective_{bucket}_per_1m"
        data[field] = (
            round(api_rate * allocation_factor, 6)
            if api_rate is not None and allocation_factor is not None else None
        )
    data["plan_rate_allocation_basis"] = (
        "7-day plan equivalent prorated to priced requests and allocated by API cost mix"
    )
    # Per-model Ollama effective $/1M. Cookie data includes each model's share
    # of the weekly usage bar. API-only mode lacks those quota weights and must
    # not fabricate per-model Ollama prices.
    is_cookie = data.get("source") == "cookie"
    positive_share_total = sum(
        max(float(seg.get("share_pct") or 0), 0.0) for seg in weekly_segs
    )

    weekly_used_fraction = max(float(data.get("weekly_used_pct") or 0), 0.0) / 100.0

    for seg in weekly_segs:
        seg["plan_rate_basis"] = None
        if is_cookie:
            gpu_share = seg.get("share_pct")
            reqs = max(int(seg.get("requests") or 0), 0)
            if gpu_share is not None and gpu_share > 0 and reqs > 0 and positive_share_total > 0:
                model_tokens = (
                    seg.get("avg_uncached_input_tokens", 0)
                    + seg.get("avg_cache_tokens", 0)
                    + seg.get("avg_out_tokens", 0)
                )
                seg["plan_effective_per_1m"] = round(
                    weekly_equivalent * weekly_used_fraction * (gpu_share / positive_share_total)
                    / (model_tokens * reqs) * 1e6,
                    9,
                ) if model_tokens > 0 and reqs > 0 else None
                seg["plan_rate_basis"] = "ollama_usage_bar+historical_tokens"
            else:
                seg["plan_effective_per_1m"] = None
        else:
            seg["plan_effective_per_1m"] = None
        if seg.get("plan_effective_per_1m") is not None and seg.get("api_effective_per_1m"):
            seg["plan_pct_of_api"] = round(
                seg["plan_effective_per_1m"] / seg["api_effective_per_1m"] * 100, 1
            )
        else:
            seg["plan_pct_of_api"] = None
        # Cache break-even: the cache hit rate at which the pay-per-token API
        # cost equals this model's subscription allocation. Always reported as
        # a percentage — above 100% means even a 100% cache hit cannot make
        # the API cheaper (plan always wins); 0% means the API wins even with
        # no caching.
        seg["api_break_even_cache_pct"] = None
        seg["api_real_cache_pct"] = None
        if seg.get("plan_effective_per_1m") is not None and seg.get("api_input_per_1m") is not None:
            p_in = float(seg["api_input_per_1m"])
            p_cache = float(seg["api_cache_per_1m"])
            p_out = float(seg["api_output_per_1m"])
            in_t = float(seg.get("avg_uncached_input_tokens") or 0)
            cache_t = float(seg.get("avg_cache_tokens") or 0)
            out_t = float(seg.get("avg_out_tokens") or 0)
            prompt_t = in_t + cache_t
            tokens_per_req = prompt_t + out_t
            sub_per_req = seg["plan_effective_per_1m"] * tokens_per_req / 1e6
            if sub_per_req > 0 and prompt_t > 0:
                api_at_0 = (prompt_t * p_in + out_t * p_out) / 1e6
                if p_cache >= p_in:
                    # Caching does not reduce the API cost; the break-even is
                    # either 0% (API cheaper even uncached) or 100%+ (plan wins).
                    seg["api_break_even_cache_pct"] = 0.0 if sub_per_req >= api_at_0 else 100.0
                else:
                    denom = prompt_t * (p_cache - p_in)
                    h = (sub_per_req * 1e6 - prompt_t * p_in - out_t * p_out) / denom
                    seg["api_break_even_cache_pct"] = round(max(h, 0.0) * 100.0, 1)
                # Real provider cache rate (state.db, non-Ollama providers).
                seg["api_real_cache_pct"] = _cache_rate_for(
                    seg["model"], _real_provider_cache_hits()
                )
                # Cache-aware API effective $/1M: prompt tokens split by the
                # real provider cache rate, priced at input/cache rates.
                seg["api_effective_per_1m_cached"] = None
                seg["plan_pct_of_api_cached"] = None
                real_cache = seg["api_real_cache_pct"]
                if real_cache is not None and prompt_t > 0:
                    uncached_t = prompt_t * (1.0 - real_cache / 100.0)
                    cached_t = prompt_t * (real_cache / 100.0)
                    per_req_cached = (
                        (uncached_t / 1e6) * p_in
                        + (cached_t / 1e6) * p_cache
                        + (out_t / 1e6) * p_out
                    )
                    api_eff_cached = per_req_cached * 1e6 / tokens_per_req
                    seg["api_effective_per_1m_cached"] = round(api_eff_cached, 6)
                    if seg.get("plan_effective_per_1m") is not None and api_eff_cached > 0:
                        seg["plan_pct_of_api_cached"] = round(
                            seg["plan_effective_per_1m"] / api_eff_cached * 100, 1
                        )
    data["plan_rate_allocation_basis"] = (
        "7-day plan equivalent × observed weekly quota fraction × normalized usage-bar share, "
        "divided by estimated model tokens"
        if is_cookie else
        "Unavailable in API-only mode: /api/usage has no per-model quota weights"
    )
    data["api_unpriced_models"] = unpriced_models
    data["api_assumption"] = (
        "Token estimate basis: " + ", ".join(sorted(token_bases))
        if token_bases else "No priced requests"
    )
    data["price_source"] = price_source
    data["token_source"] = token_source
    data["ollama_monthly"] = round(monthly_cost, 2)

    # ── session-level API equivalent cost ──
    session_segs = data.get("session_models") or []
    session_api_total = 0.0
    session_priced_requests = 0
    session_unpriced = []
    for seg in session_segs:
        reqs = max(int(seg.get("requests") or 0), 0)
        in_t, out_t, cache_t, _ = _token_profile(seg["model"])
        prompt_t = in_t + cache_t
        seg["avg_uncached_input_tokens"] = round(in_t)
        seg["avg_cache_tokens"] = round(cache_t)
        seg["avg_prompt_tokens"] = round(prompt_t)
        seg["avg_in_tokens"] = round(prompt_t)
        seg["avg_out_tokens"] = round(out_t)
        seg["total_uncached_input_tokens"] = round(in_t * reqs)
        seg["total_cache_read_tokens"] = round(cache_t * reqs)
        seg["total_prompt_tokens"] = round(prompt_t * reqs)
        seg["total_in_tokens"] = round(prompt_t * reqs)
        seg["total_out_tokens"] = round(out_t * reqs)

        prices = _lookup_price(api_prices, seg["model"])
        if not prices:
            session_unpriced.append(seg["model"])
            seg["api_session_cost"] = None
            continue
        p_in, p_out, p_cache_published = prices
        p_in = float(p_in)
        p_out = float(p_out)
        p_cache = p_in if p_cache_published is None else float(p_cache_published)
        per_req = (in_t / 1e6) * p_in + (cache_t / 1e6) * p_cache + (out_t / 1e6) * p_out
        model_session_cost = per_req * reqs
        session_api_total += model_session_cost
        session_priced_requests += reqs
        seg["api_session_cost"] = round(model_session_cost, 4)

        # Cache-aware second line for the session window.
        real_cache = _cache_rate_for(seg["model"], _real_provider_cache_hits())
        seg["api_real_cache_pct"] = real_cache
        if real_cache is not None and prompt_t > 0:
            uncached_t = prompt_t * (1.0 - real_cache / 100.0)
            cached_t = prompt_t * (real_cache / 100.0)
            per_req_cached = (
                (uncached_t / 1e6) * p_in
                + (cached_t / 1e6) * p_cache
                + (out_t / 1e6) * p_out
            )
            seg["api_session_cost_cached"] = round(per_req_cached * reqs, 4)
        else:
            seg["api_session_cost_cached"] = None

    total_session_reqs = sum(max(int(s.get("requests") or 0), 0) for s in session_segs)
    session_complete = bool(total_session_reqs) and session_priced_requests == total_session_reqs
    data["api_session_known_total"] = round(session_api_total, 4) if session_priced_requests else None
    data["api_session_total"] = data["api_session_known_total"] if session_complete else None
    data["api_session_coverage_pct"] = round(
        session_priced_requests / total_session_reqs * 100.0, 2
    ) if total_session_reqs else None
    data["api_session_unpriced"] = session_unpriced

    if api_window_total is not None and api_window_total > 0:
        savings = api_window_total - weekly_equivalent
        data["api_savings_vs_plan"] = round(savings, 2)
        data["api_savings"] = round(savings, 2)  # compatibility
        data["api_monthly_proj"] = None
        data["api_monthly_projection_reason"] = "weekly elapsed time is not exposed by the API"
        data["api_total_pct"] = round(weekly_equivalent / api_window_total * 100.0, 1)
        data["api_vs_plan_ratio"] = (
            round(api_window_total / weekly_equivalent, 3) if weekly_equivalent > 0 else None
        )
        data["break_even_usage_multiple"] = round(weekly_equivalent / api_window_total, 3)
        data["break_even_pct"] = round(weekly_equivalent / api_window_total * 100.0, 1)
    else:
        for field in (
            "api_savings_vs_plan", "api_savings", "api_monthly_proj",
            "api_total_pct", "api_vs_plan_ratio", "break_even_usage_multiple",
            "break_even_pct",
        ):
            data[field] = None


def _real_token_averages() -> dict:
    """Canonical per-model averages from Hermes usage accounting.

    ``input_tokens`` is uncached input; ``cache_read_tokens`` is a separate
    bucket. Modern ``session_model_usage`` is authoritative because one chat
    may route calls to several models. Older Hermes schemas fall back to the
    aggregate ``sessions`` table.
    """
    state_db = STATE_DB
    if not state_db.exists():
        return {}
    try:
        conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True, timeout=5)
        try:
            for table in ("session_model_usage", "sessions"):
                try:
                    rows = conn.execute(
                        f"""
                        SELECT model,
                               ROUND(SUM(input_tokens)*1.0/SUM(api_call_count), 0),
                               ROUND(SUM(output_tokens)*1.0/SUM(api_call_count), 0),
                               ROUND(SUM(cache_read_tokens)*1.0/SUM(api_call_count), 0)
                        FROM {table}
                        WHERE billing_provider = 'ollama-cloud'
                          AND api_call_count > 0
                          AND (input_tokens > 0 OR cache_read_tokens > 0 OR output_tokens > 0)
                        GROUP BY model
                        """,
                    ).fetchall()
                    if rows or table == "sessions":
                        return {r[0]: (r[1] or 0, r[2] or 0, r[3] or 0) for r in rows if r[0]}
                except sqlite3.Error:
                    if table == "sessions":
                        raise
            return {}
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return {}


def _real_global_token_average() -> tuple | None:
    """Request-weighted canonical average, modern schema first."""
    state_db = STATE_DB
    if not state_db.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True, timeout=5)
        try:
            for table in ("session_model_usage", "sessions"):
                try:
                    row = conn.execute(
                        f"""
                        SELECT ROUND(SUM(input_tokens)*1.0/SUM(api_call_count), 0),
                               ROUND(SUM(output_tokens)*1.0/SUM(api_call_count), 0),
                               ROUND(SUM(cache_read_tokens)*1.0/SUM(api_call_count), 0)
                        FROM {table}
                        WHERE billing_provider = 'ollama-cloud'
                          AND api_call_count > 0
                          AND (input_tokens > 0 OR cache_read_tokens > 0 OR output_tokens > 0)
                        """,
                    ).fetchone()
                    if row and row[0] is not None:
                        return (row[0] or 0, row[1] or 0, row[2] or 0)
                except sqlite3.Error:
                    if table == "sessions":
                        raise
            return None
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return None


def _real_provider_cache_hits() -> dict:
    """Real per-model cache hit rates from Hermes usage accounting.

    Rows for any billing provider (deepseek, openrouter, …) record actual
    native-API usage outside Ollama Cloud. Cache hit rate is
    cache_read / (uncached_input + cache_read) per model — the same canonical
    bucket semantics used everywhere else. Model names are stored with their
    provider prefix (e.g. ``minimax/minimax-m3``, ``deepseek-v4-pro``).
    When the same model was used via several providers, the rate from the
    provider with the most recorded calls wins (largest sample).
    """
    state_db = STATE_DB
    if not state_db.exists():
        return {}
    try:
        conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True, timeout=5)
        try:
            for table in ("session_model_usage", "sessions"):
                try:
                    rows = conn.execute(
                        f"""
                        SELECT billing_provider,
                               model,
                               SUM(api_call_count),
                               SUM(input_tokens),
                               SUM(cache_read_tokens)
                        FROM {table}
                        WHERE billing_provider != 'ollama-cloud'
                          AND api_call_count > 0
                          AND (input_tokens > 0 OR cache_read_tokens > 0 OR output_tokens > 0)
                        GROUP BY billing_provider, model
                        """,
                    ).fetchall()
                    if rows or table == "sessions":
                        best: dict[str, tuple[int, float]] = {}
                        for _provider, model, calls, inp, cache in rows:
                            inp = float(inp or 0)
                            cache = float(cache or 0)
                            if inp + cache > 0:
                                rate = round(cache / (inp + cache) * 100.0, 1)
                                # Merge Ollama build-variant suffixes
                                # (e.g. 'deepseek-v4-flash:0731') into the base
                                # model name so the largest sample wins across
                                # variants — a 1-call row with 0 cache must not
                                # beat 361 calls with 97.8% cache.
                                key = model
                                if ":" in model:
                                    base, _, variant = model.partition(":")
                                    if variant and variant.isdigit():
                                        key = base
                                if key not in best or calls > best[key][0]:
                                    best[key] = (calls, rate)
                        return {m: r for m, (_c, r) in best.items()}
                except sqlite3.Error:
                    if table == "sessions":
                        raise
            return {}
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return {}


def _cache_rate_for(model: str, hits: dict) -> float | None:
    """Match an Ollama model name to a real provider cache rate.

    Uses the same 4-step matching as ``_lookup_price``: exact key, ``:``→``-``
    suffix match, normalized match (strip separators + common tags), then base
    name without the variant. Provider prefixes (``minimax/minimax-m3``) are
    handled by the suffix/normalized steps.
    """
    if not hits:
        return None
    if model in hits:
        return hits[model]
    hyphen_model = model.replace(":", "-")
    for key, val in hits.items():
        if key.endswith(f"/{hyphen_model}"):
            return val

    def _norm(s: str) -> str:
        s = s.lower().replace("-", "").replace(":", "").replace(".", "")
        s = s.replace("_", "")
        for tag in ("it", "instruct", "free", "latest"):
            if s.endswith(tag):
                s = s[: -len(tag)]
        return s

    norm_model = _norm(hyphen_model)
    for key, val in hits.items():
        key_base = key.rsplit("/", 1)[-1] if "/" in key else key
        if _norm(key_base) == norm_model:
            return val
    base = model.split(":", 1)[0]
    if base in hits:
        return hits[base]
    for key, val in hits.items():
        if key.endswith(f"/{base}"):
            return val
    return None


def _fetch_usage(cookie_path: Path) -> dict:
    # Primary: authenticated settings page (real per-model usage-bar shares).
    try:
        cookie = _load_cookie(cookie_path)
        html = _fetch_settings(cookie)
        data = _parse_usage(html)
        if data.get("session_used_pct") is None or data.get("weekly_used_pct") is None:
            raise ValueError("settings page did not contain usage limits")
        data["ok"] = True
        data["fetched_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return data
    except Exception as e:
        print(f"⚠️  Cookie scrape failed ({e}), trying official API fallback…", file=sys.stderr)

    # Fallback: official API (request counts, but no model quota weights).
    api_key = _api_key()
    if not api_key:
        raise RuntimeError("cookie scrape failed and no Ollama API key is configured")
    api_data = _fetch_usage_api(api_key)
    data = _api_to_usage(api_data)
    data["ok"] = True
    data["fetched_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return data


# ── history ─────────────────────────────────────────────────────────────────

def _week_key(_reset_iso: str | None = None) -> str:
    """Current observation week's Monday; never key by a future reset date."""
    dt = datetime.now(timezone.utc)
    monday = dt - timedelta(days=dt.weekday())
    return monday.date().isoformat()


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
            "plan": data.get("plan"),
            "subscription_monthly_cost": data.get("subscription_monthly_cost"),
            "subscription_weekly_equivalent": data.get("subscription_weekly_equivalent"),
            "effective_subscription_cost_per_req": data.get("effective_subscription_cost_per_req"),
            "est_cost_consumed": data.get("subscription_weekly_equivalent"),
            "est_weekly_budget": data.get("subscription_weekly_equivalent"),
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
        now = datetime.now(timezone.utc)
        bucket_epoch = int(now.timestamp()) // (5 * 3600) * (5 * 3600)
        win = datetime.fromtimestamp(bucket_epoch, tz=timezone.utc).isoformat()
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
        lines.append("| Week | Weekly used | Plan equivalent | Top model | Requests |")
        lines.append("|------|-------------|-----------------|-----------|----------|")
        for w in weeks:
            models = w.get("models") or []
            top = max(models, key=lambda m: m.get("share_pct") or 0) if models else {}
            total = sum(m.get("requests") or 0 for m in models)
            cost = (w.get("subscription_weekly_equivalent")
                    if w.get("subscription_weekly_equivalent") is not None
                    else w.get("est_weekly_budget"))
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
    if data.get("reset_unavailable"):
        session_reset_text = weekly_reset_text = "reset not exposed"
    else:
        reset_label = "est. reset in" if data.get("reset_estimated") else "resets in"
        session_reset_text = f"{reset_label} {data.get('session_reset') or '?'}"
        weekly_reset_text = f"{reset_label} {data.get('weekly_reset') or '?'}"
    print(f"📊 Ollama Cloud — {plan} plan")
    print(f"   Session:  {s:.1f}% used · {session_reset_text}" if s is not None else "   Session:  n/a")
    print(f"   Weekly:   {w:.1f}% used · {weekly_reset_text}" if w is not None else "   Weekly:   n/a")

    # ── API equivalent cost summary ──
    session_api = data.get("api_session_total") if data.get("api_session_total") is not None else data.get("api_session_known_total")
    weekly_api = data.get("api_window_total") if data.get("api_window_total") is not None else data.get("api_known_window_total")
    if session_api is not None or weekly_api is not None:
        print()
        if session_api is not None:
            print(f"   💰 Session: ${session_api:.4f} API")
            for m in (data.get("session_models") or []):
                sc = m.get("api_session_cost")
                if sc is not None:
                    line = f"      {m['model']:<23} ${sc:.4f}"
                    scc = m.get("api_session_cost_cached")
                    if scc is not None:
                        rc = m.get("api_real_cache_pct")
                        line += f"  (with cache {rc:.0f}%: ${scc:.4f})" if rc is not None else f"  (with cache: ${scc:.4f})"
                    print(line)
        if weekly_api is not None:
            print(f"   💰 Weekly:  ${weekly_api:.4f} API")
            for m in (data.get("weekly_models") or []):
                wc = m.get("api_weekly_cost")
                if wc is not None:
                    line = f"      {m['model']:<23} ${wc:.4f}"
                    wcc = m.get("api_weekly_cost_cached")
                    if wcc is not None:
                        rc = m.get("api_real_cache_pct")
                        line += f"  (with cache {rc:.0f}%: ${wcc:.4f})" if rc is not None else f"  (with cache: ${wcc:.4f})"
                    print(line)
        lt_be = _lifetime_break_even(_load_history(HISTORY_FILE))
        if lt_be.get("api_lifetime_total") is not None:
            line = f"   💰 Lifetime: ${lt_be['api_lifetime_total']:.4f} API"
            if lt_be.get("api_lifetime_total_cached") is not None:
                line += f"  (with cache: ${lt_be['api_lifetime_total_cached']:.4f})"
            print(line)
            for m in lt_be.get("models") or []:
                lc = m.get("api_lifetime_cost")
                if lc is not None:
                    line = f"      {m['model']:<23} ${lc:.4f}"
                    lcc = m.get("api_lifetime_cost_cached")
                    if lcc is not None:
                        rc = m.get("api_real_cache_pct")
                        line += f"  (with cache {rc:.0f}%: ${lcc:.4f})" if rc is not None else f"  (with cache: ${lcc:.4f})"
                    print(line)
    print()

    sm = data.get("session_models") or []
    wm = data.get("weekly_models") or []
    share_label = "usage bar" if data.get("share_basis") == "ollama_usage_bar" else "req share"
    if sm:
        print("   Session per model:")
        for model in sm:
            print(f"     {model['model']:<25} {model['requests']:>5} req · {model.get('share_pct', 0):.1f}% {share_label}")
        print()

    if wm:
        print("   Weekly per model:")
        for model in wm:
            cpr = model.get("api_cost_per_req")
            per_m = model.get("api_effective_per_1m")
            if cpr is not None and per_m is not None:
                api_text = f"${cpr:.4f}/req · ${per_m:.3f}/1M effective"
                cache_mark = "" if model.get("api_cache_price_published") else "*"
                rates = (f"input ${model['api_input_per_1m']:.3f} · cache ${model['api_cache_per_1m']:.3f}{cache_mark} "
                         f"· output ${model['api_output_per_1m']:.3f} /1M")
            else:
                api_text, rates = "API price n/a", None
            print(f"     {model['model']:<25} {model['requests']:>5} req · {model.get('share_pct', 0):.1f}% {share_label} · {api_text}")
            if rates:
                print(f"       {rates}")
        print(f"     API price coverage: {data.get('api_price_coverage_pct', 0):.2f}% of requests")
        if any(m.get("api_cache_price_published") is False for m in wm):
            print("     * no cache discount published; normal input rate used")

    api_total = data.get("api_window_total")
    api_known_total = data.get("api_known_window_total")
    comparison_total = api_total if api_total is not None else api_known_total
    if comparison_total is not None:
        priced = [m for m in wm if m.get("api_effective_per_1m") is not None]
        print()
        print("   Plan vs API — per-model effective $/1M token comparison")
        print(f"     Source: {'settings-page usage bars' if data.get('source') == 'cookie' else 'API fallback (Ollama price unavailable)'}")
        if priced:
            print()
            for m in priced:
                plan_rate = f"${m['plan_effective_per_1m']:.4f}" if m.get("plan_effective_per_1m") is not None else "n/a"
                pct = f"{m['plan_pct_of_api']:.0f}%" if m.get("plan_pct_of_api") is not None else "n/a"
                in_p  = f"${m['api_input_per_1m']:.4f}" if m.get("api_input_per_1m") else "?"
                cache_p = f"${m['api_cache_per_1m']:.4f}" if m.get("api_cache_per_1m") else "n/a"
                cache_note = "" if m.get("api_cache_price_published") else "*"
                out_p = f"${m['api_output_per_1m']:.4f}" if m.get("api_output_per_1m") else "?"
                print(f"     {m['model']:<25} Ollama {plan_rate}  ·  API ${m['api_effective_per_1m']:.4f}  ·  {pct} of API")
                print(f"       API rates:  in {in_p}  ·  cache {cache_p}{cache_note}  ·  out {out_p} /1M")
        print(f"     Ollama $/1M uses fixed 7-day plan price, observed quota fraction, normalized usage-bar share, and estimated tokens.")
        print(f"     * = cache discount not published; input rate shown.")

    # ── Plan vs API with cache: same comparison, API priced at real cache rate ──
    cached = [m for m in wm if m.get("api_effective_per_1m_cached") is not None]
    if cached:
        print()
        print("   Plan vs API with cache — API side priced at your real provider cache rate")
        print()
        for m in cached:
            plan_rate = f"${m['plan_effective_per_1m']:.4f}" if m.get("plan_effective_per_1m") is not None else "n/a"
            pct = f"{m['plan_pct_of_api_cached']:.0f}%" if m.get("plan_pct_of_api_cached") is not None else "n/a"
            real_rate = m.get("api_real_cache_pct")
            rate_txt = f"{real_rate:.0f}%" if real_rate is not None else "?"
            print(f"     {m['model']:<25} Ollama {plan_rate}  ·  API ({rate_txt} cache) ${m['api_effective_per_1m_cached']:.4f}  ·  {pct} of API")
        print(f"     Same as Plan vs API, but the API $/1M uses prompt tokens split by your real provider cache rate (largest sample wins, OpenRouter fallback).")

    # ── Cache break-even: at what cache hit rate does the API get cheaper? ──
    be_models = [m for m in wm if m.get("api_break_even_cache_pct") is not None]
    if be_models:
        print()
        print("   Cache break-even — when the API becomes cheaper than the subscription")
        print()
        for m in be_models:
            be = m.get("api_break_even_cache_pct")
            cur = m.get("cache_hit_pct")
            real_rate = m.get("api_real_cache_pct")
            if be > 100:
                line = f">100% (plan always cheaper)"
            elif be <= 0:
                line = f"0% (API always cheaper)"
            else:
                line = f"API cheaper above {be:.0f}% cache hit"
                if cur is not None:
                    line += f" · you: {cur:.0f}%"
            if real_rate is not None:
                line += f" · real API: {real_rate:.0f}%"
            print(f"     {m['model']:<25} {line}")
        print(f"     Above the break-even the API is cheaper for that model; below it, the subscription wins.")

    # ── Lifetime break-even & price comparison (aggregated saved weeks) ──
    lt_be = _lifetime_break_even(_load_history(HISTORY_FILE))
    if lt_be.get("models"):
        print()
        print("   Lifetime break-even & price comparison")
        print(f"     Aggregated over {lt_be.get('weeks_count', 0)} saved week(s) · {lt_be.get('total_requests', 0)} requests · ${lt_be.get('subscription_total_equivalent', 0):.2f} plan equivalent")
        print()
        for m in lt_be["models"]:
            if m.get("api_break_even_cache_pct") is None and m.get("api_effective_per_1m") is None:
                continue
            be = m.get("api_break_even_cache_pct")
            real_rate = m.get("api_real_cache_pct")
            if be is None:
                be_line = "n/a"
            elif be > 100:
                be_line = ">100% (plan always cheaper)"
            elif be <= 0:
                be_line = "0% (API always cheaper)"
            else:
                be_line = f"API cheaper above {be:.0f}% cache hit"
            if real_rate is not None:
                be_line += f" · real API: {real_rate:.0f}%"
            api_line = f"${m['api_effective_per_1m']:.4f}/1M" if m.get("api_effective_per_1m") is not None else "API n/a"
            print(f"     {m['model']:<25} {m.get('requests', 0):>5} req · API {api_line} · {be_line}")
        print(f"     Prices: {lt_be.get('price_source') or 'builtin'} · Tokens: {lt_be.get('token_source') or 'unknown'}")
        print("     Break-even = cache hit rate at which the API would have been cheaper over the recorded period.")

    # ── API usage percentage: compact Ollama/API ratio ──
    if data.get('source') == 'cookie':
        pct_models = [m for m in wm if m.get('plan_pct_of_api') is not None]
        if pct_models:
            print()
            print("   API usage percentage — how much of the API price you pay")
            print(f"     Source: settings-page usage bars")
            print()
            for m in pct_models:
                pct = m['plan_pct_of_api']
                flag = " ✓ green" if pct < 20 else (" ⚠ yellow" if pct > 80 else "")
                print(f"     {m['model']:<25} {pct:.0f}% of API{flag}")
            print(f"     Lower = better subscription value. <20% green, >80% yellow.")

    # ── Price comparison: raw $/1M without the percentage column ──
    price_models = [m for m in wm if m.get('api_effective_per_1m') is not None]
    if price_models:
        print()
        print("   Price comparison — effective $/1M tokens")
        source_note = 'settings-page usage bars' if data.get('source') == 'cookie' else 'API fallback — per-model Ollama price unavailable'
        print(f"     Source: {source_note}")
        print()
        for m in price_models:
            ollama_p = f"${m['plan_effective_per_1m']:.4f}" if m.get('plan_effective_per_1m') is not None else 'n/a'
            print(f"     {m['model']:<25} Ollama {ollama_p}/1M  ·  API ${m['api_effective_per_1m']:.4f}/1M")
        print(f"     Ollama: subscription allocated by usage-bar share. API: pay-per-token estimate.")


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
    """Aggregate fixed-plan economics without inventing per-model cost."""
    if not weeks:
        return {"ok": True, "weeks_count": 0, "models": []}
    model_reqs = {}
    total_plan_equivalent = 0.0
    for record in weeks:
        weekly = record.get("subscription_weekly_equivalent")
        if weekly is None:
            weekly = record.get("est_weekly_budget")
        if weekly is None:
            weekly = record.get("est_cost_consumed") or 0.0
        total_plan_equivalent += float(weekly or 0.0)
        for row in record.get("models") or []:
            model = row.get("model")
            reqs = int(row.get("requests") or 0)
            if model and reqs > 0:
                model_reqs[model] = model_reqs.get(model, 0) + reqs
    total_reqs = sum(model_reqs.values())
    effective = total_plan_equivalent / total_reqs if total_reqs else 0.0
    return {
        "ok": True,
        "weeks_count": len(weeks),
        "total_requests": total_reqs,
        "subscription_total_equivalent": round(total_plan_equivalent, 4),
        "effective_subscription_cost_per_req": round(effective, 6),
        "est_total_cost": round(total_plan_equivalent, 4),
        "est_avg_cost_per_req": round(effective, 6),
        "models": [{"model": m, "requests": r} for m, r in sorted(model_reqs.items(), key=lambda item: item[1], reverse=True)],
    }


def _lifetime_break_even(weeks: list) -> dict:
    """Aggregate cache break-even + plan/API economics across all saved weeks.

    Mirrors the Hermes backend ``_lifetime_break_even`` so both surfaces agree.
    Each saved weekly record carries per-model request counts and usage-bar
    shares; we aggregate those, attach current resolved API prices and token
    profiles, and reuse the same break-even math as the live view.
    """
    if not weeks:
        return {"ok": True, "weeks_count": 0, "models": []}
    model_agg: dict[str, dict] = {}
    total_plan_equivalent = 0.0
    for record in weeks:
        weekly = record.get("subscription_weekly_equivalent")
        if weekly is None:
            weekly = record.get("est_weekly_budget")
        if weekly is None:
            weekly = record.get("est_cost_consumed") or 0.0
        weekly = float(weekly or 0.0)
        total_plan_equivalent += weekly
        week_rows = [r for r in (record.get("models") or []) if int(r.get("requests") or 0) > 0]
        if not week_rows:
            continue
        # Same normalization as the live view: positive usage-bar shares only.
        share_total = sum(
            max(float(r.get("share_pct") or 0.0), 0.0) for r in week_rows
        )
        weekly_used_fraction = max(float(record.get("weekly_used_pct") or 0.0), 0.0) / 100.0
        for row in week_rows:
            model = row.get("model")
            reqs = int(row.get("requests") or 0)
            share = float(row.get("share_pct") or 0.0)
            if not model or reqs <= 0:
                continue
            agg = model_agg.setdefault(model, {"requests": 0, "allocated": 0.0, "share_weighted": 0.0})
            agg["requests"] += reqs
            agg["share_weighted"] += share * reqs
            if share > 0 and share_total > 0 and weekly_used_fraction > 0:
                agg["allocated"] += (
                    weekly * weekly_used_fraction * (share / share_total)
                )

    api_prices, price_source = _resolve_api_prices()
    overrides = _load_manual_price_overrides()
    token_avgs, token_source = _resolve_token_averages(overrides)
    fallback_avg = _real_global_token_average()
    fallback_basis = "request-weighted all-model history" if fallback_avg else None
    if fallback_avg is None and token_avgs:
        vals = list(token_avgs.values())
        fallback_avg = (
            round(sum(v[0] for v in vals) / len(vals)),
            round(sum(v[1] for v in vals) / len(vals)),
            round(sum(v[2] for v in vals) / len(vals)),
        )
        fallback_basis = "cross-model mean"

    total_reqs = sum(a["requests"] for a in model_agg.values())
    models = []
    for model, agg in sorted(model_agg.items(), key=lambda kv: kv[1]["requests"], reverse=True):
        entry: dict = {
            "model": model,
            "requests": agg["requests"],
            "share_pct": round(agg["share_weighted"] / agg["requests"], 1) if agg["requests"] else None,
        }
        avg = token_avgs.get(model)
        if avg:
            basis = "model history"
        elif fallback_avg:
            avg = fallback_avg
            basis = fallback_basis
        else:
            avg = (1000.0, 500.0, 0.0)
            basis = "fixed fallback"
        entry["token_estimate_basis"] = basis
        in_t = max(float(avg[0] or 0), 0.0)
        out_t = max(float(avg[1] or 0), 0.0)
        cache_t = max(float(avg[2] or 0), 0.0)
        prompt_t = in_t + cache_t
        tokens_per_req = prompt_t + out_t
        entry["avg_uncached_input_tokens"] = round(in_t)
        entry["avg_cache_tokens"] = round(cache_t)
        entry["avg_out_tokens"] = round(out_t)
        entry["total_estimated_tokens"] = round(tokens_per_req * agg["requests"])
        entry["api_effective_per_1m"] = None
        entry["api_break_even_cache_pct"] = None
        entry["api_real_cache_pct"] = None

        prices = _lookup_price(api_prices, model)
        if prices and tokens_per_req > 0:
            p_in, p_out, p_cache_published = prices
            p_in = float(p_in)
            p_out = float(p_out)
            p_cache = p_in if p_cache_published is None else float(p_cache_published)
            per_req = (in_t / 1e6) * p_in + (cache_t / 1e6) * p_cache + (out_t / 1e6) * p_out
            entry["api_effective_per_1m"] = round(per_req * 1e6 / tokens_per_req, 6)
            entry["api_input_per_1m"] = round(p_in, 6)
            entry["api_cache_per_1m"] = round(p_cache, 6)
            entry["api_output_per_1m"] = round(p_out, 6)
            entry["api_cache_price_published"] = p_cache_published is not None
            entry["api_lifetime_cost"] = round(per_req * agg["requests"], 4)

            # Cache-aware lifetime cost using the real provider cache rate.
            real_cache = _cache_rate_for(model, _real_provider_cache_hits())
            entry["api_real_cache_pct"] = real_cache
            if real_cache is not None and prompt_t > 0:
                uncached_t = prompt_t * (1.0 - real_cache / 100.0)
                cached_t = prompt_t * (real_cache / 100.0)
                per_req_cached = (
                    (uncached_t / 1e6) * p_in
                    + (cached_t / 1e6) * p_cache
                    + (out_t / 1e6) * p_out
                )
                entry["api_lifetime_cost_cached"] = round(per_req_cached * agg["requests"], 4)
            else:
                entry["api_lifetime_cost_cached"] = None

            # Per-model subscription allocation: the model's own share of the
            # weekly plan fee across the recorded weeks, per request. This is
            # the same math as the live view — never a blended average.
            sub_per_req = (agg["allocated"] / agg["requests"]) if agg["requests"] else 0.0
            if sub_per_req > 0 and prompt_t > 0:
                api_at_0 = (prompt_t * p_in + out_t * p_out) / 1e6
                if p_cache >= p_in:
                    entry["api_break_even_cache_pct"] = 0.0 if sub_per_req >= api_at_0 else 100.0
                else:
                    denom = prompt_t * (p_cache - p_in)
                    h = (sub_per_req * 1e6 - prompt_t * p_in - out_t * p_out) / denom
                    entry["api_break_even_cache_pct"] = round(max(h, 0.0) * 100.0, 1)
        models.append(entry)

    priced = [m for m in models if m.get("api_lifetime_cost") is not None]
    return {
        "ok": True,
        "weeks_count": len(weeks),
        "total_requests": total_reqs,
        "subscription_total_equivalent": round(total_plan_equivalent, 4),
        "api_lifetime_total": round(sum(m["api_lifetime_cost"] for m in priced), 4) if priced else None,
        "api_lifetime_total_cached": round(
            sum(m["api_lifetime_cost_cached"] for m in priced if m.get("api_lifetime_cost_cached") is not None), 4
        ) if any(m.get("api_lifetime_cost_cached") is not None for m in priced) else None,
        "price_source": price_source,
        "token_source": token_source,
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
    weekly_equiv = data.get("subscription_weekly_equivalent", 0)
    effective_sub_cpr = data.get("effective_subscription_cost_per_req", 0)

    sm = data.get("session_models") or []
    wm = data.get("weekly_models") or []
    lt = _lifetime_from_history(history)
    lt_be = _lifetime_break_even(history)

    api_total = data.get("api_window_total")
    api_known_total = data.get("api_known_window_total")
    api_comparison_total = api_total if api_total is not None else api_known_total
    api_comparison_complete = api_total is not None
    api_monthly = data.get("api_monthly_proj")
    plan_monthly = data.get("subscription_monthly_cost", data.get("ollama_monthly", 20.0))
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
            cost = (wk.get("subscription_weekly_equivalent")
                    if wk.get("subscription_weekly_equivalent") is not None
                    else wk.get("est_weekly_budget"))
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

    # ── subscription vs API headline ──
    savings_html = ""
    if api_comparison_total is not None:
        difference = api_comparison_total - weekly_equiv
        if api_comparison_complete and difference >= 0:
            headline = f"Plan equivalent is <b>${difference:.2f}</b> below API estimate"
        elif api_comparison_complete:
            headline = f"API estimate is <b>${abs(difference):.2f}</b> below plan equivalent so far"
        elif difference >= 0:
            headline = f"Plan equivalent is at least <b>${difference:.2f}</b> below the known API subtotal"
        else:
            headline = "API comparison is incomplete"
        comparison_label = "API estimate" if api_comparison_complete else "Known API subtotal"
        coverage_note = "" if api_comparison_complete else f" · {data.get('api_price_coverage_pct', 0):.2f}% price coverage"
        savings_html = (
            "<div class='savings'>"
            f"<div class='savings-big'>{headline}</div>"
            f"<div class='savings-sub'>{comparison_label} ${api_comparison_total:.2f} · {plan} 7-day equivalent ${weekly_equiv:.2f}{coverage_note}</div>"
            "</div>"
        )

    # ── limits ──
    if data.get("reset_unavailable"):
        session_reset_label = weekly_reset_label = "reset not exposed"
    else:
        reset_prefix = "est. reset in" if data.get("reset_estimated") else "resets in"
        session_reset_label = f"{reset_prefix} {s_reset}"
        weekly_reset_label = f"{reset_prefix} {w_reset}"
    limits_html = collapsible(
        "Limits",
        f"<div class='limit-row'><span>Session <span class='dim'>· {session_reset_label}</span></span>"
        f"<span class='num' style='color:{_pct_color(s)}'>{s:.1f}% used</span></div>"
        f"<div class='limit-row'><span>Weekly <span class='dim'>· {weekly_reset_label}</span></span>"
        f"<span class='num' style='color:{_pct_color(w)}'>{w:.1f}% used</span></div>",
        open_=True,
    )

    # ── session / weekly per model ──
    share_heading = "Usage-bar share" if data.get("share_basis") == "ollama_usage_bar" else "Request share"
    session_table = (
        f"<table><thead><tr><th>Model</th><th class='num'>Requests</th><th class='num'>{share_heading}</th></tr></thead>"
        f"<tbody>{model_rows(sm)}</tbody></table>"
    )
    weekly_table = (
        f"<table><thead><tr><th>Model</th><th class='num'>Requests</th><th class='num'>{share_heading}</th></tr></thead>"
        f"<tbody>{model_rows(wm)}</tbody></table>"
    )

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
            cache_rows + "<div class='cost'>Historical Hermes averages, not current-window Ollama telemetry. Missing cache prices use the normal input rate.</div>",
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
            "Estimated token volume",
            tok_rows + "<div class='cost'>Current quota-window requests × historical average tokens/request</div>",
        )
    else:
        tok_html = ""

    # ── API equivalent cost ──
    api_known = [m for m in wm if m.get("api_cost_per_req") is not None and m.get("api_effective_per_1m") is not None]
    if api_known:
        api_rows = "".join(
            f"<div class='limit-row'><span>{m['model']}</span>"
            f"<span class='num'>${m['api_cost_per_req']:.4f}/req · ${m['api_effective_per_1m']:.3f}/1M effective</span></div>"
            f"<div class='cost'>input ${m['api_input_per_1m']:.3f} · cache ${m['api_cache_per_1m']:.3f}{'' if m.get('api_cache_price_published') else '*'} · output ${m['api_output_per_1m']:.3f} /1M</div>"
            for m in api_known
        )
        coverage = data.get("api_price_coverage_pct")
        unpriced = data.get("api_unpriced_models") or []
        incomplete = (f"<div class='cost'>Unpriced: {', '.join(unpriced)}. The known subtotal is a lower bound; missing prices can only increase it.</div>" if unpriced else "")
        cache_note = ("<div class='cost'>* no cache discount published; normal input rate used</div>"
                      if any(m.get("api_cache_price_published") is False for m in api_known) else "")
        # API cost totals
        session_total = data.get("api_session_total") if data.get("api_session_total") is not None else data.get("api_session_known_total")
        weekly_total = data.get("api_window_total") if data.get("api_window_total") is not None else data.get("api_known_window_total")
        totals_html = ""
        if session_total is not None or weekly_total is not None:
            parts = []
            if session_total is not None:
                parts.append(f"Session: <b>${session_total:.4f}</b>")
                for m in (data.get("session_models") or []):
                    sc = m.get("api_session_cost")
                    if sc is not None:
                        line = f"<span class='dim'>  {m['model']} — ${sc:.4f}</span>"
                        scc = m.get("api_session_cost_cached")
                        if scc is not None:
                            rc = m.get("api_real_cache_pct")
                            line = line[:-7] + f" · with cache {rc:.0f}%: ${scc:.4f}</span>" if rc is not None else line[:-7] + f" · with cache: ${scc:.4f}</span>"
                        parts.append(line)
            if weekly_total is not None:
                parts.append(f"Weekly:  <b>${weekly_total:.4f}</b>")
                for m in (data.get("weekly_models") or []):
                    wc = m.get("api_weekly_cost")
                    if wc is not None:
                        line = f"<span class='dim'>  {m['model']} — ${wc:.4f}</span>"
                        wcc = m.get("api_weekly_cost_cached")
                        if wcc is not None:
                            rc = m.get("api_real_cache_pct")
                            line = line[:-7] + f" · with cache {rc:.0f}%: ${wcc:.4f}</span>" if rc is not None else line[:-7] + f" · with cache: ${wcc:.4f}</span>"
                        parts.append(line)
            if lt_be.get("api_lifetime_total") is not None:
                lt_line = f"Lifetime: <b>${lt_be['api_lifetime_total']:.4f}</b>"
                if lt_be.get("api_lifetime_total_cached") is not None:
                    lt_line += f" <span class='dim'>(with cache: ${lt_be['api_lifetime_total_cached']:.4f})</span>"
                parts.append(lt_line)
                for m in lt_be.get("models") or []:
                    lc = m.get("api_lifetime_cost")
                    if lc is not None:
                        line = f"<span class='dim'>  {m['model']} — ${lc:.4f}</span>"
                        lcc = m.get("api_lifetime_cost_cached")
                        if lcc is not None:
                            rc = m.get("api_real_cache_pct")
                            line = line[:-7] + f" · with cache {rc:.0f}%: ${lcc:.4f}</span>" if rc is not None else line[:-7] + f" · with cache: ${lcc:.4f}</span>"
                        parts.append(line)
            totals_html = "<div class='savings'>" + "<br>".join(parts) + "</div>"

        api_html = collapsible(
            "API equivalent cost",
            totals_html
            + "<div class='cost'>Estimated API cost of tokens already consumed on Ollama Cloud this window.</div>",
        )
    else:
        api_html = ""

    # ── Ollama vs API: per-model $/1M comparison ──
    if api_comparison_total is not None:
        priced = [m for m in (data.get("weekly_models") or [])
                  if m.get("api_effective_per_1m") is not None]
        model_cards = ""
        for m in priced:
            in_p  = f"${m['api_input_per_1m']:.4f}" if m.get("api_input_per_1m") else "?"
            cache_p = f"${m['api_cache_per_1m']:.4f}" if m.get("api_cache_per_1m") else "n/a"
            cache_note = "" if m.get("api_cache_price_published") else "*"
            out_p = f"${m['api_output_per_1m']:.4f}" if m.get("api_output_per_1m") else "?"
            ollama_p = f"${m['plan_effective_per_1m']:.4f}" if m.get("plan_effective_per_1m") is not None else "n/a"
            ratio = f"{m['plan_pct_of_api']:.0f}%" if m.get("plan_pct_of_api") is not None else "n/a"
            model_cards += (
                f"<tr><td><b>{m['model']}</b></td>"
                f"<td class='num'>{ollama_p}</td>"
                f"<td class='num'>${m['api_effective_per_1m']:.4f}</td>"
                f"<td class='num'>{ratio}</td></tr>"
                f"<tr class='dim'><td>&nbsp;&nbsp;API input / cache / output</td>"
                f"<td colspan='3' class='num'>{in_p} / {cache_p}{cache_note} / {out_p} /1M</td></tr>"
            )
        source_note = (
            "Settings-page usage bars (cookie)"
            if data.get("source") == "cookie" else
            "API fallback — per-model Ollama prices unavailable"
        )
        rate_table = (
            f"<div class='cost'><b>{source_note}</b></div>"
            + (
                "<table><thead><tr><th>Model</th><th class='num'>Ollama $/1M</th><th class='num'>API $/1M</th><th class='num'>Ollama/API</th></tr></thead>"
                f"<tbody>{model_cards}</tbody></table>"
                if model_cards else ""
            )
            + "<div class='cost'>Ollama $/1M uses fixed 7-day plan price, observed quota fraction, normalized usage-bar share, and estimated model tokens. "
            "API component prices are real published rates. * = cache discount not published; input rate shown.</div>"
        )
        eff_html = collapsible(
            "Plan vs API",
            rate_table,
            open_=True,
        )

        # ── Plan vs API with cache: same comparison, API priced at real cache rate ──
        cached_html = ""
        cached = [m for m in (data.get("weekly_models") or [])
                  if m.get("api_effective_per_1m_cached") is not None]
        if cached:
            cached_cards = ""
            for m in cached:
                ollama_p = f"${m['plan_effective_per_1m']:.4f}" if m.get("plan_effective_per_1m") is not None else "n/a"
                ratio = f"{m['plan_pct_of_api_cached']:.0f}%" if m.get("plan_pct_of_api_cached") is not None else "n/a"
                real_rate = m.get("api_real_cache_pct")
                rate_txt = f"{real_rate:.0f}%" if real_rate is not None else "?"
                cached_cards += (
                    f"<tr><td><b>{m['model']}</b></td>"
                    f"<td class='num'>{ollama_p}</td>"
                    f"<td class='num'>${m['api_effective_per_1m_cached']:.4f} ({rate_txt} cache)</td>"
                    f"<td class='num'>{ratio}</td></tr>"
                )
            cached_html = collapsible(
                "Plan vs API with cache",
                "<div class='cost'>Same as Plan vs API, but the API $/1M uses prompt tokens split by your real provider cache rate (largest sample wins, OpenRouter fallback).</div>"
                + (
                    "<table><thead><tr><th>Model</th><th class='num'>Ollama $/1M</th><th class='num'>API $/1M (cache)</th><th class='num'>Ollama/API</th></tr></thead>"
                    f"<tbody>{cached_cards}</tbody></table>"
                ),
                open_=False,
            )

        # ── Cache break-even compact HTML ──
        be_html = ""
        be_models = [m for m in (data.get("weekly_models") or [])
                     if m.get("api_break_even_cache_pct") is not None]
        if be_models:
            be_rows = ""
            for m in be_models:
                be = m.get("api_break_even_cache_pct")
                cur = m.get("cache_hit_pct")
                if be > 100:
                    line = "&gt;100% (plan always cheaper)"
                elif be <= 0:
                    line = "0% (API always cheaper)"
                else:
                    line = f"API cheaper above {be:.0f}% cache hit"
                    if cur is not None:
                        line += f" · you: {cur:.0f}%"
                be_rows += f"<div class='limit-row'><span>{m['model']}</span><span class='num'>{line}</span></div>"
            be_html = collapsible(
                "Cache break-even",
                "<div class='cost'>Cache hit rate at which the pay-per-token API becomes cheaper than the subscription, per model</div>"
                + be_rows
                + "<div class='cost'>Above the break-even the API is cheaper for that model; below it, the subscription wins.</div>",
            )

        # ── API usage percentage compact HTML ──
        pct_html = ""
        if data.get("source") == "cookie":
            pct_rows = ""
            for m in priced:
                pct_val = m.get("plan_pct_of_api")
                if pct_val is None:
                    continue
                color = "#22c55e" if pct_val < 20 else ("#f59e0b" if pct_val > 80 else "var(--secondary)")
                pct_rows += (
                    f"<div class='limit-row'><span>{m['model']}</span>"
                    f"<span class='num' style='color:{color}'><b>{pct_val:.0f}%</b> of API</span></div>"
                )
            if pct_rows:
                pct_html = collapsible(
                    "API usage percentage",
                    f"<div class='cost'>How much of the API pay-per-token price you effectively pay via the subscription</div>"
                    + pct_rows
                    + "<div class='cost'>Lower = better subscription value. <20% green, >80% yellow.</div>",
                )

        # ── Price comparison compact HTML ──
        price_html = ""
        if priced:
            price_rows = ""
            for m in priced:
                ollama_p = f"${m['plan_effective_per_1m']:.4f}" if m.get("plan_effective_per_1m") is not None else "n/a"
                price_rows += (
                    f"<div class='limit-row'><span>{m['model']}</span>"
                    f"<span class='num'>Ollama {ollama_p} · API ${m['api_effective_per_1m']:.4f}</span></div>"
                    f"<div class='cost'>/1M tokens estimated</div>"
                )
            price_note = "Effective $/1M — subscription allocation vs pay-per-token API" if data.get("source") == "cookie" else "API fallback — per-model Ollama price unavailable"
            price_html = collapsible(
                "Price comparison",
                f"<div class='cost'>{price_note}</div>"
                + price_rows
                + "<div class='cost'>Ollama: subscription allocated by usage-bar share. API: pay-per-token estimate.</div>",
            )
    else:
        eff_html = ""
        cached_html = ""
        be_html = ""
        pct_html = ""
        price_html = ""

    # ── monthly projection ──
    if api_monthly is not None:
        difference = api_monthly - plan_monthly
        label = "Subscription savings / month" if difference >= 0 else "API cheaper / month"
        monthly_html = collapsible(
            "Monthly projection",
            f"<div class='limit-row'><span>API projected / month</span><span class='num'>${api_monthly:.0f}</span></div>"
            f"<div class='limit-row'><span>{plan} / month</span><span class='num'>${plan_monthly:.0f}</span></div>"
            f"<div class='limit-row total-row'><span>{label}</span><span class='num'><b>${abs(difference):.0f}</b></span></div>"
            "<div class='cost'>Assumes the current quota-window request mix repeats 4.348× per month.</div>",
        )
    else:
        monthly_html = ""

    # ── weekly history ──
    history_html = collapsible(
        "Weekly history",
        "<table><thead><tr><th>Week</th><th class='num'>Used</th><th class='num'>Plan equivalent</th><th>Top model</th>"
        "<th class='num'>Requests</th></tr></thead>"
        f"<tbody>{history_rows(history)}</tbody></table>"
        "<div class='cost'>Weekly snapshots — kept locally, survives Ollama resets</div>",
    )

    # ── sessions ──
    sessions_html = collapsible("All 5h sessions", session_blocks(sessions))

    # ── lifetime break-even & price comparison ──
    ltbe_html = ""
    if lt_be.get("models"):
        ltbe_rows = ""
        for m in lt_be["models"]:
            if m.get("api_break_even_cache_pct") is None and m.get("api_effective_per_1m") is None:
                continue
            be = m.get("api_break_even_cache_pct")
            real_rate = m.get("api_real_cache_pct")
            if be is None:
                be_line = "n/a"
            elif be > 100:
                be_line = "&gt;100% (plan always cheaper)"
            elif be <= 0:
                be_line = "0% (API always cheaper)"
            else:
                be_line = f"API cheaper above {be:.0f}% cache hit"
            if real_rate is not None:
                be_line += f" · real API: {real_rate:.0f}%"
            api_line = f"${m['api_effective_per_1m']:.4f}/1M" if m.get("api_effective_per_1m") is not None else "API n/a"
            ltbe_rows += (
                f"<div class='limit-row'><span>{m['model']} · {m.get('requests', 0)} req</span>"
                f"<span class='num'>API {api_line} · {be_line}</span></div>"
            )
        ltbe_html = collapsible(
            "Lifetime break-even & price comparison",
            f"<div class='cost'>Aggregated over {lt_be.get('weeks_count', 0)} saved week(s) · {lt_be.get('total_requests', 0)} requests · ${lt_be.get('subscription_total_equivalent', 0):.2f} plan equivalent</div>"
            + ltbe_rows
            + f"<div class='cost'>Prices: {lt_be.get('price_source') or 'builtin'} · Tokens: {lt_be.get('token_source') or 'unknown'}. Break-even = cache hit rate at which the API would have been cheaper over the recorded period.</div>",
        )

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
    --accent: #3b82f6; --accent-contrast: #0a0a0a;
    --green: #22c55e; --yellow: #f59e0b; --red: #ef4444;
  }}
  [data-theme="light"] {{
    --bg: #fafafa; --card: #ffffff; --border: #e4e4e7;
    --text: #18181b; --secondary: #52525b; --quaternary: #a1a1aa;
    --accent: #2563eb; --accent-contrast: #ffffff;
    --green: #16a34a; --yellow: #d97706; --red: #dc2626;
  }}
  @media (prefers-color-scheme: light) {{
    :root:not([data-theme="dark"]) {{
      --bg: #fafafa; --card: #ffffff; --border: #e4e4e7;
      --text: #18181b; --secondary: #52525b; --quaternary: #a1a1aa;
      --accent: #2563eb; --accent-contrast: #ffffff;
      --green: #16a34a; --yellow: #d97706; --red: #dc2626;
    }}
  }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ background: var(--bg); color: var(--text); font: 14px/1.5 -apple-system,BlinkMacSystemFont,system-ui,sans-serif; padding: 20px; max-width: 800px; margin: 0 auto; }}
  .topbar {{ display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }}
  .topbar-right {{ display: flex; align-items: center; gap: 8px; }}
  .theme-btn {{ border: 1px solid var(--border); background: var(--card); color: var(--secondary); border-radius: 6px; padding: 3px 8px; cursor: pointer; font-size: 0.8rem; }}
  .theme-btn:hover {{ color: var(--text); border-color: var(--accent); }}
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
  .update-btn {{ display: inline-block; margin-left: 8px; padding: 3px 10px; border: 1px solid var(--accent); border-radius: 6px; background: transparent; color: var(--accent); font-size: 0.7rem; cursor: pointer; }}
  .update-btn:hover {{ background: var(--accent); color: var(--accent-contrast); }}
  .update-btn:disabled {{ opacity: 0.6; cursor: default; }}
</style>
</head>
<body>
  <div class="topbar">
    <div>
      <h1>📊 Ollama Cloud Usage Stats</h1>
      <div class="sub">{plan} plan · generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</div>
    </div>
    <div class="topbar-right">
      <span id="update-slot"></span>
      <button id="theme-btn" class="theme-btn" type="button" title="Toggle dark/light theme">🌓</button>
    </div>
  </div>

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

  {cache_html}
  {tok_html}
  {api_html}
  {eff_html}
  {cached_html}
  {be_html}
  {pct_html}
  {price_html}
  {monthly_html}
  {history_html}
  {sessions_html}
  {ltbe_html}

  <footer>
    Ollama Cloud Usage Stats · <a href="https://github.com/Kosello/ollama-cloud-watch">github.com/Kosello/ollama-cloud-watch</a>
  </footer>
  <script>
  // Theme toggle — persisted in localStorage, defaults to system preference.
  (function () {{
    var btn = document.getElementById('theme-btn');
    if (!btn) return;
    var saved = null;
    try {{ saved = localStorage.getItem('ocw-theme') }} catch (e) {{}}
    var theme = saved || (window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
    document.documentElement.setAttribute('data-theme', theme);
    btn.textContent = theme === 'dark' ? '☀️' : '🌙';
    btn.onclick = function () {{
      var next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', next);
      btn.textContent = next === 'dark' ? '☀️' : '🌙';
      try {{ localStorage.setItem('ocw-theme', next) }} catch (e) {{}}
    }};
  }})();
  // Update check — progressive enhancement; the dashboard works without it.
  (function () {{
    var slot = document.getElementById('update-slot');
    if (!slot) return;
    fetch('/api/version').then(function (r) {{ return r.json() }}).then(function (v) {{
      if (!v.ok) {{ slot.textContent = ' · update check failed'; return }}
      if (!v.update_available) return;
      var btn = document.createElement('button');
      btn.textContent = '⬆ Update available (' + v.local + ' → ' + v.remote + ')';
      btn.className = 'update-btn';
      btn.onclick = function () {{
        btn.disabled = true;
        btn.textContent = 'Updating…';
        fetch('/api/update', {{ method: 'POST' }}).then(function (r) {{ return r.json() }}).then(function (res) {{
          if (res.ok && res.updated) {{
            btn.textContent = 'Updated — reloading…';
            setTimeout(function () {{ location.reload() }}, 1500);
          }} else {{
            btn.disabled = false;
            btn.textContent = 'Update failed: ' + (res.error || 'unknown error');
          }}
        }}).catch(function (e) {{
          btn.disabled = false;
          btn.textContent = 'Update failed: ' + e;
        }});
      }};
      slot.appendChild(btn);
    }}).catch(function () {{}});
  }})();
  </script>
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
        if method != "GET" and not (method == "POST" and path == "/api/update"):
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
        if path == "/api/version":
            return 200, "application/json", json.dumps(_check_for_update(), indent=2)
        if path == "/api/update":
            if method != "POST":
                return 405, "text/plain", "Method not allowed"
            return 200, "application/json", json.dumps(_apply_update(), indent=2)
        if path == "/health":
            return 200, "application/json", json.dumps({"ok": True})

        if self.mode == "serve":
            return 404, "text/plain", "Not found"
        return 404, "application/json", json.dumps({"error": "not found"})


def _run_server(cookie_path: Path, mode: str, port: int, host: str = "127.0.0.1") -> int:
    """Start a stdlib HTTP server (no dependencies)."""
    from http.server import HTTPServer, BaseHTTPRequestHandler
    import socket

    handler_ctx = _APIHandler(cookie_path, mode)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            status, ctype, body = handler_ctx.handle(self.command, self.path)
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))

        def do_POST(self):
            status, ctype, body = handler_ctx.handle(self.command, self.path)
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))

        def log_message(self, fmt, *args):
            pass  # quiet

    class LocalHTTPServer(HTTPServer):
        address_family = socket.AF_INET6 if ":" in host else socket.AF_INET

    label = "dashboard" if mode == "serve" else "API"
    display_host = "localhost" if host in {"127.0.0.1", "::1", "localhost"} else host
    print(f"🚀 Ollama Cloud {label} server on http://{display_host}:{port}")
    if mode == "serve":
        print(f"   Dashboard: http://localhost:{port}/")
    print(f"   Endpoints: /api/usage  /api/history  /api/sessions  /api/lifetime  /health")
    print(f"   Ctrl+C to stop")
    try:
        server = LocalHTTPServer((host, port), Handler)
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
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind host (default: 127.0.0.1)")
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
        return _run_server(args.cookie, "serve", port, args.host)

    # --api: JSON API server
    if args.api:
        port = args.port or 8643
        return _run_server(args.cookie, "api", port, args.host)

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