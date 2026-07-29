# Avalon Q Scheduler Development History

This document is a public-safe rendering of the complete Git history preserved in the private Avalon Q Scheduler repository. It shows every commit, date, original commit subject, and preserved branch label. Commit hashes, author metadata, emails, and private deployment details are omitted.

AI assistance was used to extract, verify, and format this graph. The dates and subjects come from the preserved Git objects and required no subject redaction.

## Project Arc

Avalon developed quickly because the operating goal was concrete: control one known miner in one known home-energy environment, then test each policy against observed device and provider behavior. The history is a single line of iterative corrections and feature expansions. It moves from safe miner control, through weather and solar scheduling, into home-battery policy, read-only integrations, measured recovery behavior, and conservative external-service handling.

## How to Read the Graph

- `*` is a commit.
- The graph runs from newest to oldest.
- A branch label in brackets marks the preserved point associated with that branch.
- The preserved graph is linear: both real branch heads lie on one ancestor chain, and no two-parent merge commit is preserved.

## Branch and Ref Status

| Ref | Type | Preserved point | Result |
|---|---|---|---|
| `master` | Primary branch | 2026-07-17, `Document Avalon project operating rules` | Complete 49-commit development line. |
| `evening-low-change` | Branch label | 2026-06-26, `Refresh README` | Fully contained in `master`. It has no unique commits and `master` continues for 23 commits after it. |
| `pull/1/head` | GitHub PR archival ref | Same point as `evening-low-change` | Adds no unique commit and does not create a separate development line. |

The object graph is consistent with a fast-forward or simply continuing development from the branch tip. Git records the resulting linear topology, not which user-interface operation moved the branch pointer. There is no merge commit to claim.

## Complete Sanitized Git Graph

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

## Completeness Check

- Unique commits reachable from all refs: 49
- Commits reachable from `master`: 49
- Commits reachable from `evening-low-change`: 26
- Commits after the preserved branch label: 23
- Branch-only commits: 0
- Merge commits: 0
- Commit entries shown in the graph: 49
- Tags: 0

The complete private repository remains the source of truth. This document is a readable history summary, not a replacement Git history.
