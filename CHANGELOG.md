# BotCoin Changelog

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
