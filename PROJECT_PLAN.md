# Avalon Q Scheduler Project Plan

This file tracks the next major automation layers that are not fully active yet.

## F2Pool Profitability Signal

Goal: allow the miner to move from Eco to Super at any hour when mining is paying unusually well, while keeping hard safety protections in place.

### API Token Setup

Use the least powerful F2Pool token that can answer the first read-only questions.

The first implementation uses API v1 because one endpoint returns account, worker, hashrate, stale, and estimated revenue data:

- Check `legacy/api_v1`.
- Do not check `All permissions`.
- Do not check wallet, withdrawal, payment, threshold, distribution, add, delete, pause, resume, or terminate permissions.

If the token also includes API v2 read-only permissions, keep them limited to:

- `hash_rate/info`
- `hash_rate/history`
- `hash_rate/worker/list`
- `hash_rate/worker/history`
- `hash_rate/info_list`, optional for multi-account reads
- `assets/balance`, optional for estimated today/yesterday revenue
- `assets/transactions/list`, required for settled Rewards/history rows shown in Study Hub

Do not enable these for automation unless there is a specific reason:

- `mining_user/wallet/update`
- `mining_user/balance/withdraw`
- `mining_user/payment/pause`
- `mining_user/payment/resume`
- `mining_user/threshold/update`
- `hash_rate/distribute`
- `hash_rate/distribution/terminate`
- `revenue/distribution/add`
- `revenue/distribution/delete`
- `tx_acc/tx_acc`

Trusted IP is a good extra lock if the scheduler computer uses a stable public IP. If the public IP changes, F2Pool API calls may fail until the token allowlist is updated. The token itself must not be committed to git; store it in an environment variable or a local ignored file.

Planned local values:

- `F2POOL_API_TOKEN`: the read-only F2Pool API token
- `F2POOL_MINING_USER`: the BTC mining account/sub-account name
- Currency: `bitcoin`
- Historical CSV seed file: `data/f2pool_btc_settlements_2025-07-01_2026-06-22.csv`

### Data To Pull

F2Pool v1 account endpoint:

- `value_today`
- `hashrate`
- `hashrate_history`
- `hashrate_history_stale`
- `hashes_last_hour`
- `hashes_last_day`
- `worker_length_online`
- `workers`
- stale/rejected hash fields

F2Pool Rewards history:

- v2 `assets/transactions/list` with `type: revenue`
- settlement time from `mining_extra.settle_date`
- paid amount from `changed_balance`
- average pool-day hashrate from `mining_extra.hash_rate`
- optional difficulty/comment fields when F2Pool returns them
- write the current rows to `f2pool.historical_settlement_csv_path` for Study Hub
- do not treat `value_today`, `value_last_day`, `today_paid`, or balance changes as settled payout rows

Bitcoin network signal:

- F2Pool public Bitcoin Network Status page for global hashrate, current difficulty, estimated next difficulty, estimated difficulty change, and retarget ETA
- exact F2Pool pool hashrate only when F2Pool exposes it in the captured payload
- mempool.space remains a fallback estimate, and should be labeled as an estimate
- BTC/USD price, optional

Avalon local API:

- local `summary`
- local `stats`
- miner errors or unhealthy readings

### Baseline Strategy

Do not compare raw daily revenue directly. Super naturally earns more than Eco because it hashes harder. Compare normalized earnings instead:

```text
current BTC per TH-hour / normal BTC per TH-hour baseline
```

The first baseline is conservative and read-only:

- poll F2Pool every 10 minutes
- write snapshots to `outputs/f2pool_profitability.jsonl`
- maintain Eco and Super baselines separately
- compare current Eco samples only to the Eco baseline, and current Super samples only to the Super baseline
- ignore the first warm-up period after a mode change
- compute the derivative of `value_today` between polls
- record BTC/hour and normalized BTC/TH-hour
- reject baseline samples that cross a mode change
- count samples toward Eco/Super baselines only when `state.json` knows the current mode
- keep Off/unknown samples out of the Eco/Super baselines
- optionally seed rough Eco/Super baselines from F2Pool daily settlement CSV exports using conservative hashrate thresholds
- log what the scheduler would do before making profitability changes active
- do not let F2Pool shadow signals change miner mode during the first roughly two-week observation period

Current shadow output terms:

- `collecting_rate`: no usable previous sample yet.
- `collecting_baseline`: rate exists, but the current mode does not have enough baseline samples.
- `normal`: rate is within the normal range.
- `would_enter_super`: Eco is paying at or above the configured entry threshold.
- `would_hold_super`: Super is still paying above the configured hold threshold.
- `would_return_to_eco`: Super has fallen below the configured exit threshold.

Starting thresholds:

- Enter Super shadow signal at `2.0x` Eco baseline.
- Hold Super shadow signal at `1.4x` Super baseline.
- Return-to-Eco shadow signal below `1.3x` Super baseline.
- Use a 14-day baseline window.
- Require at least 24 usable samples for a mode before trusting its baseline.
- Historical CSV seed rows use a separate 365-day seed window by default.

### Planned Decision Rules

Hard safety rules should still win:

- grid outage or island/off-grid status means preserve Powerwall
- miner command errors or health problems mean do not force Super
- rapid mode flipping is blocked by minimum hold/cooldown times
- Powerwall reserve is treated as hard even though Tesla Electric usually enforces it already

AC cooling is not a hard veto at first. It is a cost signal:

- log HVAC cooling state beside miner mode and profitability
- use it later to estimate whether Super is causing enough extra AC runtime to erase the mining benefit

Planned profitability behavior:

- enter Super when normalized profitability is at least `2.0x` baseline
- also consider entering Super when next difficulty estimate is strongly negative, for example `-7%` or worse, if the live normalized earnings are already elevated
- stay in Super while normalized profitability remains at least `1.4x`
- drop back to Eco after normalized profitability stays below `1.3x` for 30-60 minutes
- never switch modes more often than the configured minimum mode-change interval
- later, consider a paid-grid override only when profitability is unusually high and Tesla/monthly-credit guardrails say the economics still make sense
- do not allow a future profitability override to burn through the monthly VPP/export credit target, for example the expected monthly credit floor

### Implementation Phases

1. Add read-only F2Pool client and CLI commands. Done.
2. Add snapshot logging, derivative rate, baseline calculation, and shadow decisions. Done.
3. Watch the shadow decisions for about two weeks.
4. Add F2Pool API v2 Rewards sync for settled pool-day payout history. Done.
5. Add active Eco/Super changes after the shadow logs look sane.

## Local Dashboard / Home Automation Console

Goal: eventually replace the habit of keeping F2Pool, Tesla, Google Home, and miner tabs open by showing the same useful status in one local dashboard, plus the scheduler-specific signals those sites do not show.

This is a future phase. The dashboard should read from existing logs and APIs first; it should not change miner behavior until the underlying automation rules are already trusted.

### Hosting Options

- Keep this as a small dashboard inside Avalon Q Scheduler if it is only for miner/energy automation.
- Or expose Avalon Q Scheduler status as JSON and let another local web project, such as the existing Codex Word Game app/portal, render the dashboard on its existing port.
- Prefer a read-only dashboard first. Mode-change buttons can come later, behind dry-run/state checks.

### F2Pool View

Recreate the useful parts of the F2Pool BTC page:

- total income, total payout, balance, yesterday revenue, and today's estimated revenue if available from API v1/v2
- current hashrate, hashes last hour/day, worker count, online workers, and stale/rejected hash signals
- recent settlement rows from the checked-in CSV and future API/CSV imports: settlement time, average hashrate, amount, difficulty, and description
- raw sats/day and BTC/day alongside normalized BTC/TH-hour so Eco-vs-Super payout is easy to understand
- current profitability ratio against the correct mode baseline
- shadow signal: `normal`, `would_enter_super`, `would_hold_super`, or `would_return_to_eco`
- baseline samples by mode, baseline age, and whether current samples are being accepted or rejected

Useful extra dashboard signals that F2Pool does not show directly:

- Eco baseline and Super baseline shown separately
- current derivative of `value_today`: BTC/hour and sats/hour
- current normalized earning rate vs Eco/Super baseline
- a clear "would have triggered Super" marker during the two-week shadow period
- notes explaining why a sample did not count, such as unknown mode, off mode, too soon after mode change, or sample crossing a mode change

### Miner / Avalon View

Show enough Avalon local API status to avoid opening the Canaan app unless something looks wrong:

- current known scheduler mode: Eco, Super, or Off
- last commanded mode, previous mode, last command time, and last command reason
- last raw Avalon command and response
- read-only `version`, `summary`, and `stats` health fields
- softoff/softon state if known
- command failures, calibration/wake retries, and miner rejection messages

### Tesla / Powerwall View

Show the energy context that decides whether Super is cheap or expensive:

- battery percentage and reserve floor
- solar production, home load, grid import/export, and Powerwall charge/discharge if available
- monthly net export/import and VPP/export credit guardrail status
- whether paid-grid override is currently allowed, blocked, or only theoretical
- current scheduler reason for allowing Eco/Super/Off

### Nest / AC View

Show AC as a cost signal rather than a hard veto at first:

- current HVAC state, indoor temperature, setpoints, humidity, and thermostat mode
- cooling runtime baseline by time of day/season when enough samples exist
- whether AC is likely increasing the effective cost of Super
- later, estimated extra AC cost caused by Super heat

### Dashboard Phases

1. Add a read-only CLI summary command, for example `f2pool-dashboard`, that prints the important F2Pool/profitability stats without opening Chrome.
2. Add a machine-readable status command or endpoint that returns the current scheduler/F2Pool/Tesla/Nest state as JSON.
3. Add a small local dashboard page that renders the combined status.
4. Decide whether the dashboard lives in this repo or is consumed by the existing local web portal/project.
5. Only after the shadow logs look sane, add optional controls or active profitability overrides.
