# BotCoin Changelog

## v2.2.1 — 2026-06-20 — Update-flow safety (infra only, no trading-logic change)
- Rewrote update.sh into a safe, idempotent, self-healing host update:
  - Builds new images BEFORE recreating containers, so a build failure leaves
    the old stack fully running (fixes the 2026-06-20 down-then-failed-build
    incident that left zero containers).
  - Idempotency guard: no-op if local VERSION already matches remote.
  - Health-checks /api/health for up to 60s after the swap; on failure, rolls
    back to the previous commit and rebuilds. Loud manual-recovery breadcrumb
    if even the rollback is unhealthy.
  - Single-flight flock; timestamped append log at logs/update.log with rotation.
  - Writes data/update.status (JSON) for the dashboard to poll.
- Hardened install-update-watcher.sh: idempotent, ensures dirs, reloads cron.
- api.py: /api/update docstring corrected (it only drops a marker); added
  /api/update-status endpoint that reports the host update state.
- No regime, order-placement, pricing, threshold, or snapshot code touched.

## v2.1.0 — 2026-06-14 — v1 strategy removed
- STRATEGY_VERSION env var deleted; v2 is the only execution path
- Removed v1 USD-mode functions: usd_spike_sell_tier{1,2,3}, usd_dca_sell, usd_recycler_buy, usd_recycler_resell
- Removed v1/v2 selector validation from /api/settings
- Historical trade reason labels retained for DB backward compatibility

## v1.5.2 — 2026-06-13
- Fix cost-basis drift: avg_cost_basis is now computed from the confirmed-buy
  ledger at fill-confirmation time (in the reconciler), not from a live balance
  at order-placement time. Stacked resting maker buys no longer skew the basis.
  Includes a one-time backfill that recomputes basis on every historical snapshot.
- Partial maker fills now record only the executed volume (vol_exec), not the
  full requested size.
- Removed paper-trading mode entirely — the bot has a single real-execution path.
  (The historical paper_trade DB column is retained for old rows.)
- Removed the dashboard Quick Buy feature and its /api/buy endpoint.

## v1.0.0 — 2026-03-21
- Initial release
- DCA buying with daily/weekly/monthly frequency
- Three-tier dip buying system
- Recycler sell/rebuy strategy
- Auto mode (200-day MA based switching)
- Two-server architecture (bot + dashboard)
- Quick Buy BTC from dashboard
- Light/dark mode toggle
- One-click update from dashboard
