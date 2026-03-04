# Tasks

## Active

- [ ] **Record demo video** — The single highest-leverage unlock. Community, visibility, and distribution all gate on this. Can't build audience without the "holy shit" moment on camera.
- [ ] **TASKS.md as source of truth** — Daily Backlog cron reads this file. Keep it tight: 3-5 Active max, everything else in Backlog or Done.

## Waiting On

- [ ] **Kalshi key rotation** — Current key `CURRENT_KEY...` works but MEMORY.md still references old key `OLD_KEY...`. Need to verify key hasn't expired + clean stale refs. (Waiting: next auth failure to confirm)

## Backlog

- [ ] **Evening briefing news quality** — ddgs news search works but quality varies. Consider adding Qwen local filtering (same Stage 2 pattern as X signals) to only surface material developments.
- [ ] **MEMORY.md cleanup** — Stale Kalshi credentials, outdated portfolio snapshot (Feb 17), "Active Issues" section missing. Should be pruned to current state.
- [ ] **Proactive agent observability** — No easy way to see what the materiality gate is filtering out. Add a daily digest log: "X scanner ran 12 times, passed 1 signal, blocked 11."
- [ ] **Community/visibility push** — Ship demo video first, then build audience. Sequencing issue, not a limitation.

## Dead

- ~~Voice (STT/TTS)~~ — Zero leverage, massive complexity. Input modality isn't the bottleneck.
- ~~iPhone companion~~ — Premature scaling. Mac-as-server works.
- ~~Robinhood~~ — No public API. Non-starter.
- ~~More tool depth~~ — Horizontal expansion before vertical depth is a trap.

## Done (all shipped in v0.8.0)

- [x] **Group chat support** — Multi-chat routing works.
- [x] **Push transport (BlueBubbles)** — BB Socket.IO, polling suspended.
- [x] **Proactive triggers (event/webhook)** — 7 triggers, single launchd service.
- [x] **Coinbase read-only** — CDP API key + handlers.
- [x] **Polymarket read-only** — Gamma + CLOB APIs.
- [x] **Cross-platform comparator** — Kalshi vs PM divergence alerts.
- [x] **X signal scanner** — DDG + local Qwen, zero API cost.
- [x] **Kalshi edge scanner** — Spread + distance-from-50 heuristic.
- [x] **Morning brief** — Unified daily digest.
- [x] **Unified proactive agent** — 7 triggers, PID lockfile.
- [x] **iMessage formatting** — Markdown stripping on all outbound.
- [x] **X signal fail-closed fix** — Stage 2 materiality gate fails closed, interval 30m→120m, confidence 0.7→0.8.
- [x] **Kalshi briefing auth fix** — Removed hardcoded stale key, reads from config.yaml.
- [x] **Evening briefing search fix** — Replaced web_search (needs Brave key) with ddgs (free).
