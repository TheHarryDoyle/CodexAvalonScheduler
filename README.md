# Avalon Q Scheduler

> **Public review snapshot.** Avalon Q Scheduler is a personal home-automation project I direct and operate with AI-assisted coding tools. AI assistance was also used to audit and sanitize this copy. The source, tests, and functional intent are retained; credentials, runtime state, and deployment-specific material remain private. See the [sanitized development history](DEVELOPMENT_HISTORY.md) and [publication boundary](PUBLIC_REVIEW_SNAPSHOT.md).

Local Python automation for a Canaan Avalon Q miner. It decides between `Super`, `Eco`, and optional `Off/Standby` using weather, a limited peak-solar schedule, and an optional overnight cheap-power window.

The Avalon miner API used here is raw TCP text on port `4028`. It is not HTTP, HTTPS, SSH, or browser automation. This project never uses `requests`, Selenium, or SSH for miner control.

## Understand This Project

| Start here | What it provides evidence for |
|---|---|
| [`avalon_q_scheduler/decision_engine.py`](avalon_q_scheduler/decision_engine.py) | Pure weather and solar policy decisions, kept separate from device and provider I/O. |
| [`avalon_q_scheduler/scheduler.py`](avalon_q_scheduler/scheduler.py) and [`state_store.py`](avalon_q_scheduler/state_store.py) | Job orchestration, cooldowns, startup catch-up, persisted mode state, API-request budgeting, and failure recovery. |
| [`avalon_client.py`](avalon_q_scheduler/avalon_client.py), [`tesla_client.py`](avalon_q_scheduler/tesla_client.py), and [`nest_client.py`](avalon_q_scheduler/nest_client.py) | The raw TCP device boundary and the optional external-service adapters, including token persistence and response validation. |
| [`f2pool_client.py`](avalon_q_scheduler/f2pool_client.py), [`f2pool_profitability.py`](avalon_q_scheduler/f2pool_profitability.py), and [`PROJECT_PLAN.md`](PROJECT_PLAN.md) | Source-labeled network data, settlement parsing, per-mode baselines, and the shadow-first rollout boundary. |
| [`tests/`](tests/) | The 112-test offline suite covering policy rules, state transitions, provider payloads, token rotation, retries, and failure cases. |
| [`config.example.yaml`](config.example.yaml) | The public configuration surface, dry-run default, optional integrations, schedules, timeouts, and safety gates. |
| [`DEVELOPMENT_HISTORY.md`](DEVELOPMENT_HISTORY.md) | All 49 preserved commits, their real linear topology, and the retained branch/ref labels without hashes or author metadata. |
| [`PUBLIC_REVIEW_SNAPSHOT.md`](PUBLIC_REVIEW_SNAPSHOT.md) | AI-assistance disclosure, publication boundary, and the material intentionally kept private. |

## Install

```powershell
cd "C:\path\to\CodexAvalonScheduler"
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item config.example.yaml config.yaml
```

Edit `config.yaml` if needed. It defaults to:

- Miner host: set this to your Avalon Q's LAN IP address
- Miner API port: `4028`
- `dry_run: true`
- Weather provider: Open-Meteo, no API key required

## Test Read-Only API First

These commands only send read-only miner API commands: `version`, `summary`, and `stats`.

```powershell
python -m avalon_q_scheduler.main test-api
python -m avalon_q_scheduler.main version
python -m avalon_q_scheduler.main summary
python -m avalon_q_scheduler.main stats
```

## Dry Run

Dry-run is enabled by default. In dry-run mode, the app logs and prints the raw TCP command it would send but does not contact the miner for mode changes.

```powershell
python -m avalon_q_scheduler.main decide-now --dry-run
python -m avalon_q_scheduler.main set-eco
python -m avalon_q_scheduler.main set-super
python -m avalon_q_scheduler.main softoff
python -m avalon_q_scheduler.main softon
python -m avalon_q_scheduler.main tesla-products
python -m avalon_q_scheduler.main tesla-live-status
python -m avalon_q_scheduler.main tesla-monthly-energy
python -m avalon_q_scheduler.main tesla-monthly-charging --start-date 2026-07-12
python -m avalon_q_scheduler.main nest-status
python -m avalon_q_scheduler.main nest-baseline
```

The Tesla and Nest commands above are read-only external API checks. They do not change the miner mode.

To enable real mode changes, edit `config.yaml`:

```yaml
automation:
  dry_run: false
```

Then run the same command again.

## Modes

The configured Avalon Q work mode commands are:

- Eco: `ascset|0,workmode,set,0`
- Standard: `ascset|0,workmode,set,1`
- Super: `ascset|0,workmode,set,2`

The scheduler never chooses Standard. `set-standard` exists only as a manual CLI command.

Optional standby commands are configurable because Avalon Q firmware behavior should be verified:

- Soft off, default timestamped form: `ascset|0,softoff,1:<unix_timestamp>`
- Soft off, legacy immediate form: `ascset|0,softoff,1:0`
- Soft on: `ascset|0,softon,1:<unix_timestamp>`

If the miner returns `STATUS=E`, the scheduler normally treats the command as failed and does not record the requested mode as successful. After a successful `softon`, the Avalon Q can temporarily reject a work-mode command while it wakes and calibrates. The scheduler recognizes that firmware response and retries every 15 seconds for up to 5 minutes. Both values are configurable with `softon_mode_retry_interval_seconds` and `softon_mode_retry_timeout_seconds`.

The scheduler persists a successful `softon` immediately. If the later Eco or Super command still fails, the next Off decision will send `softoff` instead of incorrectly assuming the miner remained off.

## Run Scheduler

```powershell
python -m avalon_q_scheduler.main run
```

Stop the foreground scheduler with `Ctrl+C` (or `Ctrl+Break` on Windows).

Daily schedules use `America/Chicago` by default:

- `08:00`: safety/weather decision: rainy = Off, otherwise Eco
- `11:30`: enter Super only if the `11:00`-`15:00` peak-solar forecast is strong enough
- `14:30`: return to Eco unless the same-day weather decision was rainy/Off, then remain Off
- `16:00`: late-afternoon bridge; if `16:00`-`18:00` solar is weak, start the evening behavior early
- `18:00`: Eco unless the most recent same-day weather decision was rainy/Off, then remain Off

The peak-solar Super window is enabled by default:

```yaml
automation:
  enable_peak_solar_super_window: true
  enable_late_afternoon_solar_bridge: true

schedule:
  peak_solar_high: "11:30"
  peak_solar_low: "14:30"
  late_afternoon_bridge: "16:00"

weather:
  peak_solar_start: "11:00"
  peak_solar_end: "15:00"
  peak_solar_min_avg_shortwave_radiation_w_m2: 550
  late_afternoon_bridge_start: "16:00"
  late_afternoon_bridge_end: "18:00"
  late_afternoon_bridge_min_avg_shortwave_radiation_w_m2: 250
```

The peak decision uses Open-Meteo `shortwave_radiation`, which is a closer proxy for solar production than cloud-cover percentage. If that field is unavailable, the scheduler falls back to the older cloud-cover sunny check.

The late-afternoon bridge exists for winter and cloudy afternoons. If the `16:00`-`18:00` forecast is below `late_afternoon_bridge_min_avg_shortwave_radiation_w_m2`, it applies `evening_behavior` at `16:00` instead of waiting until `18:00`. In summer, if late-afternoon solar is still usable, it leaves the miner in Eco until the normal `18:00` job.

To run an overnight-off experiment, set:

```yaml
automation:
  evening_behavior: "off"
```

That makes the `18:00` job send softoff on non-rainy days too. If the late-afternoon bridge is enabled and solar is weak from `16:00` to `18:00`, it can send softoff at `16:00`. Set it back to `"eco"` to leave the miner low overnight.

To restore the older behavior where the `09:00` weather decision can choose Super for the daytime window, set:

```yaml
automation:
  enable_peak_solar_super_window: false
```

The old overnight cheap-power window is still available, but it is disabled by default. With the default `enable_cheap_power_window: false`, the scheduler leaves the miner low overnight after the `18:00` Eco change. To turn the old overnight behavior back on later, set:

```yaml
automation:
  enable_cheap_power_window: true

schedule:
  cheap_power_high: "00:00"
  cheap_power_low: "04:00"
```

## Powerwall Policy

Tesla Fleet API integration is optional. It uses your Tesla developer app client ID/secret from environment variables and stores OAuth tokens only in a local token cache file.

One-time auth on the computer that runs the scheduler:

```powershell
$env:TESLA_CLIENT_ID = "<client id>"
$env:TESLA_CLIENT_SECRET = "<client secret>"
python -m avalon_q_scheduler.main tesla-auth
python -m avalon_q_scheduler.main tesla-products
python -m avalon_q_scheduler.main tesla-live-status
python -m avalon_q_scheduler.main tesla-monthly-energy
```

Recommended starting config:

```yaml
automation:
  enable_powerwall_policy: true
  startup_catchup_enabled: true
  morning_catchup_interval_seconds: 900
  daily_job_misfire_grace_seconds: 21600
  enable_cheap_power_window: false

schedule:
  morning_weather_decision: "08:00"
  evening_low: "18:00"
  cheap_power_high: "00:00"
  cheap_power_low: "04:00"
  powerwall_guard_times:
    - "21:00"
    - "22:00"
    - "23:00"
    - "23:45"
    - "01:00"
    - "02:00"
    - "03:00"
    - "03:30"

tesla:
  enabled: true
  energy_site_id: null  # replace with your numeric Tesla energy-site ID
  token_cache_path: "tesla_token_cache.json"

energy_policy:
  battery_reserve_percent: 25
  monthly_net_export_min_kwh: 0
  max_tesla_requests_per_day: 12
  enable_evening_predrain_super: true
  cheap_power_requires_monthly_net_export: true
  off_at_cheap_power_end: true
  enable_unlimited_charging_adjustment: true
  unlimited_charging_start_date: "2026-07-12"
  unlimited_charging_start: "00:00"
  unlimited_charging_end: "06:00"
```

Policy behavior:

- If the month is net exported (`exported_kWh - imported_kWh >= monthly_net_export_min_kwh`), the miner can use Super in the evening predrain and during Tesla Wind Hours (`00:00`-`04:00`).
- If the month is net imported, the miner stays Eco overnight and turns Off once Powerwall battery percentage reaches the configured reserve.
- If battery reaches reserve before midnight but the month is net exported, the miner drops to Eco until Wind Hours begin.
- At `04:00`, the miner softoffs until the `08:00` morning Eco/weather decision.
- If the scheduler host sleeps, restarts, or the weather provider has a transient error at `08:00`, catch-up reruns the missing same-day morning decision every 15 minutes before evening so a softoff latch does not wait until the next day.
- Daily cron jobs allow a six-hour misfire grace by default, so short host sleep/wake gaps can still execute the missed daily transition.
- Tesla data calls are capped by `max_tesla_requests_per_day`. With the default guard schedule, normal use is about 9 requests with the car adjustment disabled, about 10 with it enabled, and up to 12 on the first day of a month when both midnight and evening monthly snapshots are refreshed. With an active valid charging-adjustment plan and every relevant scheduled policy run reached, the adjustment adds one `telemetry_history?kind=charge` request per monthly snapshot refresh: `+1` on days 2 through month-end and `+2` on day 1, or `29/30/31/32` additional requests in a `28/29/30/31`-day month. Conditional policy bypasses, downtime, an inactive plan, or an exhausted request budget can reduce that increment, potentially to `0`. When the cap is exhausted, the scheduler avoids cheap Super rather than using an incompatible cached policy.

The optional unlimited-charging adjustment is disabled in the example config so a new installation cannot change its monthly gate without an explicit plan start date. The read-only `tesla-monthly-charging` command combines Tesla Fleet API Wall Connector charge history with the site's energy-flow rows and reports total vehicle charging, grid-supplied vehicle charging, and the Powerwall/other remainder without printing raw provider data.

When explicitly enabled, the scheduler keeps Tesla's original import/export totals and calculates a separate gate:

```text
car_grid_import_kwh = grid_imported_kwh - home_from_grid_kwh - powerwall_from_grid_kwh
adjusted_imported_kwh = imported_kwh - car_grid_import_kwh
adjusted_net_exported_kwh = exported_kwh - adjusted_imported_kwh
```

Only the car's grid-supplied portion is subtracted. Powerwall- or solar-supplied vehicle energy remains counted normally, so a session with 14.7 kWh total and 12.3 kWh from the grid adjusts imports by 12.3 kWh, not 14.7 kWh. The grid adjustment is capped at both Wall Connector energy and site imports. If some charging falls outside the configured unlimited window, the calculation conservatively assigns that outside-window charging to the grid first; only grid energy left after that can adjust the eligible window. This can under-credit the plan slightly, but it cannot turn an unknown outside-window source mix into free eligible energy.

If the source breakdown, charge-history API, start date, or response is unavailable, the scheduler uses the original unadjusted net kWh rather than falling back to total car charging. The cached monthly snapshot includes the adjustment settings and whether the plan has started, so the next Powerwall monthly-policy check after a restart refreshes changed settings instead of reusing a disabled or not-yet-started snapshot. Enabling or disabling this setting requires restarting the scheduler after changing `config.yaml`.

## Nest AC Monitoring

Nest Device Access integration is optional and starts read-only. The scheduler can poll the Hallway Thermostat for current HVAC status, indoor temperature, humidity, thermostat mode, setpoints, fan timer, connectivity, and room name. When `nest.enabled: true`, `python -m avalon_q_scheduler.main run` also starts a read-only interval monitor and writes snapshots to `outputs/nest_readings.jsonl`.

One-time authorization must happen on the 24/7 computer under the same Windows account that runs the scheduler. Before issuing the refresh token, verify the configured Avalon Google Cloud project, verify that the exact configured OAuth client ID belongs to it, and verify that project's Google Auth Platform publishing status is `In production`; publishing later does not revive a dead Testing-issued grant. The agent operating that computer should handle the terminal, redirect code, DPAPI storage, checks, and restart. The user's only unavoidable step is Google sign-in/consent (and, only if Google blocks the grant, the explicitly explained account-security approval). A DPAPI token file is tied to its Windows user and computer and must not be copied from another PC. Keep the client secret and refresh token out of git. The commands below are operator/agent reference, not manual user steps:

```powershell
$env:NEST_CLIENT_ID = "<client id>"
$env:NEST_CLIENT_SECRET = "<client secret>"
python -m avalon_q_scheduler.main nest-auth
python -m avalon_q_scheduler.main nest-refresh-check
python -m avalon_q_scheduler.main nest-status
python -m avalon_q_scheduler.main nest-log-once
python -m avalon_q_scheduler.main nest-baseline
python -m avalon_q_scheduler.main f2pool-account
python -m avalon_q_scheduler.main f2pool-log-once
python -m avalon_q_scheduler.main f2pool-import-settlements --dry-run
```

Recommended starting config:

```yaml
nest:
  enabled: true
  project_id: "<GOOGLE_DEVICE_ACCESS_PROJECT_ID>"
  client_id_env: "NEST_CLIENT_ID"
  client_secret_env: "NEST_CLIENT_SECRET"
  refresh_token_env: "NEST_REFRESH_TOKEN"
  refresh_token_dpapi_path: "outputs/nest_sdm_refresh_token.dpapi.txt"
  readings_log_path: "outputs/nest_readings.jsonl"
  monthly_backfill_path: "outputs/nest_energy_dashboard_monthly_backfill.csv"
  selected_thermostat_name: "Hallway Thermostat"
  redirect_uri: "https://www.google.com"
  auth_base_url: "https://nestservices.google.com/partnerconnections"
  poll_interval_seconds: 60
  failure_backoff_max_seconds: 3600
```

The official SDM API does not provide the Google Home Energy Dashboard's historical monthly/day-bar runtime export. The screenshot-backed monthly totals are seeded in `outputs/nest_energy_dashboard_monthly_backfill.csv`, and future history comes from the scheduler's own minute-by-minute readings. Room sensors visible in Google Home may not appear as separate SDM devices, so this first phase trusts the thermostat's official traits and uses room-sensor screenshots only as manual context.

Nest access-token renewal is automatic. An unexpected SDM `401` invalidates the cached access token and gets one automatic refresh-and-retry, without an unbounded loop. If Google rotates the refresh token during a successful renewal, the scheduler verifies and atomically replaces the DPAPI-encrypted token too. The encrypted token file takes precedence over a legacy `NEST_REFRESH_TOKEN` environment value, and the running scheduler reloads it automatically when it changes.

An OAuth `invalid_grant` response means Google has already expired or revoked the underlying refresh grant; Google will not exchange that dead grant for another one. The scheduler now treats this as terminal for that token and pauses further Google requests instead of retrying forever. It resumes automatically as soon as the encrypted token changes. Keep the Google OAuth consent app in Production: an External app left in Testing receives refresh tokens that expire after seven days.

The first baseline is deliberately shadow-only:

- Compare live cooling runtime against the screenshot-backed monthly history.
- Track when the AC is currently cooling before deciding that Super is cheap.
- Build rolling seasonal bins by outdoor weather, time of day, miner mode, Tesla load, and cooling duty cycle.
- Only after enough data, add optional Super veto/cooldown rules.

## F2Pool Profitability

F2Pool profitability automation starts read-only. It logs account, worker, hashrate, stale-share, and estimated-revenue snapshots so the scheduler can build a normal BTC-per-TH-hour baseline before any 24-hour Eco/Super profitability boost is allowed.

Use a least-privilege token. The account/profitability poll needs `legacy/api_v1`; settled Rewards history needs `ASSETS_MODULE` -> `assets/transactions/list`. The hashrate read permissions can remain limited to `hash_rate/info`, `hash_rate/history`, `hash_rate/worker/list`, `hash_rate/worker/history`, and optionally `hash_rate/info_list`. Do not give the scheduler wallet, withdraw, payment, threshold, distribution, add, delete, pause, resume, terminate, or transaction-accelerator permissions.

One-time local setup:

```powershell
[Environment]::SetEnvironmentVariable("F2POOL_API_TOKEN", "<token>", "User")
[Environment]::SetEnvironmentVariable("F2POOL_MINING_USER", "<mining user>", "User")
```

Close and reopen PowerShell, then test:

```powershell
python -m avalon_q_scheduler.main f2pool-account
python -m avalon_q_scheduler.main f2pool-log-once
python -m avalon_q_scheduler.main f2pool-sync-rewards --dry-run
```

`f2pool-sync-rewards` calls F2Pool v2 `assets/transactions/list` with `type: revenue` and writes the current Rewards rows to `f2pool.historical_settlement_csv_path`. Those rows are what the Study Hub paid-trend graph should use. It does not print the API token or raw provider payload. If the token lacks `assets/transactions/list`, the command returns a clear missing-permission message instead of falling back to estimates.

To seed rough Eco/Super baselines from an exported F2Pool BTC settlement CSV:

```powershell
python -m avalon_q_scheduler.main f2pool-import-settlements --dry-run
python -m avalon_q_scheduler.main f2pool-import-settlements
```

The default import uses the checked-in private-repo seed file at `data/f2pool_btc_settlements_2025-07-01_2026-06-22.csv`. You can still pass `--csv "C:\path\to\export.csv"` for a newer export. The import is conservative: daily average hashrate between `50` and `65` TH/s is treated as Eco, `90` to `110` TH/s is treated as Super, and everything else is skipped as mixed, partial, or off-ish. Historical seed rows are useful for initial ballpark baselines, but live 10-minute samples are still the cleaner source of truth.

The CSV import prints both raw daily earnings and normalized earnings. Raw sats/day is the intuitive Eco vs Super payout comparison. Normalized `BTC/TH-hour` is deliberately divided by hashrate, so Eco and Super can look close there; it is meant to detect when the pool/network is paying unusually well for each unit of hashing.

To collect snapshots in the scheduler, set:

```yaml
f2pool:
  enabled: true
  api_v2_base_url: "https://api.f2pool.com/v2"
  network_context_enabled: true
  network_context_api_base_url: "https://mempool.space/api/v1"
  network_context_pool_slug: "f2pool"
  network_status_page_url: "https://www.f2pool.com/coin/bitcoin"
  rewards_backfill_days: 14
  poll_interval_seconds: 600
  snapshots_log_path: "outputs/f2pool_profitability.jsonl"
  historical_settlement_csv_path: "data/f2pool_btc_settlements_2025-07-01_2026-06-22.csv"
  baseline_window_days: 14
  baseline_min_samples_per_mode: 24
  shadow_enter_super_ratio: 2.0
  shadow_hold_super_ratio: 1.4
  shadow_exit_super_ratio: 1.3
  historical_seed_baseline_window_days: 365
  csv_seed_eco_min_hashrate_th_s: 50
  csv_seed_eco_max_hashrate_th_s: 65
  csv_seed_super_min_hashrate_th_s: 90
  csv_seed_super_max_hashrate_th_s: 110
```

Every poll records the current miner mode from `state.json`, the change in F2Pool `value_today` since the previous poll, estimated BTC/hour, normalized BTC/TH-hour, the current Eco or Super baseline, and a shadow signal such as `collecting_baseline`, `normal`, `would_enter_super`, `would_hold_super`, or `would_return_to_eco`. F2Pool's account endpoint provides the account and worker fields. The scheduler first enriches the snapshot from F2Pool's public Bitcoin Network Status page for global hashrate, current difficulty, estimated next difficulty, estimated difficulty change, and retarget ETA, then falls back to mempool.space estimates if F2Pool's page is unavailable. If F2Pool captures the exact network fields but not pool-wide hashrate, the scheduler supplements only that missing pool-hashrate field from mempool.space `estimatedHashrate` and marks it as a mempool public estimate. Exact F2Pool pool hashrate is only recorded when F2Pool exposes that field in the captured payload; the fallback estimate is not mislabeled as F2Pool. Each F2Pool poll also attempts a Rewards sync through v2 `assets/transactions/list` so local dashboards can graph real settled pool-day payouts instead of `value_today` estimates. Samples that cross a mode change are logged but not used in the baseline.

Per-mode baseline samples only count when `state.json` knows the current mode. Normal scheduler-driven Eco/Super changes update that state. Manual changes made only in the Avalon app may make the next F2Pool snapshot show `miner_mode: null` or stale mode until the scheduler commands the miner again.

This integration does not change miner modes yet. The shadow signal is there so we can watch it for roughly two weeks before allowing active profitability-driven Eco/Super changes. See [PROJECT_PLAN.md](PROJECT_PLAN.md) for the future decision rules.

Logs go to console and `avalon_q_scheduler.log`. State is saved to `state.json`.
Running `decide-now` replaces the saved weather decision for that date. Evening logic ignores weather state from previous dates.

Rain/storm weather codes only force Off when the daytime forecast also reaches both the configured probability and precipitation thresholds. Forecast precipitation amounts independently require the configured probability confidence threshold; if probability data is unavailable, a meaningful precipitation amount can still be used by itself. This avoids shutting the miner off for inconsistent forecasts that show thunderstorm codes alongside zero precipitation or show an isolated precipitation amount with very low probability.

## Windows Scheduled Task

Create a task that starts the scheduler at login:

```powershell
$Project = "C:\path\to\CodexAvalonScheduler"
$Python = "$Project\.venv\Scripts\python.exe"
$Action = New-ScheduledTaskAction -Execute $Python -Argument "-m avalon_q_scheduler.main run" -WorkingDirectory $Project
$Trigger = New-ScheduledTaskTrigger -AtLogOn
Register-ScheduledTask -TaskName "Avalon Q Scheduler" -Action $Action -Trigger $Trigger -Description "Weather and solar-window scheduler for Canaan Avalon Q" -User $env:USERNAME
```

## Keep Miner IP Stable

Reserve the miner's LAN IP in Google Home or your router DHCP settings so the IP does not change. In Google Home, look for Wi-Fi device settings, select the miner, and create a DHCP/IP reservation for its current address.

## Safety

`Super`/high mode may pull significant power. Verify the outlet, wiring, circuit rating, breaker, plug, extension cords, and ambient cooling are safe before enabling `dry_run: false`.

## Profiling and Performance Notes

These measurements were taken locally on 2026-07-29 with Python 3.12.13 on Windows AMD64. The pure-function and file-parsing measurements used `timeit.repeat` after one warm-up call. Results will vary with the Python build, processor, filesystem cache, and background load. Network, provider, and physical-device latency was deliberately excluded.

| Operation | Samples | Iterations per sample | Average | Min | Max | Measurement scope |
|---|---:|---:|---:|---:|---:|---|
| Parse [`config.example.yaml`](config.example.yaml) with `load_config` | 10 | 100 | 3.417 ms | 3.200 ms | 4.076 ms | YAML parsing plus construction of the immutable application configuration |
| Evaluate one in-memory morning weather decision | 10 | 100,000 | 0.652 us | 0.627 us | 0.696 us | Fixed sunny forecast; no file, device, or network access |
| Parse the checked-in settlement CSV | 10 | 100 | 3.887 ms | 3.295 ms | 4.998 ms | Reads and normalizes 337 valid settlement rows |
| Run the complete pytest process | 5 | 1 | 1.263 s | 1.188 s | 1.354 s | Includes interpreter and pytest startup; all 112 tests passed in every sample |

The test-suite measurement used:

```powershell
python -B -m pytest -q -p no:cacheprovider
```

The three application measurements exercise `load_config`, `DecisionEngine.morning_weather_decision`, and `_read_settlement_csv` against the public example configuration, a fixed in-memory forecast, and the checked-in CSV respectively. They are local compute measurements; they do not estimate end-to-end scheduler latency.

### Compute Cost and Bottlenecks

Let `C` be the number of weather codes in one forecast, `B` the size of the YAML configuration, `R` the number of settlement rows, and `H` the number of accumulated profitability-history records.

| Path | Cost shape | Primary constraint |
|---|---|---|
| Weather policy evaluation | `O(C)` time and constant auxiliary memory | A small fixed set of thresholds and weather codes |
| Configuration loading | `O(B)` time and memory | YAML parsing at process startup |
| Settlement CSV parsing | `O(R)` time and memory | Full-file parsing into normalized row dictionaries |
| Profitability snapshot construction | `O(H)` time and memory | The JSONL history is loaded into a list, then scanned for the prior sample and per-mode baseline |
| Device and provider calls | Dominated by external I/O | Synchronous TCP/HTTP calls are bounded by configured timeouts, not by the sub-millisecond policy calculation |

The main growth-sensitive path is profitability history: `load_profitability_records` reads the complete JSONL file, and baseline construction makes linear passes over those records. If that log grows materially, a bounded streaming window, compacted summaries, or indexed storage would reduce both memory and repeated scan cost. The scheduler limits interval jobs to one instance and coalesces missed executions, preventing slow external calls from creating an unbounded queue; configured polling intervals and request budgets also keep this a low-frequency control workload.

## Complete Sanitized Git Graph

The complete no-hash Git graph from [`DEVELOPMENT_HISTORY.md`](DEVELOPMENT_HISTORY.md) is reproduced below, newest to oldest.

```text
* 2026-07-17 | Document Avalon project operating rules [master]
* 2026-07-17 | Make Nest OAuth recovery automatic
* 2026-07-15 | Use grid-only Tesla charging adjustment
* 2026-07-12 | Add optional Tesla car charging adjustment
* 2026-07-07 | Add Nest OAuth relink command
* 2026-07-07 | Retry missed morning weather catchup
* 2026-07-07 | Fix missed morning Avalon wakeup
* 2026-07-03 | Supplement pool hashrate from mempool estimate
* 2026-07-03 | Require documentation checks before integration limits
* 2026-07-03 | Add Codex agent guardrails
* 2026-07-03 | Sync real F2Pool reward settlements
* 2026-07-03 | Capture public Bitcoin network context
* 2026-07-03 | Capture F2Pool network context fields
* 2026-06-29 | Add future local dashboard plan
* 2026-06-29 | Clarify F2Pool shadow baseline plan
* 2026-06-29 | Track F2Pool settlement CSV seed data
* 2026-06-29 | Seed F2Pool baselines from settlement CSV
* 2026-06-29 | Add F2Pool profitability shadow baselines
* 2026-06-29 | Add read-only F2Pool profitability snapshots
* 2026-06-29 | Add read-only Nest AC baseline monitoring
* 2026-06-29 | Add Powerwall-aware energy policy
* 2026-06-27 | Add late afternoon solar bridge
* 2026-06-27 | Add peak solar Super window
* 2026-06-26 | Refresh README [evening-low-change; pull/1/head]
* 2026-06-26 | Refresh config example
* 2026-06-26 | Refresh cheap power scheduling gate
* 2026-06-26 | Refresh schedule defaults
* 2026-06-26 | Update example config for current solar window
* 2026-06-26 | Document optional overnight cheap power window
* 2026-06-26 | Deduplicate scheduler test fakes
* 2026-06-26 | Deduplicate scheduler test fakes
* 2026-06-26 | Test optional cheap power window scheduling
* 2026-06-26 | Document optional overnight cheap power window
* 2026-06-26 | Update example config for current solar window
* 2026-06-26 | Gate overnight cheap power jobs behind config
* 2026-06-26 | Make overnight cheap power window configurable
* 2026-06-26 | Document 6 PM low schedule and remove overnight high window
* 2026-06-26 | Update scheduler job registration test
* 2026-06-26 | Stop scheduling overnight cheap power mode changes
* 2026-06-26 | Update example schedule for evening low only
* 2026-06-26 | Update evening low schedule defaults
* 2026-06-23 | Persist manual weather decisions
* 2026-06-22 | Require precipitation for storm codes
* 2026-06-20 | Handle Windows scheduler shutdown
* 2026-06-20 | Require confidence for rain amount
* 2026-06-20 | Fix manual softoff command
* 2026-06-20 | Retry mode changes after softon
* 2026-06-18 | Remove local defaults from example config
* 2026-06-18 | Initial Codex Avalon scheduler [root]
```
