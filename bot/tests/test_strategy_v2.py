"""
test_strategy_v2.py — Unit tests for the Strategy v2.0 decision logic.

These exercise the PURE logic only (no Kraken, no live network): the regime
detector's transition function, the Universal Recycler's volatility-adaptive
bands, the Harvest gate/exit/sustain math, the 3-band operating-regime
classifier, and the v2 config defaults + validation. The database is pointed at
a throwaway temp file so the modules that touch bot_state work in isolation.

Run from the bot/ directory:  python -m unittest tests.test_strategy_v2
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

# Make the bot package importable when run as `python -m unittest` from bot/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Dummy Kraken creds so config.load_config() (which requires them) succeeds.
os.environ.setdefault("KRAKEN_API_KEY", "test")
os.environ.setdefault("KRAKEN_API_SECRET", "test")

import config  # noqa: E402
import database  # noqa: E402
import regime_detector as rd  # noqa: E402
import universal_recycler as ur  # noqa: E402
import harvest  # noqa: E402
from mode_manager import OperatingRegime, get_operating_regime  # noqa: E402


def _fresh_db():
    """Point database at a throwaway file and initialise the schema."""
    database.DB_PATH = Path(tempfile.mktemp(suffix=".db"))
    database.init_db()


class ConfigDefaultsTest(unittest.TestCase):
    def setUp(self):
        self.cfg = config.load_config()

    def test_strategy_version_defaults_to_v1(self):
        # The single most important safety default: a bot with no STRATEGY_VERSION
        # set must stay on the legacy v1 engine.
        self.assertEqual(self.cfg.strategy_version, "v1")

    def test_harvest_bands_ordered(self):
        # Exit must sit below the entry threshold, both above parity with the MA.
        self.assertLess(self.cfg.harvest_exit_pct, self.cfg.harvest_threshold_pct)
        self.assertGreater(self.cfg.harvest_exit_pct, 1.0)

    def test_locked_caps(self):
        self.assertEqual(self.cfg.harvest_fire_cap_pct, 0.02)
        self.assertEqual(self.cfg.harvest_total_cap_pct, 0.33)
        self.assertEqual(self.cfg.recycler_time_limit_days, 90)


class RegimeTransitionTest(unittest.TestCase):
    def setUp(self):
        self.cfg = config.load_config()

    def test_volatile_high_break_ignites_breakout(self):
        out = rd._next_regime(self.cfg, rd.REGIME_CHOP,
                              self.cfg.breakout_atr_multiplier + 0.1, True, False)
        self.assertEqual(out, rd.REGIME_BREAKOUT_UP)

    def test_volatile_low_break_ignites_breakdown(self):
        out = rd._next_regime(self.cfg, rd.REGIME_CHOP,
                              self.cfg.breakdown_atr_multiplier + 0.1, False, True)
        self.assertEqual(out, rd.REGIME_BREAKDOWN)

    def test_break_without_vol_spike_stays_chop(self):
        # A break with calm volatility is not an ignition.
        out = rd._next_regime(self.cfg, rd.REGIME_CHOP, 1.0, True, False)
        self.assertEqual(out, rd.REGIME_CHOP)

    def test_active_regime_relaxes_to_cooling_when_calm(self):
        out = rd._next_regime(self.cfg, rd.REGIME_BREAKOUT_UP, 1.0, False, False)
        self.assertEqual(out, rd.REGIME_COOLING)

    def test_fresh_break_interrupts_cooling(self):
        # Highest priority: a fresh volatile break re-ignites from cooling.
        out = rd._next_regime(self.cfg, rd.REGIME_COOLING,
                              self.cfg.breakdown_atr_multiplier + 0.1, False, True)
        self.assertEqual(out, rd.REGIME_BREAKDOWN)


class RecyclerBandTest(unittest.TestCase):
    def test_calm_band_is_tightest(self):
        self.assertEqual(ur._bands_for(0.5), (-0.02, 0.03))

    def test_storm_band_is_widest_and_reachable(self):
        # Q-new-4: the Storm bucket must be reachable from the RAW ratio (>1.6).
        self.assertEqual(ur._bands_for(2.0), (-0.06, 0.09))

    def test_nonpositive_ratio_falls_back_to_normal(self):
        self.assertEqual(ur._bands_for(0.0), (-0.03, 0.05))

    def test_vol_state_names(self):
        self.assertEqual(ur._vol_state_name(0.5), "calm")
        self.assertEqual(ur._vol_state_name(2.0), "storm")


class OperatingRegimeTest(unittest.TestCase):
    def setUp(self):
        _fresh_db()
        self.cfg = config.load_config()
        # Seed 120 DISTINCT daily-price rows (calculate_200ma needs >=100 days).
        # record_daily_price UPSERTs by calendar day, so insert dated rows
        # directly to simulate a real multi-month history at a flat $100.
        from datetime import datetime, timezone, timedelta
        base = datetime.now(timezone.utc)
        with database.get_connection() as conn:
            for i in range(120):
                day = (base - timedelta(days=i)).strftime("%Y-%m-%d")
                conn.execute(
                    "INSERT OR REPLACE INTO daily_prices (date, price_usd) "
                    "VALUES (?, ?)", (day, 100.0))

    def test_below_exit_is_accumulate(self):
        # Price well under the MA → keep stacking.
        self.assertEqual(get_operating_regime(self.cfg, 90.0),
                         OperatingRegime.ACCUMULATE)

    def test_between_bands_is_neutral(self):
        price = 100.0 * ((self.cfg.harvest_exit_pct + self.cfg.harvest_threshold_pct) / 2)
        self.assertEqual(get_operating_regime(self.cfg, price),
                         OperatingRegime.NEUTRAL)

    def test_above_threshold_is_harvest(self):
        price = 100.0 * (self.cfg.harvest_threshold_pct + 0.05)
        self.assertEqual(get_operating_regime(self.cfg, price),
                         OperatingRegime.HARVEST)


class HarvestGateTest(unittest.TestCase):
    def setUp(self):
        _fresh_db()
        self.cfg = config.load_config()
        self.ma200 = 100.0

    def test_below_exit_resets_and_returns_none(self):
        price = self.ma200 * (self.cfg.harvest_exit_pct - 0.01)
        out = harvest.evaluate(self.cfg, price, self.ma200,
                               btc_balance=1.0, avg_cost_basis=50.0,
                               regime=rd.REGIME_CHOP)
        self.assertIsNone(out)

    def test_gate_met_but_not_sustained_returns_none(self):
        # First tick crossing the gate: sustain timer not yet satisfied.
        price = self.ma200 * (self.cfg.harvest_threshold_pct + 0.01)
        out = harvest.evaluate(self.cfg, price, self.ma200,
                               btc_balance=1.0, avg_cost_basis=50.0,
                               regime=rd.REGIME_CHOP)
        self.assertIsNone(out)

    def test_stack_below_floor_suppressed(self):
        # Even sustained, a near-zero rebuild stack is never harvested. Force the
        # sustain window open by backdating the threshold-crossed timestamp.
        from datetime import datetime, timezone, timedelta
        database.set_state(
            "harvest_threshold_since_ts",
            (datetime.now(timezone.utc)
             - timedelta(days=self.cfg.harvest_sustain_days + 1)).isoformat())
        price = self.ma200 * (self.cfg.harvest_threshold_pct + 0.01)
        out = harvest.evaluate(self.cfg, price, self.ma200,
                               btc_balance=self.cfg.harvest_min_stack_btc / 2,
                               avg_cost_basis=50.0, regime=rd.REGIME_CHOP)
        self.assertIsNone(out)


if __name__ == "__main__":
    unittest.main()
