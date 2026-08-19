# Changelog

All notable changes to **Ollama Cloud Watch** (standalone dashboard + CLI).

The format follows [Keep a Changelog](https://keepachangelog.com/), and this project adheres to [Semantic Versioning](https://semver.org/) loosely — the dashboard is a single-file tool, so releases are tracked by date and commit.

## [Unreleased]

### Added
- **Dark / light theme** — theme toggle (🌓) in the dashboard header, persisted in `localStorage`, defaults to the system color scheme. All colors are CSS variables, so both themes cover every section.
- **Update button moved to the header** — the self-update button now sits top-right next to the theme toggle (was in the footer), where update indicators belong.

## [2026-08-19] — Self-update + official DeepSeek pricing

### Added
- **Dashboard self-update button** — `/api/version` compares the local HEAD with `origin/main` (1h cache); when a newer commit exists, an **⬆ Update available** button appears. Clicking it POSTs to `/api/update`, which runs `git pull --ff-only` and restarts the server (launchd `KeepAlive` brings it back). Safety guards: dirty checkouts are refused, `--ff-only` never rewrites history, only POST is accepted.
- **Official DeepSeek-V4 API pricing** — the new rate table (effective 2026-08-16 16:00 UTC) with **peak/off-peak windows** (peak: 01:00–04:00 and 06:00–10:00 UTC). The tool picks the right rate for the current UTC hour automatically. Official rates override stale OpenRouter snapshots; manual overrides still win.

### Fixed
- **Cache-rate resolution for model variants** — `deepseek-v4-flash:0731` was matching a 1-call/0-cache row (0.0% cache) instead of the base model's 361-call/97.8% sample. Variant suffixes are now merged into the base name during aggregation, so the largest sample wins.

## [2026-08-12] — Cache economics

### Added
- **Cache break-even section** — per model, the cache hit rate at which the pay-per-token API becomes cheaper than the subscription. Values >100% mean the plan always wins; 0% means the API always wins.
- **Lifetime break-even & price comparison** — the same analysis aggregated over all saved weekly history.
- **Real provider cache rates** — cache hit rates pulled from the user's actual API usage (state.db, non-Ollama providers). Largest sample wins per model; OpenRouter is the fallback when no native key recorded the model.
- **Cache-aware API cost** — the API equivalent cost section now shows a second line per model: what the used tokens would cost at the real provider cache rate (e.g. Flash: $20.35 → $4.52 at 98% cache).
- **Plan vs API with cache** — a second comparison table under Plan vs API, with the API side priced at the real cache rate.

## [2026-08-10] — Corrected economics

### Fixed
- **Reliable per-model Ollama $/1M** — the plan allocation is now scaled by the observed weekly quota fraction (`weekly_used_fraction`). Previously a freshly-reset week (0.5% used) allocated the full $4.60 weekly fee across a handful of requests, producing absurd rates like "GLM costs 2076% of the API price". The formula is now:

  ```
  allocated plan value = 7-day plan equivalent × observed weekly quota fraction × normalized model usage-bar share
  Ollama $/1M          = allocated plan value / estimated model tokens × 1,000,000
  ```

## [2026-08-09] — Economics corrections + fallback chain

### Fixed
- **Correct usage economics and API cost estimates** — the weekly fee is no longer scaled by `weekly_used_fraction` in the wrong direction; API-only mode no longer fabricates per-model Ollama prices or reset timestamps; cookie fallback validates the parsed page (a login/expired-session page is not accepted as valid data).
- **Price matching for model variants** (`:0731`) and naming conventions.
- **Crash when an OpenRouter price has no `cache_read`** — missing cache pricing falls back to the input rate, labeled `n/a`.

### Added
- **Price fallback chain** — manual override → live OpenRouter (24h cache) → builtin offline defaults; the active source is shown in the UI.
- **Token fallback chain** — manual override → Hermes `session_model_usage` → legacy `sessions` → request-weighted global history → fixed fallback.
- **Full dashboard mirroring the Hermes pane** — all sections collapsible.

## [2026-08-08] — Initial release

### Added
- **Standalone CLI** — `ollama-cloud-watch.py`, zero dependencies, stdlib only.
- **Three output modes** — `--serve` (persistent web dashboard on `localhost:8642`), `--html` (static report), plain CLI output.
- **Usage tracking** — session & weekly quota percentages, reset times, per-model request counts and usage-bar shares, weekly history snapshots, 5h session snapshots.
- **Plan vs API comparison** — per-model effective Ollama $/1M (subscription allocated by usage-bar share) vs real API $/1M, with the Ollama/API ratio.
- **API equivalent cost** — what the consumed tokens would cost on the API, per model, with cache-aware pricing.
- **Savings estimate** — subscription vs API-equivalent spend.
- **Threshold alerts** — warnings when session/weekly usage crosses configurable thresholds.
- **LaunchAgent support** — `com.kosello.ollama-cloud-watch-dashboard` keeps the dashboard alive across reboots.
