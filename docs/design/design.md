# Design notes — DBA

Durable design/architecture docs for this project, committed to the repo so they travel with the code (distinct from `HANDOFF.md`, which is transient work-stream state, and from this assistant's own `Brain/Note Pad/dba/`, which is workspace-level AI memory that isn't part of this repo).

| File | Covers | Status |
| --- | --- | --- |
| [architecture.md](./architecture.md) | Layered-monolith structure (`bot/`/`services/`/`data/`/`core/`/`phase/`); the `services/` never imports `discord`, `data/repositories/` owns all SQL invariant; `Announcer` protocol | Current |
| [trade-logic-rules.md](./trade-logic-rules.md) | CPU trade evaluator + proposal generator rule specs (B1-B8): posture gating, archetype/role fit, asset upside valuation, safety-gate parity across incoming/outgoing-first paths | Current |
| [trade-proposal-restructure.md](./trade-proposal-restructure.md) | Swap-aware bidirectional trade initiation (incoming-first vs outgoing-first modes), `pick_proposal_modes` dispatcher | Shipped 2026-05-22 |
| [columnist-ride-along.md](./columnist-ride-along.md) | Attach-only sidecar + file-IPC design for pausing sims to capture live feedback on columnist articles mid-run | Shipped 2026-05-21 — superseded in practice by the Discord-reply feedback capture feature (`ae7cd97`), which coexists with the sidecar rather than replacing it; see `HANDOFF.md` |
| [persona-redesign.md](./persona-redesign.md) | 8-persona voice/renderer redesign — headline-dedup defense-in-depth, cadence trigger changes | Current — drafted, not yet implemented |

Shipped docs are kept as historical design record — they explain *why* a shipped feature is shaped the way it is, which git log alone doesn't answer well. Update a doc in place if its subject's architecture changes again; don't append a new dated entry to the same file (that's what git history is for).
