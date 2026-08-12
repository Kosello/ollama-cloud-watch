# ollama-cloud-watch

> **⚠️ WORK IN PROGRESS — expect bugs.** This tool is under active development.
> The cookie scraper, API fallback, watch mode, and reports work, but you may hit
> rough edges. If something breaks, [open an issue](https://github.com/Kosello/ollama-cloud-watch/issues).

Standalone Ollama Cloud usage monitor — a single Python file, zero dependencies (stdlib only). Works on macOS, Linux, and Windows. No Hermes Agent needed.

![Dashboard screenshot](dashboard-screenshot.png)

![Ollama Cloud](https://img.shields.io/badge/ollama-cloud-000000?logo=ollama&logoColor=white)
![Python](https://img.shields.io/badge/python-3.9+-blue)
![License](https://img.shields.io/badge/license-MIT-green)

The script fetches the authenticated [settings page](https://ollama.com/settings) first because its per-model usage-bar shares are required for Ollama $/1M estimates. `GET /api/usage` is a fallback for aggregate percentages and request counts; it cannot provide per-model Ollama prices.

- **Current usage** — observed session/weekly quota %, per-model request counts and usage-bar shares, estimated Ollama $/1M, and API-equivalent $/request + $/1M
- **API-equivalent cost** — actual $ cost of tokens consumed on Ollama, priced as if they went through the API. Per-model breakdown with a **cache-aware second line** showing the cost at your real provider cache hit rate
- **Cache break-even** — per model, the cache hit rate at which the API becomes cheaper than the subscription, with your **real API cache rate** from other provider keys (DeepSeek, OpenRouter, etc.)
- **Lifetime break-even** — aggregated over all saved weeks: per-model break-even and API price comparison with real cache rates
- **Watch mode** — continuous polling with history recording and threshold alerts
- **History** — weekly snapshots + 5h session snapshots saved locally (survives Ollama's resets)
- **Alerts** — silent watchdog that fires OS notifications when usage crosses 75% / 90%
- **Report** — full stats MD report with weekly overview + all 5h session detail

## Quick start

```bash
# Download
curl -O https://raw.githubusercontent.com/Kosello/ollama-cloud-watch/main/ollama-cloud-watch.py
chmod +x ollama-cloud-watch.py

# Primary: save __Secure-session from ollama.com in a mode-600 cookie file
echo '__Secure-session=<value>' > ~/.ollama-cloud-cookie.txt
chmod 600 ~/.ollama-cloud-cookie.txt

# Optional fallback: export the API key or use ~/.ollama-cloud-api-key.txt
export OLLAMA_API_KEY='...'

# Print current usage
python ollama-cloud-watch.py
```

Output (abridged):
```text
📊 Ollama Cloud — Pro plan
   Session:  2.7% used · resets in 1h
   Weekly:   15.9% used · resets in 4 days

   💰 Session: $1.0921 API
      deepseek-v4-flash:0731  $1.0921  (with cache 98%: $0.2423)
   💰 Weekly:  $21.4867 API
      glm-5.2                 $0.4576  (with cache 62%: $0.2278)
      deepseek-v4-flash:0731  $20.3505  (with cache 98%: $4.5154)
      deepseek-v4-pro         $0.4774  (with cache 96%: $0.0966)
      minimax-m3              $0.2012  (with cache 94%: $0.0831)

   Cache break-even — when the API becomes cheaper than the subscription
      glm-5.2                   >100% (plan always cheaper) · real API: 62%
      deepseek-v4-flash:0731    >100% (plan always cheaper) · real API: 98%
      deepseek-v4-pro           API cheaper above 84% cache hit · real API: 95%

   Lifetime break-even & price comparison
      Aggregated over 2 saved week(s) · 2606 requests · $9.20 plan equivalent
      glm-5.2                    1429 req · API $0.7561/1M · >100% (plan always cheaper) · real API: 62%
      deepseek-v4-flash:0731     1024 req · API $0.0804/1M · >100% (plan always cheaper) · real API: 98%
      deepseek-v4-pro             113 req · API $0.2569/1M · API cheaper above 98% cache hit · real API: 95%
```

The Ollama estimate uses the real weekly usage-bar share plus historical tokens/request. API input/cache/output prices are resolved per model. `Ollama/API` is the Ollama estimate as a percentage of the real API estimate; lower means better subscription value.

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

# If Hermes API already owns IPv4 127.0.0.1:8642, bind the dashboard to
# IPv6 loopback. http://localhost:8642/ will use it while the Hermes API
# remains available at http://127.0.0.1:8642/v1.
python ollama-cloud-watch.py --serve --host ::1 --port 8642

# Custom port
python ollama-cloud-watch.py --serve --port 8080
```

Shows: session/weekly usage bars with color-coded thresholds, per-model request
mix, effective subscription cost, cache/token estimates, API-equivalent cost,
break-even comparison, weekly history, and 5h session snapshots.

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
| `/api/usage` | Current usage (session/weekly %, resets, per-model, cost estimates, break-even) |
| `/api/history` | Weekly history snapshots (all recorded weeks) |
| `/api/sessions` | 5h session snapshots |
| `/api/lifetime` | Lifetime aggregated stats (all weeks, per-model avg cost) |
| `/api/lifetime-break-even` | Lifetime cache break-even + plan/API comparison across all saved weeks |
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

## API fallback

If the cookie is missing, expired, or the settings markup changes, the script tries `GET https://ollama.com/api/usage` using `OLLAMA_API_KEY`, `~/.ollama-cloud-api-key.txt`, or the Hermes API-key file. The fallback provides aggregate session/weekly percentages and per-model request counts. It does **not** expose per-model usage-bar shares, current-window token/cache counts, exact reset timestamps, or plan tier, so per-model Ollama $/1M and Ollama/API percentages are reported as unavailable.

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

## Calculation model

The cookie-backed settings page exposes each model's share of Ollama's weekly usage bar. The tool combines that share with the fixed plan price, request count, and estimated tokens/request:

```text
allocated plan value = 7-day plan equivalent × observed weekly quota fraction × normalized model usage-bar share
Ollama $/1M          = allocated plan value / estimated model tokens × 1,000,000
```

This is an effective plan-price estimate, not an Ollama token tariff. The fixed 7-day plan equivalent is scaled by the observed weekly quota fraction, then allocated across models by Ollama's observed usage-bar shares, normalized so rounded bars reconcile exactly. Token volume is estimated because Ollama exposes no current-window token/cache counts. If a bar is rounded to `0.0%`, or only `/api/usage` is available, the model's Ollama $/1M and Ollama/API percentage are shown as unavailable rather than guessed.

### API-equivalent cost + cache-aware line

```text
API_$/req = uncached_input × input_$/token
          + cached_input   × cache_$/token
          + output         × output_$/token
```

Per model, a **cache-aware second line** shows the cost when prompt tokens are split by your **real provider cache hit rate** (from other API keys in Hermes `state.db` — DeepSeek native, OpenRouter, etc.):

```text
API_$/req (cached) = prompt × (1 − cache_rate) × input_$/token
                   + prompt × cache_rate       × cache_$/token
                   + output                   × output_$/token
```

When the same model was used via several providers, the rate from the provider with the most recorded calls wins (largest sample). OpenRouter is the fallback — native provider keys take priority.

### Cache break-even

```text
break-even cache % = cache hit rate at which API cost = subscription allocation per request
```

`>100%` means even perfect caching can't make the API cheaper. The section shows your **real API cache rate** from other providers so you can see whether you're above or below the threshold.

### Lifetime break-even

Aggregates all saved weekly history: per-model request counts, usage-bar shares, and plan allocations across all recorded weeks. Same break-even math with today's resolved prices and token profiles. Shows which models were consistently plan-wins versus borderline over the full recorded period.

Prices and token averages use automatic sources with per-model manual overrides:

| Level | Prices (per 1M tokens) | Tokens per request |
|---|---|---|
| Automatic base | Live OpenRouter data (cached 24h), then builtin gaps | Hermes `state.db` request-weighted model/global averages |
| Manual layer | `~/.ollama-cloud-prices.json` per-model fields | Same file, `tokens_per_request` per model |
| Final fallback | Builtin table | 1000 input + 500 output |

The output shows which source was used (`price_source` / `token_source` in `--json`). To pin prices yourself, create `~/.ollama-cloud-prices.json`:

```json
{
  "models": {
    "glm-5.2": { "input": 0.07, "output": 0.22, "cache_read": 0.013 }
  },
  "tokens_per_request": {
    "glm-5.2": [100000, 3000, 20000]
  }
}
```

Delete the file to revert to automatic. If no cache-read price is published,
cached input uses the regular input price; it is never treated as free.

## Caveats

- The official API does not expose plan tier, reset timestamps, current token
  counts, cache hits, elapsed weekly-period time, or per-model quota weights.
  API reset times are shown as unavailable; token costs are labeled estimates.
- Cookie scraping is fallback-only and remains brittle.
- The cookie is a login token — keep it private (`chmod 600` on the file).
- The script uses only Python stdlib — no pip install needed.

## Hermes Agent integration

If you use [Hermes Agent](https://hermes-agent.nousresearch.com), the integrated
desktop/backend plugin lives at [Kosello/ollama-usage-monitor](https://github.com/Kosello/ollama-usage-monitor).

## License

MIT