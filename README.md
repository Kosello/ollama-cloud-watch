# ollama-cloud-watch

> **⚠️ WORK IN PROGRESS — expect bugs.** This tool is under active development.
> The core scraper, watch mode, and reports work, but you may hit rough edges
> (parser breakage when Ollama changes their HTML, cookie expiry, edge cases in
> the dashboard). If something breaks, [open an issue](https://github.com/Kosello/ollama-cloud-watch/issues)
> or just re-paste a fresh cookie.

Standalone Ollama Cloud usage monitor — a single Python file, zero dependencies (stdlib only). Works on macOS, Linux, and Windows. No Hermes Agent needed.

![Ollama Cloud](https://img.shields.io/badge/ollama-cloud-000000?logo=ollama&logoColor=white)
![Python](https://img.shields.io/badge/python-3.9+-blue)
![License](https://img.shields.io/badge/license-MIT-green)

Ollama Cloud has **no usage API** — the only source is the web dashboard at [ollama.com/settings](https://ollama.com/settings). This script scrapes that page with your session cookie and gives you:

- **Current usage** — session & weekly %, reset times, per-model request counts, estimated cost per request
- **Watch mode** — continuous polling with history recording and threshold alerts
- **History** — weekly snapshots + 5h session snapshots saved locally (survives Ollama's resets)
- **Alerts** — silent watchdog that fires OS notifications when usage crosses 75% / 90%
- **Report** — full stats MD report with weekly overview + all 5h session detail

## Quick start

```bash
# Download
curl -O https://raw.githubusercontent.com/Kosello/ollama-cloud-watch/main/ollama-cloud-watch.py
chmod +x ollama-cloud-watch.py

# Set up cookie (get it from ollama.com/settings → DevTools → Cookies → __Secure-session)
echo '__Secure-session=<value>' > ~/.ollama-cloud-cookie.txt
chmod 600 ~/.ollama-cloud-cookie.txt

# Print current usage
python ollama-cloud-watch.py
```

Output:
```
📊 Ollama Cloud — Pro plan
   Session:  19.2% used · resets in 4h
   Weekly:   48.7% used · resets in 1 day
   Est. cost consumed: $2.25 / $4.62/wk

   Session per model:
     glm-5.2                      34 req · 79.6%
     deepseek-v4-flash:0731       81 req · 20.4%

   Weekly per model:
     glm-5.2                     668 req · 89.6% · $0.0030/req
     deepseek-v4-flash:0731      504 req · 7.5% · $0.0003/req
     deepseek-v4-pro               2 req · 2.3% · $0.0258/req
     minimax-m3                   12 req · 0.5% · $0.0009/req
     Avg: $0.0019/req across all models
```

## All modes

```bash
# Print current usage once and exit
python ollama-cloud-watch.py

# Poll every 30 minutes, record history, fire threshold alerts
python ollama-cloud-watch.py --watch

# Poll every 5 minutes
python ollama-cloud-watch.py --watch --interval 300

# Record one history snapshot and exit (good for cron)
python ollama-cloud-watch.py --history

# Silent alert if threshold crossed, else no output (cron watchdog)
python ollama-cloud-watch.py --alert

# Custom thresholds
python ollama-cloud-watch.py --alert --warn 80 --crit 95

# Generate full stats MD report from history
python ollama-cloud-watch.py --report

# Generate and open report in default app
python ollama-cloud-watch.py --report --open

# Raw JSON output (pipe into jq, other tools)
python ollama-cloud-watch.py --json
```

## Dashboard (3 ways)

### 1. Live web dashboard (`--serve`)

Starts a local HTTP server with a self-contained dark-themed dashboard — no dependencies, just Python stdlib.

```bash
python ollama-cloud-watch.py --serve
# → http://localhost:8642/

# Custom port
python ollama-cloud-watch.py --serve --port 8080
```

Shows: session/weekly usage bars with color-coded thresholds, savings headline, per-model breakdown (session + weekly), avg cost per request (this week + lifetime), cache hit % per model, token volume, API equivalent cost, cost efficiency, break-even, monthly projection, weekly history, and all 5h session snapshots — every section collapsible (native `<details>`, zero JS).

### 2. Static HTML file (`--html`)

Generates a standalone `.html` file from current data — open it in any browser, share it, no server needed.

```bash
python ollama-cloud-watch.py --html          # prints path
python ollama-cloud-watch.py --html --open   # opens in default app
```

### 3. JSON API server (`--api`)

Serves REST endpoints only — for Grafana, custom dashboards, curl/jq, or your own frontend.

```bash
python ollama-cloud-watch.py --api
# → http://localhost:8643/api/usage
```

Endpoints:

| Endpoint | Returns |
|---|---|
| `/api/usage` | Current usage (session/weekly %, resets, per-model, cost estimates) |
| `/api/history` | Weekly history snapshots (all recorded weeks) |
| `/api/sessions` | 5h session snapshots |
| `/api/lifetime` | Lifetime aggregated stats (all weeks, per-model avg cost) |
| `/health` | `{"ok": true}` |

```bash
curl http://localhost:8643/api/usage | jq '.weekly_used_pct'
curl http://localhost:8643/api/lifetime | jq '.est_avg_cost_per_req'
```

All endpoints return JSON with CORS headers (`Access-Control-Allow-Origin: *`), so you can fetch from any frontend.

## Cookie

The script reads your Ollama Cloud session cookie from either:

**macOS Keychain** (recommended on macOS):
```bash
security add-generic-password -s ollama-cloud-watch -a ollama -w '<cookie>' -U
```

**Or a plain file** (works on any OS):
```bash
echo '__Secure-session=<value>' > ~/.ollama-cloud-cookie.txt
chmod 600 ~/.ollama-cloud-cookie.txt
```

Get the cookie from your browser: open [ollama.com/settings](https://ollama.com/settings) (logged in) → DevTools → Application/Storage → Cookies → `https://ollama.com` → copy `__Secure-session`.

Set `OLLAMA_COOKIE_SOURCE=auto|keychain|file` (default: `auto` — Keychain if present, otherwise file).

Use `--cookie /custom/path.txt` for a custom cookie file location.

## Cron setup

```bash
# Watchdog every 30 min — silent unless threshold crossed
*/30 * * * * python /path/to/ollama-cloud-watch.py --alert --cookie ~/.ollama-cloud-cookie.txt

# Daily summary at 9am — records snapshot + prints usage
0 9 * * * python /path/to/ollama-cloud-watch.py --history --cookie ~/.ollama-cloud-cookie.txt

# Weekly report every Monday at 8am
0 8 * * 1 python /path/to/ollama-cloud-watch.py --report
```

## Storage

All files are kept in your home directory, independent of any other tool:

```
~/.ollama-cloud-history.jsonl     # weekly snapshots (one per ISO week, deduped)
~/.ollama-cloud-sessions.jsonl    # 5h session snapshots (one per session window)
~/.ollama-cloud-report.md         # generated report
~/.ollama-cloud-alert-state.json  # threshold state (prevents repeat alerts)
```

## Cost estimate

Ollama Cloud bills by GPU-time utilization, not tokens. The per-request cost is a rough proxy based on the dashboard's per-model usage share:

```
weekly_budget   = plan_price / 4.33          # Pro $20/mo ≈ $4.62/wk
cost_consumed   = weekly_budget × weekly_used%
model_cost      = cost_consumed × model_share%
cost_per_req    = model_cost / requests
```

The model share% comes from Ollama's own usage bar segments, which already reflect the GPU-time weighting — so heavier models show higher per-request cost.

### "What would this cost on the native APIs?" — fallback chain

The API-equivalent cost uses real token counts × official API list prices, resolved through a fallback chain — the first source that has data wins, manual is the last resort when everything else fails:

| Level | Prices (per 1M tokens) | Tokens per request |
|---|---|---|
| 1 | **Manual override file** `~/.ollama-cloud-prices.json` | **Manual override** (same file, `tokens_per_request` key) |
| 2 | **Live OpenRouter fetch** (vendor list prices, cached 24h) | **Hermes state.db** (real per-model averages, if Hermes is installed) |
| 3 | Builtin defaults (bundled table) | Cross-model mean of known models |
| 4 | — | 1000 in + 500 out assumption |

The output shows which source was used (`price_source` / `token_source` in `--json`). To pin prices yourself, create `~/.ollama-cloud-prices.json`:

```json
{
  "models": {
    "glm-5.2": { "input": 1.40, "output": 4.40, "cache_read": 0.26 }
  },
  "tokens_per_request": {
    "glm-5.2": [100000, 3000, 20000]
  }
}
```

Delete the file to revert to automatic. Cache-hit input tokens are billed at the discounted cache rate.

## Caveats

- **Cookie scraping is brittle** — if Ollama changes their settings page markup or the cookie expires, re-extract the cookie.
- The cookie is a login token — keep it private (`chmod 600` on the file).
- The script uses only Python stdlib — no pip install needed.

## Hermes Agent integration

If you use [Hermes Agent](https://hermes-agent.nousresearch.com), there's a full desktop plugin (statusbar chip + pane with collapsible sections, per-model breakdown, API price comparison, lifetime stats) at [Kosello/ollama-cloud-usage-stats](https://github.com/Kosello/ollama-cloud-usage-stats).

## License

MIT