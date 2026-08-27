"""
risk_manager.py — ClaudeFX v2.0 position sizing + loss limits

- Stake is sized off RISK_PER_TRADE_PCT (1%) of balance by default.
  Note: this bot trades Deriv CALL/PUT (Rise/Fall) contracts, which have a
  fixed payout at expiry rather than a linear price-to-P&L relationship —
  there's no exact "stake such that hitting the SL loses exactly 1% of
  balance" for that contract type. Risking `balance * 1%` as the stake
  itself is the standard simplification (worst case on a Rise/Fall
  contract is losing the full stake).
- The dashboard's manual lot_size still works as a ceiling — if the user
  sets a smaller lot size than the 1%-risk stake, their setting wins.
- Trading pauses for the day after MAX_CONSECUTIVE_LOSSES losses in a row,
  or after MAX_DAILY_LOSS_PCT daily loss — both from the strategy doc.
"""
from config import (
    MAX_DAILY_LOSS_PCT, MIN_STAKE, MAX_STAKE,
    LOT_TO_USD, DEFAULT_STAKE, RISK_PER_TRADE_PCT, MAX_CONSECUTIVE_LOSSES,
)
from database import get_daily_pnl, get_open_trades, get_consecutive_losses
from logger import log


class RiskManager:
    def __init__(self, balance, user_id=None, mode="demo",
                 lot_size=1.0, max_positions=1):
        self.balance       = balance
        self.user_id       = user_id
        self.mode          = mode           # "demo" or "live" — keeps risk limits isolated per mode
        self.lot_size      = lot_size       # user-selected lot size (now acts as a ceiling)
        self.max_positions = max_positions  # user-selected max concurrent trades
        self._paused       = False

    def update_balance(self, b):
        self.balance = b

    def update_settings(self, lot_size=None, max_positions=None):
        """Update lot size and max positions live."""
        if lot_size is not None:
            self.lot_size = max(MIN_STAKE / LOT_TO_USD, float(lot_size))
            log.info("Lot size updated → %.2f", self.lot_size)
        if max_positions is not None:
            self.max_positions = max(1, int(max_positions))
            log.info("Max positions updated → %d", self.max_positions)

    def _daily_ok(self):
        if self.balance <= 0:
            if not self._paused:
                log.warning("Account balance is $0.00 — nothing to risk. Fund this "
                            "account on app.deriv.com before trading. Bot paused.")
            self._paused = True
            return False

        pnl   = get_daily_pnl(self.user_id, self.mode)
        limit = -(self.balance * MAX_DAILY_LOSS_PCT / 100)
        if pnl <= limit:
            if not self._paused:
                log.warning("Daily loss limit reached ($%.2f, %.0f%% of balance, %s mode). Bot paused.",
                            pnl, MAX_DAILY_LOSS_PCT, self.mode.upper())
            self._paused = True
            return False
        self._paused = False
        return True

    def _trades_ok(self):
        """Check how many more positions we can open."""
        open_count = len(get_open_trades(self.user_id, self.mode))
        if open_count >= self.max_positions:
            log.info("Max positions reached (%d/%d, %s mode).",
                     open_count, self.max_positions, self.mode.upper())
            return False
        return True

    def _consecutive_losses_ok(self):
        losses = get_consecutive_losses(self.user_id, self.mode)
        if losses >= MAX_CONSECUTIVE_LOSSES:
            if not self._paused:
                log.warning("Max consecutive losses reached (%d, %s mode). Bot paused for today.",
                            losses, self.mode.upper())
            self._paused = True
            return False
        return True

    def can_trade(self):
        return self._daily_ok() and self._trades_ok() and self._consecutive_losses_ok()

    def positions_available(self):
        """How many more positions can be opened right now."""
        open_count = len(get_open_trades(self.user_id, self.mode))
        return max(0, self.max_positions - open_count)

    def get_stake(self):
        """
        Risk-based stake: RISK_PER_TRADE_PCT of balance, capped by the
        user's manual lot-size setting (whichever is smaller wins),
        clamped between MIN_STAKE/MAX_STAKE, and never more than 5% of balance.
        """
        risk_stake = round(self.balance * RISK_PER_TRADE_PCT / 100, 2)
        user_cap   = round(self.lot_size * LOT_TO_USD, 2)
        stake = min(risk_stake, user_cap) if user_cap > 0 else risk_stake
        if stake <= 0:
            stake = DEFAULT_STAKE
        stake = max(MIN_STAKE, min(stake, MAX_STAKE))
        max_allowed = self.balance * 0.05
        if max_allowed > MIN_STAKE:
            stake = min(stake, max_allowed)
        log.info("Stake=$%.2f (risk=%.1f%% of $%.2f, lot cap=%.2f)",
                 stake, RISK_PER_TRADE_PCT, self.balance, self.lot_size)
        return stake

    def daily_loss_pct(self):
        pnl = get_daily_pnl(self.user_id, self.mode)
        return round((pnl / self.balance) * 100, 2) if self.balance else 0.0
