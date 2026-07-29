# Public Review Snapshot

Prepared: 2026-07-27

## Context and AI Assistance

Avalon Q Scheduler is a personal home-automation project that I direct and operate in my own deployment. I set the operating goals, safety boundaries, and deployment requirements and evaluate the scheduler against the real system; AI-assisted coding tools were used during implementation, testing, documentation, and iterative review. AI assistance was also used to audit and sanitize this public snapshot and to create the linked sanitized development history.

The public repository began as a sanitized snapshot and keeps a deliberately short, publication-only history. The private working history contains home-deployment details, machine paths, device and service identifiers, credential-handling notes, runtime observations, and operating instructions that were never intended for a public, general-purpose distribution. Rather than rewrite that history commit by commit, this repository presents a reviewed snapshot that preserves the scheduler source, tests, example configuration, technical design, and functional intent.

The project can run after its dependencies are installed, but it is not a turnkey copy of my deployment. Another installation requires compatible local hardware plus its own configuration, network details, and provider credentials.

## Preserved Development History

[`DEVELOPMENT_HISTORY.md`](DEVELOPMENT_HISTORY.md) renders all 49 preserved commits with their dates, original subjects, and real parent relationships. It explains that `evening-low-change` is an older label on the same linear history, not an unmerged branch, and that no merge commit exists. Hashes, author metadata, emails, and private deployment details are omitted.

## Publication Boundary

This snapshot includes the scheduler source, tests, example configuration, technical documentation, and representative baseline data. It intentionally excludes the complete private Git history, local credentials, token stores, machine-specific `config.yaml`, runtime state, logs, generated outputs, and private operating instructions.

The complete operational repository remains preserved privately. This public snapshot is not the deployment remote used by the running scheduler.

Copyright (c) 2026 Harrison Doyle. All rights reserved.
