"""
risk_manager.py — position cap, loss limits and stake validation

* The stake is EXACTLY what the user configured (Deriv Rise/Fall contracts use a
  stake in account currency, not forex lots). It is never silently changed — if it
  is invalid or unaffordable the order is refused with a clear message.
* Risk gates: daily loss limit, consecutive-loss limit, empty balance, position cap
  (counts every order that occupies a slot, including ones still being confirmed).
* Realized results only: floating profit never affects these checks.
"""
from config import (
    MAX_DAILY_LOSS_PCT, MIN_STAKE, MAX_STAKE, MAX_OPEN_TRADES,
    LOT_TO_USD, MAX_CONSECUTIVE_LOSSES,
)
from database import get_daily_pnl, get_active_trades, get_consecutive_losses, get_exposure
from logger import log


class RiskManager:
    def __init__(self, balance, user_id=None, mode="demo",
                 lot_size=1.0, max_positions=1):
        self.balance       = float(balance or 0)
        self.user_id       = user_id
        self.mode          = mode
        self.lot_size      = float(lot_size)        # stake per trade (as configured by the user)
        self.max_positions = int(max_positions)
        self.pause_reason  = ""
        self._last_logged  = ""

    def update_balance(self, b):
        self.balance = float(b)

    def update_settings(self, lot_size=None, max_positions=None):
        if lot_size is not None:
            self.lot_size = float(lot_size)
            log.info("Stake per trade updated -> %.2f", self.lot_size)
        if max_positions is not None:
            self.max_positions = max(1, min(int(max_positions), MAX_OPEN_TRADES))
            log.info("Max open trades updated -> %d", self.max_positions)

    # ── gates ────────────────────────────────────────────────
    def check(self):
        """(ok, reason). `reason` is '' when trading is allowed."""
        if self.balance <= 0:
            return self._set("Account balance is 0 — fund the account before trading")
        pnl   = get_daily_pnl(self.user_id, self.mode)
        limit = -(self.balance * MAX_DAILY_LOSS_PCT / 100.0)
        if pnl <= limit:
            return self._set("Daily loss limit reached ({:.2f}, limit {:.1f}% of balance)".format(
                pnl, MAX_DAILY_LOSS_PCT))
        losses = get_consecutive_losses(self.user_id, self.mode)
        if losses >= MAX_CONSECUTIVE_LOSSES:
            return self._set("{} losses in a row today — paused until tomorrow (UTC)".format(losses))
        return self._set("")

    def _set(self, reason):
        self.pause_reason = reason
        if reason and reason != self._last_logged:
            log.warning("Risk protection: %s", reason)
        self._last_logged = reason
        return (reason == ""), reason

    def can_trade(self):
        return self.check()[0]

    def positions_open(self):
        return len(get_active_trades(self.user_id, self.mode))

    def positions_available(self):
        return max(0, self.max_positions - self.positions_open())

    # ── stake ────────────────────────────────────────────────
    def stake_checked(self):
        """(stake, error). The stake is the user's setting — never silently altered."""
        stake = round(self.lot_size * LOT_TO_USD, 2)
        if stake < MIN_STAKE:
            return None, "Stake {:.2f} is below the minimum of {:.2f}".format(stake, MIN_STAKE)
        if stake > MAX_STAKE:
            return None, "Stake {:.2f} is above the maximum of {:.2f}".format(stake, MAX_STAKE)
        exp = get_exposure(self.user_id, self.mode)
        if stake + exp["open_stake"] > self.balance + 1e-9:
            return None, ("Stake {:.2f} + {:.2f} already open exceeds the account balance {:.2f}"
                          .format(stake, exp["open_stake"], self.balance))
        return stake, ""

    def get_stake(self):
        stake, _ = self.stake_checked()
        return stake

    def daily_loss_pct(self):
        pnl = get_daily_pnl(self.user_id, self.mode)
        return round((pnl / self.balance) * 100, 2) if self.balance else 0.0
