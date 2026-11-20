[中文](../../README.md) · [Русский](../ru/README.md) · [日本語](../ja/README.md) · English

# Generic Fallback Proxy

[![CI](https://github.com/sch-chun/astrbot_plugin_generic_fallback/actions/workflows/ci.yml/badge.svg)](https://github.com/sch-chun/astrbot_plugin_generic_fallback/actions/workflows/ci.yml)

An AstrBot plugin providing a generic OpenAI-compatible proxy service built on provider fallback chains.

## Features

- **Virtual models**: combine multiple providers into one virtual model exposing a unified OpenAI-compatible endpoint
- **Fallback chain**: try providers in priority order, automatically switching to the next one on failure
- **Cooldown mechanism**: a failed provider enters cooldown, with duration increasing by a configurable "consecutive failures" ladder
- **Manual disable**: disable/re-enable any provider at any time from the monitoring panel; disabled nodes are skipped in the fallback
- **Call logs**: view recent call records per provider (time, success/failure, failure reason) in the monitoring panel
- **Custom rules**: configure optional per-provider rules that trigger cooldown or disable when an error message consecutively matches a given fragment N times
- **Streaming support**: full SSE streaming response proxying
- **API Key verification**: optional authentication for the proxy layer itself
- **Model tags**: optionally prefix response text with `[Provider name]` to trace which model was actually used
- **Monitoring panel**: built-in web status page showing cooldown state and virtual model configuration
- **Multilingual**: configuration items and the monitoring panel support 简体中文 / English / 日本語 / Русский, following the AstrBot dashboard locale; this README is available in all four languages too

## Installation

Install via the link in the AstrBot management panel (https://github.com/sch-chun/astrbot_plugin_generic_fallback), or clone manually into the plugins directory:

```bash
cd AstrBot/data/plugins
git clone https://github.com/sch-chun/astrbot_plugin_generic_fallback.git
```

## Configuration

After installation, set the following options in the plugin configuration in the management panel:

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `proxy_host` | string | `127.0.0.1` | Proxy listen address; keep local by default |
| `proxy_port` | int | `3474` | Proxy listen port |
| `proxy_api_key` | string | (empty) | Proxy-layer API Key; no verification when empty |
| `empty_response_max_attempts` | int | `3` | Empty-response retries; beyond this, fall back to the next provider without counting as a consecutive failure (in streaming, retries and fallback complete before the first content byte is sent; returns 503 when all providers are unavailable, same as non-streaming) |
| `cooldown_policy` | list | `[[1, 1], [2, 5], [5, 30]]` | Consecutive-failure cooldown ladder, see below |
| `call_log_max_per_provider` | int | `200` | Log entries kept per provider; each provider keeps its own latest N entries. Set to 0 to disable logs |
| `virtual_models` | list | (see below) | Virtual model configuration list |

### virtual_models

Each virtual model contains:

- **display_name**: the virtual model name used by clients (e.g. `my-fallback`); the legacy `name` field is still supported
- **provider_ids**: providers forming the fallback chain, ordered from highest to lowest priority
- **timeout**: request timeout in seconds, default 120

> ⚠️ Note: do not select the virtual model itself in provider_ids (the plugin filters circular references automatically and warns).

### cooldown_policy

`cooldown_policy` is a nested list where each item is `[consecutive failures, cooldown minutes]`, sorted ascending by failure count:

```json
[[1, 1], [2, 5], [5, 30]]
```

This means: **1 failure** → cooldown **1 minute**, **2-4 failures** → **5 minutes**, **5 or more failures** → **30 minutes**.
A tier's cooldown stays in effect until the next tier's threshold; failure counts below the first threshold use the first tier (e.g. `[[3, 10]]` means a 10-minute cooldown regardless of failure count).

- A successful request **resets** the provider's consecutive failure count.
- Cooldown minutes may be fractional (e.g. `0.5` = 30 seconds); `0` means no cooldown.
- An equivalent JSON string is also accepted (e.g. `"[[1, 1], [2, 5]]"`).
- Empty-response retry exhaustion, `completion has no choices`, content-safety filtering, and 415 `Unsupported Media Type` are **neither counted nor cooled down** — they only fall back to the next provider (the latter two are explicit upstream rejections, so retrying is pointless and should not penalize the provider). **Streaming and non-streaming behave identically**: `completion has no choices` is retried in place under streaming too, and only falls back (no cooldown) once retries are exhausted.
- Invalid configuration (not a list, item not of length two, non-positive-integer or duplicate thresholds, negative or non-numeric minutes) is logged as an error and **falls back to the default** `[[1, 1], [2, 5], [5, 30]]`.

### Custom rules

In addition to the built-in cooldown ladder, you can configure **optional per-provider rules** in the monitoring panel: if a failure reason contains a given match fragment (case-insensitive), it counts as one hit; reaching the threshold on consecutive hits triggers one of three actions — cooldown for a specified number of minutes, cooldown until 00:00 the next day, or disable.

Rules are managed via the "Rules" button on each provider card in the monitoring panel. Each rule contains:

- **Match text**: a fragment of the error code or message (e.g. `429`, `quota`), up to 100 characters;
- **Count**: how many consecutive hits trigger the action (positive integer; a success resets the counter);
- **Action**: `Cooldown for minutes` (fractional minutes allowed) / `Cooldown until 00:00 next day` / `Disable`;
- **Timezone** (only for "Cooldown until 00:00"): leave empty or `local` for local time; `UTC` / `GMT` for universal time; prefixed offsets are also supported, e.g. `UTC+8`, `UTC +8`, `GMT-5.5`, `UTC+05:30` (colon = minutes, `.5` = half an hour); plain UTC offset hours work too (e.g. `-8`, `+9`, `+5.5`). Quotas reset at 00:00 in the given timezone; different providers in a fallback chain often reset in different timezones, so this can be set per rule.

Behavior notes:

- When a rule hits, the **built-in cooldown ladder is skipped** (no cooldown accumulation via cooldown_policy); the rule action takes over;
- Rules and live hit counts (`hits/threshold`) are shown in the rule modal; rules at the threshold are highlighted;
- Rules are stored in plugin KV and restored automatically after an AstrBot restart; a single invalid rule is discarded without affecting the others;
- Add/edit/delete is submitted via `POST /generic_fallback/rules/set` (replace semantics). This endpoint uses AstrBot panel authentication and requires login; the `custom_rules` field of the `status` endpoint exposes each provider's rules and live hit counts.

## Usage

### Configure a provider in AstrBot

Once the plugin is running, fill in AstrBot's OpenAI provider settings:

- **API Base URL**: `http://127.0.0.1:3474/v1` (adjust for proxy_host/proxy_port)
- **API Key**: the value set in `proxy_api_key` (if unset, leave blank — but the OpenAI provider requires a non-empty value, so use any placeholder such as `no-key`)
- **Model name**: the name of a virtual model configured in virtual_models (e.g. `my-fallback`)

### External calls

The proxy exposes standard OpenAI-compatible endpoints:

```bash
# List virtual models
curl http://127.0.0.1:3474/v1/models

# Chat completion
curl http://127.0.0.1:3474/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-proxy-api-key" \
  -d '{"model": "my-fallback", "messages": [{"role": "user", "content": "Hello"}]}'

# Check status
curl http://127.0.0.1:3474/v1/status
```

## Monitoring

The plugin ships with a built-in monitoring panel, available on the plugin page in the AstrBot management panel. It shows each provider's cooldown state, consecutive failure count, and virtual model configuration.

Each provider card has a **Disable / Enable** button:

- Once disabled, the provider immediately stops participating in fallback (shown as "Disabled") until manually re-enabled;
- Re-enabling also clears its cooldown and consecutive failure count;
- This state is kept in memory only — it resets to all-available after a plugin reload or AstrBot restart.

The top of the panel shows the currently active cooldown ladder (e.g. `1 failure→1 min, 2 failures→5 min, 5 failures→30 min`).

Each provider card also has a **Logs** button to view that provider's recent call records:

- Each record includes call time, success/failure, and failure reason (e.g. explicit upstream rejection, repeated empty responses);
- Data is kept in memory only — the latest N entries per provider (default 200, adjustable via `call_log_max_per_provider`, without evicting each other); cleared on plugin reload or AstrBot restart;
- Records are read via `GET /generic_fallback/logs?provider_id=...`, which uses AstrBot panel authentication and requires login.

Each provider card also has a **Rules** button to manage that provider's custom rules:

- The rule list shows each rule's match text, trigger condition, and live hit count (`hits/threshold`); rules at the threshold are highlighted;
- You can add, edit, and delete rules in the modal; the "Rules ×N" badge on the card shows the current rule count;
- Click "Save" to apply changes; closing without saving warns that changes were discarded.

## Testing

Run in the plugin directory (requires `pytest` and `pytest-asyncio`, with the AstrBot directory on `PYTHONPATH`):

```bash
pytest -q
```

## License

GNU Affero General Public License v3.0
