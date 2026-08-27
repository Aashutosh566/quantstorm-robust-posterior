"""
engine.py - Divided Oracle: Core Game Engine
=============================================

Self-contained game engine with full constraint enforcement, timing,
and anti-cheat measures.

WHAT THIS MODULE GUARANTEES:
  - Every return value is validated and clamped to legal ranges
  - Bot exceptions are caught and handled (bot forfeits that action)
  - A call that overruns HARD_TIME_LIMIT_MS is abandoned and counted as a
    violation; enough violations forfeit the match to the opponent
  - Obs objects are frozen with immutable fields, GameConfig is read-only
  - No lookahead: bots only see coins revealed up to the current round
  - Zero-sum invariant is asserted after every deal, forfeits included
  - Score invariance is asserted (powers cannot change S)

WHAT IT DOES NOT GUARANTEE:
  Called directly, this module runs bot code IN THIS PROCESS. A bot that
  walks the call stack can read `hands` and `score` out of play_deal's
  frame, and an infinite loop hangs the caller: a timeout cannot interrupt
  a running Python frame from inside the same thread. For untrusted code
  use `sandbox.SandboxedBot`, which runs each bot in its own process where
  none of this module's locals exist. That is what `--isolate` reproduces,
  and what the tournament runner must use.
  See sandbox.py for the full threat model.

USAGE:
    from engine import play_deal, play_match
    from game_config import GameConfig

    config = GameConfig()
    result = play_match(BotA, BotB, config, seed=42, mirror=True)
    print(result.pnl)
"""

from __future__ import annotations

import hashlib
import random
import time
from typing import Any, Callable, Sequence

from game_config import (
    Contract,
    DealResult,
    GameConfig,
    MatchResult,
    Obs,
    DEFAULT_CONFIG,
)


class BotTimeout(Exception):
    """Raised by an isolated bot adapter when a call overruns its budget.

    The engine treats this as a forfeited action plus a counted violation,
    which is different from an ordinary exception (forfeited action only).
    """


# ════════════════════════════════════════════════════════════════════
#  DETERMINISTIC SEED DERIVATION
# ════════════════════════════════════════════════════════════════════

def derive_seed(*parts) -> int:
    """Derive a deterministic seed from multiple components.

    Concatenates string representations, hashes with BLAKE2b,
    and returns a 64-bit integer. Guarantees reproducibility.
    """
    key = "|".join(str(p) for p in parts).encode()
    h = hashlib.blake2b(key, digest_size=8)
    return int.from_bytes(h.digest(), "little")


# ════════════════════════════════════════════════════════════════════
#  TIME TRACKER
# ════════════════════════════════════════════════════════════════════

class TimeTracker:
    """Records per-call timings, counts violations, and checks budgets."""

    def __init__(self, label: str, config: GameConfig):
        self.label = label
        self.config = config
        self.call_times: list[float] = []   # milliseconds per call
        self.warnings: list[str] = []       # things the author should fix
        self.violations = 0                 # calls over the hard limit
        # Routine, legal clamps -- a quote re-centred into the width band,
        # a counter pulled back inside the current range. Counted rather than
        # listed: a competent bot trips these many times a match, and burying
        # the real warnings under hundreds of them is how a genuine problem
        # goes unnoticed.
        #
        # An over-budget bid vector is counted here too, but it is ALSO
        # warned about, because it is no longer routine: under the zeroing
        # rule it costs the bot that round's auction outright.
        self.clamps = 0

    def record(self, elapsed_ms: float, call_label: str = ""):
        """Record a single call's timing and check limits."""
        self.call_times.append(elapsed_ms)
        if elapsed_ms > self.config.HARD_TIME_LIMIT_MS:
            self.violations += 1
            msg = (
                f"[{self.label}] {call_label}: {elapsed_ms:.1f}ms "
                f"EXCEEDS hard limit ({self.config.HARD_TIME_LIMIT_MS}ms)"
            )
            self.warnings.append(msg)

    def last_ms(self) -> float:
        """Duration of the most recent recorded call, or 0.0 if there is none.

        A bot whose reset() raised never records a call, so this must not
        assume the list is non-empty -- reading call_times[-1] directly used
        to crash the backtester with an opaque IndexError on the single most
        common participant mistake.
        """
        return self.call_times[-1] if self.call_times else 0.0

    @property
    def total_ms(self) -> float:
        return sum(self.call_times)

    @property
    def avg_ms(self) -> float:
        return self.total_ms / len(self.call_times) if self.call_times else 0.0

    @property
    def max_ms(self) -> float:
        return max(self.call_times) if self.call_times else 0.0

    @property
    def over_budget(self) -> bool:
        return self.avg_ms > self.config.TIME_BUDGET_MS


# ════════════════════════════════════════════════════════════════════
#  POWER STATE TRACKER
# ════════════════════════════════════════════════════════════════════

class PowerState:
    """Tracks power ownership, persistence, and one-shot consumption."""

    def __init__(self):
        # Per-round powers (cleared each round)
        self.round_powers: dict[int, set[str]] = {0: set(), 1: set()}
        # Persistent powers (carry across rounds within a deal)
        self.persistent: dict[int, set[str]] = {0: set(), 1: set()}
        # Rounds where SUBSTITUTE is active for each seat
        self.substitute_rounds: dict[int, set[int]] = {0: set(), 1: set()}
        # One-shot powers consumed this deal
        self.used: set[str] = set()
        self.current_round: int = 0

    def new_round(self, r: int):
        """Reset per-round state for a new round."""
        self.round_powers = {0: set(), 1: set()}
        self.current_round = r

    def acquire(self, seat: int, name: str, r: int, config: GameConfig):
        """Record that seat won power 'name' in round r."""
        pspec = config.POWERS[name]
        if pspec.get("once_per_deal", False):
            if name in self.used:
                return  # Already consumed; silently ignore
            self.used.add(name)
            if name == "STEALTH_ROCK":
                self.persistent[seat].add(name)
        self.round_powers[seat].add(name)
        if name == "SUBSTITUTE":
            self.substitute_rounds[seat].add(r)

    def consumed(self, name: str, config: GameConfig) -> bool:
        """Check if a one-shot power has been used this deal."""
        return config.POWERS[name].get("once_per_deal", False) and name in self.used

    def active(self, seat: int, r: int) -> set[str]:
        """Powers affecting seat in round r: this round's wins + persistent."""
        cur = self.round_powers[seat] if r == self.current_round else set()
        return cur | self.persistent[seat]


# ════════════════════════════════════════════════════════════════════
#  BOT WRAPPER — validates & sanitises every bot call
# ════════════════════════════════════════════════════════════════════

class UnbuiltBot:
    """Stand-in for a submission whose constructor raised.

    Deliberately defines none of the five methods, so BotWrapper takes the
    documented fallback for every call and the deal is scored instead of
    abandoned. Building the bot was the one unguarded call in the harness:
    a ``Bot.__init__`` that raised propagated out of ``play_match``, taking
    down the opponent's result and the rest of the match with it.
    """

    def __init__(self, error: BaseException):
        self.error = error


def build_bot(factory) -> Any:
    """Construct one bot, turning a failed constructor into a scored loss."""
    try:
        return factory()
    except BaseException as e:
        return UnbuiltBot(e)


class BotWrapper:
    """Wraps a bot instance with timing, validation, and error handling.

    Every call to the bot is:
      1. Timed (ms recorded)
      2. Wrapped in try/except (exceptions → safe default)
      3. Return value validated and clamped to legal range
      4. Warnings logged for illegal values, timeouts, exceptions
    """

    def __init__(self, bot, name: str, config: GameConfig):
        self.bot = bot
        self.name = name
        self.config = config
        self.timer = TimeTracker(name, config)
        self.failed = False  # Set True if reset() fails
        if isinstance(bot, UnbuiltBot):
            # Say which exception it was. The organiser otherwise sees only
            # a bot that fell back on every single call, with nothing to
            # hand the author when they ask why.
            self.failed = True
            self.timer.warnings.append(
                f"[{name}] could not be constructed: "
                f"{type(bot.error).__name__}: {bot.error}"
            )

    # -- call plumbing --------------------------------------

    def _call(self, method: str, label: str, *args):
        """Invoke one bot method, timing it and normalising its failures.

        Returns (ok, value). ``ok`` is False when the bot raised, timed out
        or does not implement the method, in which case the caller applies
        that method's documented fallback.

        An isolated bot reports the time it actually spent computing via
        ``last_call_ms``; the wall clock here would also include IPC, and
        charging a bot for the harness's own overhead would make the 2ms
        budget unmeetable for reasons the participant cannot control.
        """
        fn = getattr(self.bot, method, None)
        if fn is None:
            self.timer.warnings.append(f"{method}() is not implemented")
            return False, None

        start = time.perf_counter()
        try:
            value = fn(*args)
        except BotTimeout as e:
            # A killed call always counts as a violation, whatever the clock
            # says, and it ends this bot's deal: its worker was destroyed
            # mid-frame, so it has no state left to answer with. Every
            # remaining action takes its documented fallback.
            self.timer.violations += 1
            self.timer.warnings.append(f"[{self.name}] {label}: timed out ({e})")
            self.failed = True
            return False, None
        except Exception as e:
            self.timer.warnings.append(
                f"{method}() {label} raised {type(e).__name__}: {e}"
            )
            return False, None

        elapsed = getattr(self.bot, "last_call_ms", None)
        if elapsed is None:
            elapsed = (time.perf_counter() - start) * 1000
        self.timer.record(elapsed, label)
        return True, value

    # -- reset ----------------------------------------------

    def do_reset(self, seat: int, config: GameConfig, seed: int):
        """Call bot.reset() with error handling."""
        ok, _ = self._call("reset", "reset", seat, config, seed)
        if not ok:
            self.failed = True

    # -- bid ------------------------------------------------

    def do_bid(self, obs: Obs, offered: list[str]) -> dict[str, int]:
        """Call bot.bid() with full validation.

        Validates:
          - Returns a dict
          - Keys are from the offered list only
          - Values are non-negative integers
          - Total does not exceed obs.te_mine (every bid zeroed if it does)
          - Individual values clamped to MAX_REASONABLE_VALUE
        """
        if self.failed:
            return {}

        # Give bot a COPY of the offered list so it can't mutate ours
        ok, result = self._call("bid", f"bid R{obs.round}", obs, list(offered))
        if not ok:
            return {}

        return self._sanitise_bid(result, obs, offered)

    def _sanitise_bid(self, result: Any, obs: Obs, offered: list[str]) -> dict[str, int]:
        """Validate and clamp a bid dict."""
        if not isinstance(result, dict):
            self.timer.warnings.append(
                f"bid() returned {type(result).__name__}, expected dict → empty"
            )
            return {}

        clean: dict[str, int] = {}
        for name in offered:
            if name not in result:
                continue
            val = result[name]

            # Must be numeric
            if not isinstance(val, (int, float)):
                self.timer.warnings.append(
                    f"bid[{name}] = {type(val).__name__}, expected int → 0"
                )
                continue

            # Convert to int, clamp to [0, MAX_REASONABLE_VALUE]. int() is
            # not total over the numeric types: int(float("inf")) raises
            # OverflowError and int(float("nan")) raises ValueError, and
            # neither was caught anywhere up the stack, so one infinite bid
            # took the whole match down with it.
            try:
                val = int(val)
            except (OverflowError, ValueError):
                self.timer.warnings.append(
                    f"bid[{name}] = {val!r} is not a finite number → ignored"
                )
                continue
            if val < 0:
                self.timer.warnings.append(
                    f"bid[{name}] = {val} negative → clamped to 0"
                )
                val = 0
            val = min(val, self.config.MAX_REASONABLE_VALUE)
            clean[name] = val

        # Discard keys not in offered
        extra = set(result.keys()) - set(offered)
        if extra:
            self.timer.warnings.append(
                f"bid() contained keys not in offered: {extra} → ignored"
            )

        # Enforce total <= te_mine. A vector that does not fit is ZEROED, not
        # rescaled: the bot bid something it could not pay for, and the engine
        # does not guess which part of it was meant.
        #
        # Rescaling was the old rule and it quietly did the bot's job for it.
        # A vector scaled to fit still wins auctions, so "bid an absurd number"
        # was a legal way to spell all-in without tracking a budget -- and
        # because the scaled result was truncated, the engine also chose which
        # power lost its last tick. Zeroing keeps the allocation decision where
        # it belongs and makes an over-budget vector cost the round's auction.
        total = sum(clean.values())
        if total > obs.te_mine:
            self.timer.clamps += 1
            self.timer.warnings.append(
                f"bid() totalled {total} against te_mine={obs.te_mine} "
                f"→ every bid zeroed; no power contested this round"
            )
            return dict.fromkeys(clean, 0)

        return clean

    # -- quote ----------------------------------------------

    def do_quote(self, obs: Obs) -> tuple[int, int]:
        """Call bot.quote() with full validation.

        Validates:
          - Returns a tuple/list of exactly 2 values
          - Both are integers
          - bid <= ask
          - final_cap <= spread (ask - bid) <= spread_cap
          - Values clamped to [-MAX, +MAX]

        The width is the Maker's to choose anywhere in that band, and it is
        priced at settlement by WIDTH_PREMIUM. The fallback for a crashed or
        timed-out quote is the full cap, unchanged: it is the passive choice in
        the negotiation, and it pays the premium like anyone else's.
        """
        cap = obs.spread_cap
        if self.failed:
            # Default: centre on 0 with max spread
            return (-(cap // 2), cap - (cap // 2))

        ok, result = self._call("quote", f"quote R{obs.round}", obs)
        if not ok:
            return (-(cap // 2), cap - (cap // 2))

        return self._sanitise_quote(result, obs)

    def _sanitise_quote(self, result: Any, obs: Obs) -> tuple[int, int]:
        """Validate and clamp an opening quote."""
        MAX = self.config.MAX_REASONABLE_VALUE
        cap = obs.spread_cap

        # Must be a sequence of 2. KeyError catches a dict return, whose
        # result[0] is a missing key rather than a missing index; OverflowError
        # catches int(float("inf")). Both used to escape this handler and
        # abort the whole match from inside the harness.
        try:
            bid, ask = int(result[0]), int(result[1])
        except (TypeError, IndexError, ValueError, KeyError, OverflowError):
            self.timer.warnings.append(
                f"quote() returned invalid type {type(result).__name__} → default"
            )
            return (-(cap // 2), cap - (cap // 2))

        # Clamp to reasonable range
        bid = max(-MAX, min(bid, MAX))
        ask = max(-MAX, min(ask, MAX))

        # Ensure bid <= ask
        if bid > ask:
            self.timer.warnings.append(
                f"quote() bid {bid} > ask {ask} → swapped"
            )
            bid, ask = ask, bid

        # Enforce the width band [final_cap, spread_cap]: re-centre if outside.
        # The floor matters as much as the cap -- the obligation pays out in
        # inverse proportion to the straddle rate the quoted width can achieve,
        # so an unfloored hair-thin quote would collect almost the whole
        # obligation on the occasions it landed.
        floor = obs.final_cap
        width = ask - bid
        if not (floor <= width <= cap):
            target = cap if width > cap else floor
            mid = (bid + ask) // 2
            bid = mid - target // 2
            ask = bid + target
            self.timer.warnings.append(
                f"quote() spread {width} outside [{floor}, {cap}] → re-centred at {target}"
            )

        # Keep the quote within reach of a score that can actually occur.
        # S is a sum of N_COINS fair +/-1 coins, so |S| <= N_COINS. A quote
        # lying entirely outside that band cannot straddle anything: it is
        # not a price, it is an offer to fill the counterparty at a number
        # the game can never reach. MAX_REASONABLE_VALUE bounds the
        # arithmetic but not the damage -- at 10000 it left room for a
        # six-figure transfer in a single match, which is enough to decide
        # the standings between two bots that never played each other well
        # or badly. The Maker's chosen width is preserved; the quote slides
        # rigidly to the edge of the reachable band.
        reach = self.config.N_COINS
        if bid > reach or ask < -reach:
            width = ask - bid
            if bid > reach:
                bid, ask = reach, reach + width
            else:
                bid, ask = -reach - width, -reach
            self.timer.warnings.append(
                f"quote() sits outside the achievable score range "
                f"[-{reach}, {reach}] → slid to ({bid}, {ask})"
            )

        return (bid, ask)

    # -- respond --------------------------------------------

    def do_respond(
        self, obs: Obs, quote: tuple[int, int], turn: int
    ) -> str | tuple[str, int, int]:
        """Call bot.respond() with full validation.

        Validates:
          - Returns "ACCEPT_BUY", "ACCEPT_SELL", or ("COUNTER", bid, ask)
          - COUNTER bid/ask are integers
          - COUNTER range is inside previous bounds
          - COUNTER shrinks spread by at least MIN_REDUCTION
          - Invalid actions → clamped to legal counter or ACCEPT_BUY
        """
        if self.failed:
            return "ACCEPT_BUY"

        ok, result = self._call(
            "respond", f"respond R{obs.round} T{turn}", obs, quote, turn
        )
        if not ok:
            return "ACCEPT_BUY"

        return self._sanitise_response(result, quote, obs, turn)

    # -- use_transform --------------------------------------

    def do_use_transform(self, obs: Obs) -> bool:
        """Ask the winner of the TRANSFORM auction whether to fire it.

        Winning TRANSFORM buys the OPTION to swap hands, not the swap
        itself. Declining is a legitimate and often correct play: it is how
        a player holding a decisive hand defends it, because the power is
        consumed either way and cannot then be bought by the opponent.

        Fallback on a missing method, a crash or a timeout is False --
        decline, i.e. leave the board untouched. That is the neutral
        outcome: a bot that never implements this can lose the option value
        it paid for, but can never be tricked into handing away a good hand.
        """
        if self.failed:
            return False
        ok, result = self._call("use_transform", f"use_transform R{obs.round}", obs)
        if not ok:
            return False
        # bool() runs the object's own __bool__, which is submission code and
        # can therefore raise. Declining is the documented fallback for a
        # crash here, so a __bool__ that throws gets exactly that.
        try:
            decision = bool(result)
        except Exception as e:
            self.timer.warnings.append(
                f"use_transform() return value raised "
                f"{type(e).__name__} when tested for truth → declined"
            )
            return False
        if not isinstance(result, bool):
            self.timer.warnings.append(
                f"use_transform() returned {type(result).__name__}, "
                f"expected bool -> treated as {decision}"
            )
        return decision

    def _sanitise_response(
        self, result: Any, quote: tuple[int, int], obs: Obs, turn: int
    ) -> str | tuple[str, int, int]:
        """Validate and clamp a negotiation response."""
        bid, ask = quote

        # Check for accept actions
        if isinstance(result, str):
            if result in ("ACCEPT_BUY", "ACCEPT_SELL"):
                return result
            self.timer.warnings.append(
                f"respond() returned unknown string {result!r} → ACCEPT_BUY"
            )
            return "ACCEPT_BUY"

        # Must be a tuple/list starting with "COUNTER"
        try:
            kind = result[0]
            nb, na = int(result[1]), int(result[2])
        except (TypeError, IndexError, ValueError, KeyError, OverflowError):
            self.timer.warnings.append(
                f"respond() returned unparseable {type(result).__name__} → ACCEPT_BUY"
            )
            return "ACCEPT_BUY"

        if kind != "COUNTER":
            self.timer.warnings.append(
                f"respond() action {kind!r} unknown → ACCEPT_BUY"
            )
            return "ACCEPT_BUY"

        # Clamp to reasonable range
        MAX = self.config.MAX_REASONABLE_VALUE
        nb = max(-MAX, min(nb, MAX))
        na = max(-MAX, min(na, MAX))

        # Ensure nb <= na
        if nb > na:
            nb, na = na, nb
            self.timer.warnings.append("respond() counter bid > ask → swapped")

        # Enforce: new range inside old range
        nb = max(bid, min(nb, ask))
        na = max(nb, min(na, ask))

        # Enforce: shrink by at least MIN_REDUCTION, but never below the round's
        # final cap. Reduction used to apply unconditionally, so a Maker that
        # opened tight watched its range squeezed to nothing by the last turn
        # and ended on a locked price it never chose. Convergence now stops
        # where the pricing math says the range stops carrying information.
        floor = obs.final_cap
        max_width = min(ask - bid, max(floor, (ask - bid) - self.config.MIN_REDUCTION))
        width = na - nb
        if not (floor <= width <= max_width):
            target = max_width if width > max_width else floor
            mid = (nb + na) // 2
            nb = max(bid, mid - target // 2)
            na = min(ask, nb + target)
            nb = max(bid, na - target)

        return ("COUNTER", nb, na)


# ════════════════════════════════════════════════════════════════════
#  COIN GENERATION & SPLITTING
# ════════════════════════════════════════════════════════════════════

def make_coins(rng: random.Random, config: GameConfig) -> list[int]:
    """Draw one coin vector: N_COINS independent fair +1/-1 values."""
    return [1 if rng.getrandbits(1) else -1 for _ in range(config.N_COINS)]


def split_coins(
    coins: list[int], config: GameConfig, swap: bool
) -> tuple[list[int], list[int]]:
    """Split coins into two private hands.

    If swap is True, hands are exchanged (used for mirroring).
    """
    a = list(coins[: config.N_PRIVATE])
    b = list(coins[config.N_PRIVATE :])
    return (b, a) if swap else (a, b)


# ════════════════════════════════════════════════════════════════════
#  POWER EFFECTS
# ════════════════════════════════════════════════════════════════════

def apply_foresight(
    won: dict[int, set[str]],
    revealed: tuple[list[int], list[int]],
    config: GameConfig,
    rng: random.Random,
) -> dict[int, list[int]]:
    """FORESIGHT: secretly show a sample of the opponent's revealed coins.

    Returns the per-seat leak. The coins you were NOT shown are still
    mean-zero, so the honest estimator of their sum is the raw sample sum;
    scaling it up to hand size is unbiased with ~12x the variance.
    """
    leak: dict[int, list[int]] = {0: [], 1: []}
    for seat in (0, 1):
        if "FORESIGHT" in won[seat] and "FORESIGHT" in config.POWERS:
            k = int(config.POWERS["FORESIGHT"]["magnitude"])
            opp_revealed = revealed[1 - seat]
            if opp_revealed and k > 0:
                n_sample = min(k, len(opp_revealed))
                idx = rng.sample(range(len(opp_revealed)), n_sample)
                leak[seat] = [opp_revealed[i] for i in idx]
    return leak


def apply_transform(
    hands: list[list[int]],
    revealed: tuple[list[int], list[int]],
) -> None:
    """Swap the two private hands wholesale, revealed portion included.

    Mutates hands and revealed in place. Score S = sum(hands[0]) +
    sum(hands[1]) is invariant: this permutes the partition of the coins
    between the seats, not the multiset.

    Swapping only the UNREVEALED tails -- the obvious first design -- trades
    noise for noise: both tails are mean-zero and unseen, so the exchange is
    EV-neutral by symmetry while still announcing on the public tape that
    you held a flat hand. Swapping the revealed portion is what converts a
    zero-edge hand into a live one.
    """
    hands[0][:], hands[1][:] = list(hands[1]), list(hands[0])
    n0, n1 = len(revealed[0]), len(revealed[1])
    revealed[0][:] = hands[0][:n0]
    revealed[1][:] = hands[1][:n1]


def fill_shift(
    short_seat: int,
    powers: tuple[set[str], set[str]],
    config: GameConfig,
) -> int:
    """Net tick shift on a forced-midpoint fill.

    Each holder of TRICK_ROOM or STEALTH_ROCK pushes the fill their way.
    Opposing holders cancel exactly.
    """
    shift = 0
    for seat in (0, 1):
        sign = 1 if seat == short_seat else -1
        for name in ("TRICK_ROOM", "STEALTH_ROCK"):
            if name in powers[seat] and name in config.POWERS:
                shift += sign * int(config.POWERS[name]["magnitude"])
    return shift


def shift_sources(seat: int, active: set[str], config: GameConfig) -> int:
    """Total fill-shift magnitude a seat controls.

    Used by bots to decide if riding to forced fill beats the forcing fee.
    """
    return sum(
        int(config.POWERS[n]["magnitude"])
        for n in ("TRICK_ROOM", "STEALTH_ROCK")
        if n in active and n in config.POWERS
    )


def apply_settlement_powers(
    pnl: list[float],
    contracts: list[Contract],
    state: PowerState,
    config: GameConfig,
    score: int,
):
    """SUBSTITUTE: cap the holder's loss on that round's contract.

    Refund is paid by counterparty (zero-sum preserved).
    Profit is uncapped — the asymmetry is the design.
    """
    if "SUBSTITUTE" not in config.POWERS:
        return
    cap = float(config.POWERS["SUBSTITUTE"]["magnitude"])
    for c in contracts:
        for seat in (0, 1):
            if c.round in state.substitute_rounds[seat]:
                raw = (score - c.price) if c.long_seat == seat else (c.price - score)
                if raw < -cap:
                    refund = (-cap) - raw
                    pnl[seat] += refund
                    pnl[1 - seat] -= refund


# ════════════════════════════════════════════════════════════════════
#  NEGOTIATION
# ════════════════════════════════════════════════════════════════════

def trade_round(
    bots: tuple[BotWrapper, BotWrapper],
    obs: tuple[Obs, Obs],
    config: GameConfig,
    rng: random.Random,
    logs: list[str],
    verbose: bool,
) -> Contract:
    """Run one round's negotiation to a contract.

    The Maker opens; then up to N_TURNS-1 responses alternate.
    If no acceptance by the final turn, contract forces at midpoint.
    """
    r = obs[0].round
    maker = 0 if obs[0].is_maker else 1
    taker = 1 - maker

    # -- Maker opens with quote --
    bid, ask = bots[maker].do_quote(obs[maker])
    opening = {"maker_seat": maker, "open_bid": bid, "open_ask": ask}

    if verbose:
        logs.append(
            f"    Turn 1 (Maker {bots[maker].name}): "
            f"quote [{bid}, {ask}]  spread={ask - bid}"
            f"  [{bots[maker].timer.last_ms():.2f}ms]"
        )

    # -- Responses (turns 2 through N_TURNS) --
    speaker, last_quoter = taker, maker
    for turn in range(2, config.N_TURNS + 1):
        act = bots[speaker].do_respond(obs[speaker], (bid, ask), turn)

        if isinstance(act, str):
            # ACCEPT_BUY or ACCEPT_SELL
            if act == "ACCEPT_BUY":
                if verbose:
                    logs.append(
                        f"    Turn {turn} ({bots[speaker].name}): "
                        f"ACCEPT_BUY at ask={ask}"
                        f"  [{bots[speaker].timer.last_ms():.2f}ms]"
                    )
                return Contract(r, ask, speaker, forced=False, **opening)
            else:  # ACCEPT_SELL
                if verbose:
                    logs.append(
                        f"    Turn {turn} ({bots[speaker].name}): "
                        f"ACCEPT_SELL at bid={bid}"
                        f"  [{bots[speaker].timer.last_ms():.2f}ms]"
                    )
                return Contract(r, bid, 1 - speaker, forced=False, **opening)

        # COUNTER
        _, nb, na = act
        if verbose:
            logs.append(
                f"    Turn {turn} ({bots[speaker].name}): "
                f"COUNTER [{nb}, {na}]  spread={na - nb}"
                f"  [{bots[speaker].timer.last_ms():.2f}ms]"
            )

        bid, ask = nb, na
        last_quoter = speaker
        speaker = 1 - speaker

    # -- No acceptance: forced fill at midpoint --
    rule = config.MIDPOINT_SIDE_RULE
    if rule == "last_quoter_sells":
        short = last_quoter
    elif rule == "maker_sells":
        short = maker
    else:  # coin_flip
        short = rng.randrange(2)

    pw = (set(obs[0].powers_mine), set(obs[1].powers_mine))
    shift_val = fill_shift(short, pw, config)
    fill_price = (bid + ask) // 2 + shift_val

    if verbose:
        logs.append(
            f"    → FORCED FILL at midpoint {(bid + ask) // 2}"
            + (f" + shift {shift_val}" if shift_val else "")
            + f" = {fill_price}"
            + f"  |  short={bots[short].name}  long={bots[1-short].name}"
            + f"  |  forcer={bots[last_quoter].name}"
        )

    return Contract(
        r, fill_price, 1 - short, forced=True,
        forcer=last_quoter, shift=shift_val, **opening
    )


# ════════════════════════════════════════════════════════════════════
#  AUCTION
# ════════════════════════════════════════════════════════════════════

def run_auction(
    bots: tuple[BotWrapper, BotWrapper],
    obs_pre: tuple[Obs, Obs],
    offered: list[str],
    te: list[int],
    power_state: PowerState,
    config: GameConfig,
    sym: random.Random,
    tie_flip: int,
    r: int,
    auction_log: list[dict],
    logs: list[str],
    verbose: bool,
) -> dict[int, set[str]]:
    """Blind first-price auction. Highest bidder wins and pays their own bid.

    Equal NON-ZERO bids are settled by a coin flip drawn from the deal RNG,
    and the winner pays. Under the old refund-on-tie rule denial was free:
    bids are blind, but a deterministic opponent is predictable, so matching
    a known bid exactly destroyed the auction at zero cost -- and it was the
    only available defence against TRANSFORM. Pricing the tie removes both
    problems at once.

    Two zero bids are not a tie. Neither side wanted the power, and handing
    it over for 0 TE would give away a free power nearly every round.

    Each seat's bids are feasibility-checked against their TE budget.
    Returns dict of seat -> set of powers won.
    """
    won: dict[int, set[str]] = {0: set(), 1: set()}

    if not offered:
        return won

    # Get bids from both bots
    raw_bids = [
        bots[s].do_bid(obs_pre[s], list(offered))
        for s in (0, 1)
    ]

    if verbose:
        for s in (0, 1):
            bids_str = {k: v for k, v in raw_bids[s].items() if v > 0}
            logs.append(
                f"    {bots[s].name} bids: {bids_str if bids_str else '{}'}"
                f"  [{bots[s].timer.last_ms():.2f}ms]"
            )

    # Resolve auction: per-power, highest bidder wins and pays their bid
    for name in offered:
        b0 = raw_bids[0].get(name, 0)
        b1 = raw_bids[1].get(name, 0)

        tied = False
        if b0 == b1:
            if b0 == 0:
                continue  # nobody bid: no winner, no free power
            if config.TIE_RULE == "refund":
                continue  # legacy rule, kept for A/B comparison only
            winner = sym.randrange(2) ^ tie_flip
            tied = True
        else:
            winner = 0 if b0 > b1 else 1

        cost = raw_bids[winner].get(name, 0)

        if cost > te[winner]:
            # Can't afford after earlier wins this round
            continue

        te[winner] -= cost
        won[winner].add(name)
        auction_log.append(
            {"round": r, "seat": winner, "power": name, "cost": cost}
        )
        power_state.acquire(winner, name, r, config)

        if verbose:
            logs.append(
                f"    → {bots[winner].name} wins {name} for {cost} TE"
                + ("  (tie -> coin flip)" if tied else "")
            )

    if verbose and not any(won.values()):
        logs.append("    → No powers won (all ties or zero bids)")

    return won


# ════════════════════════════════════════════════════════════════════
#  SETTLEMENT
# ════════════════════════════════════════════════════════════════════

def apply_maker_obligation(
    contracts: list[Contract],
    config: GameConfig,
    pnl: list[float],
    score: int,
    logs: list[str],
    verbose: bool,
):
    """Charge the Maker for the quote it showed: its centre and its width.

    Transfer is scaled by the straddle rate achievable at the width the Maker
    actually quoted, p_w:
        Straddle (bid <= S <= ask): Taker pays Maker  λ x (1 - p_w)
        Miss:                      Maker pays Taker   λ x p_w
        ...and always:             Maker pays Taker   WIDTH_PREMIUM x excess

    where `excess` is the opening spread above the round's floor.

    An honest Maker breaks even exactly at EVERY width. Scored at the cap
    instead -- as it used to be -- the rule silently priced width along with
    honesty, and in one direction only: a tighter quote straddles less often
    against an unchanged bar, so it lost money for being tight no matter how
    well it was centred. That made maximum width the only sane opening and
    deleted half of what a quote is. The premium is what a wide quote pays for
    the option it buys instead.
    """
    lam = config.MAKER_OBLIGATION
    prem = config.WIDTH_PREMIUM
    if lam == 0 and prem == 0:
        return

    for c in contracts:
        if c.maker_seat < 0:
            continue
        width = c.open_ask - c.open_bid
        straddle = c.open_bid <= score <= c.open_ask
        amount = 0.0

        if lam:
            p_w = config.straddle_prob(c.round, width)
            amount += lam * (1.0 - p_w) if straddle else -lam * p_w
        if prem:
            amount -= prem * max(0, width - config.open_width_floor(c.round))

        pnl[c.maker_seat] += amount
        pnl[1 - c.maker_seat] -= amount

        if verbose:
            logs.append(
                f"    R{c.round} Maker obligation: "
                f"{'straddle' if straddle else 'miss'} at width {width} - "
                f"maker gets {amount:+.2f}"
            )


def apply_forcing_fee(
    contracts: list[Contract],
    config: GameConfig,
    pnl: list[float],
    logs: list[str],
    verbose: bool,
):
    """Charge the player who countered on the final turn.

    Paid to counterparty as a transfer (zero-sum preserved).
    """
    fee = config.FORCED_FILL_FEE
    if fee == 0:
        return

    for c in contracts:
        if c.forced and c.forcer >= 0:
            pnl[c.forcer] -= fee
            pnl[1 - c.forcer] += fee
            if verbose:
                logs.append(
                    f"    R{c.round} forcing fee: "
                    f"seat {c.forcer} pays {fee:.1f}"
                )


# ════════════════════════════════════════════════════════════════════
#  PLAY ONE DEAL
# ════════════════════════════════════════════════════════════════════

def build_obs(
    s: int,
    r: int,
    revealed: tuple[list[int], list[int]],
    te: list[int],
    config: GameConfig,
    maker_seat: int,
    power_state: PowerState,
    auction_log: list[dict],
    contracts: list[Contract],
    foresight: tuple[int, ...],
) -> Obs:
    """Construct a frozen Obs for seat s.

    Every field is immutable. The engine creates a fresh Obs for
    every bot call, so even if a bot were to find a way to mutate
    a contained object, it would not affect the game.
    """
    return Obs(
        seat=s,
        round=r,
        my_revealed=tuple(revealed[s]),
        te_mine=te[s],
        te_theirs=te[1 - s],
        spread_cap=config.spread_cap(r),
        final_cap=config.final_cap(r),
        is_maker=(s == maker_seat),
        powers_mine=frozenset(power_state.active(s, r)),
        powers_theirs=frozenset(power_state.active(1 - s, r)),
        # Both of these are handed over in the shape RULEBOOK §9 documents.
        # They used to be flattened -- the log to sorted key/value pairs and
        # each contract to a bare 4-tuple -- so `entry["power"]` raised
        # TypeError and `c.open_bid` did not exist. The engine catches both,
        # applies the fallback action and says nothing, so a bot written to
        # the published spec lost without ever seeing an error.
        #
        # The dicts are rebuilt per call rather than shared: a bot that
        # mutates what it is given must not reach the engine's own tape.
        auction_log=tuple(dict(d) for d in auction_log),
        contracts=tuple(contracts),
        foresight=tuple(foresight),
        n_unknown_both=config.unknown_to_both(r),
        n_turns=config.N_TURNS,
    )


def play_deal(
    bot_a_instance,
    bot_b_instance,
    coins: list[int],
    config: GameConfig,
    seed: int = 0,
    swap: bool = False,
    invert_roles: bool = False,
    verbose: bool = True,
    bot_a_name: str = "Bot_A",
    bot_b_name: str = "Bot_B",
) -> tuple[DealResult, BotWrapper, BotWrapper]:
    """Play one deal: 5 rounds of auction + negotiation, then settlement.

    Args:
        bot_a_instance: Fresh instance of bot A
        bot_b_instance: Fresh instance of bot B
        coins: Exact coin vector (length N_COINS, each +1 or -1)
        config: Game configuration
        seed: Random seed for this deal
        swap: If True, swap private hands (mirror mode)
        invert_roles: If True, invert Maker assignment (mirror mode)
        verbose: If True, generate step-by-step logs
        bot_a_name / bot_b_name: Display names

    Returns:
        (DealResult, BotWrapper_A, BotWrapper_B)
    """
    # Validate coins
    if len(coins) != config.N_COINS:
        raise ValueError(f"Expected {config.N_COINS} coins, got {len(coins)}")
    for i, c in enumerate(coins):
        if c not in (-1, 1):
            raise ValueError(f"Coin {i} is {c}, must be +1 or -1")

    logs: list[str] = []
    rng = random.Random(derive_seed("deal", seed, swap, invert_roles))

    # A second stream, deliberately NOT keyed on the mirror leg, for the
    # randomness that must MIRROR rather than be redrawn: which seat wins a
    # tied auction, and which coins FORESIGHT reveals.
    #
    # The mirror leg exists to cancel luck, and it can only do that if it
    # replays the same luck with the seats exchanged, exactly as it replays
    # the same coin vector. Redraw these per leg and two instances of the
    # same bot stop netting to zero over a mirrored match -- which is the
    # fairness property the mirror is there to provide, and the baseline
    # every measured power value is a difference against.
    sym = random.Random(derive_seed("deal-sym", seed))
    tie_flip = int(swap)

    # The round-by-round power slate, drawn once for the whole deal from a
    # stream keyed only on the deal seed -- so the direct leg and its mirror
    # face the same offers, and neither the seats nor how the deal is played
    # can move them. See GameConfig.draw_slate.
    slate = config.draw_slate(random.Random(derive_seed("deal-slate", seed)))

    # Split coins into private hands
    hands = list(split_coins(coins, config, swap))
    score = sum(hands[0]) + sum(hands[1])

    # Wrap bots with validation & timing
    bot_a_wrap = BotWrapper(bot_a_instance, bot_a_name, config)
    bot_b_wrap = BotWrapper(bot_b_instance, bot_b_name, config)
    bots = (bot_a_wrap, bot_b_wrap)

    # Reset bots (fresh state for this deal)
    for i, b in enumerate(bots):
        b.do_reset(i, config, derive_seed("bot", seed, i, swap, invert_roles))

    te = [config.TE_BUDGET, config.TE_BUDGET]
    power_state = PowerState()
    auction_log: list[dict] = []
    contracts: list[Contract] = []
    revealed: tuple[list[int], list[int]] = ([], [])

    if verbose:
        coin_str = "".join("+" if c == 1 else "-" for c in coins)
        logs.append(f"  Coins: {coin_str}  S={score}")
        a_hand = "".join("+" if c == 1 else "-" for c in hands[0])
        b_hand = "".join("+" if c == 1 else "-" for c in hands[1])
        logs.append(f"  {bot_a_name} hand: {a_hand}  |  {bot_b_name} hand: {b_hand}")
        if swap:
            logs.append("  [MIRROR: hands swapped]")
        if invert_roles:
            logs.append("  [MIRROR: roles inverted]")

    # -- Round loop -----------------------------------------
    for r in range(1, config.N_ROUNDS + 1):
        if verbose:
            logs.append(f"\n  - Round {r} {'-' * 50}")

        # Reveal coins for this round
        for s in (0, 1):
            lo = (r - 1) * config.REVEAL_PER_ROUND
            hi = lo + config.REVEAL_PER_ROUND
            revealed[s].extend(hands[s][lo:hi])

        if verbose:
            for s in (0, 1):
                new_coins = hands[s][(r-1)*config.REVEAL_PER_ROUND : r*config.REVEAL_PER_ROUND]
                new_str = " ".join(f"{c:+d}" for c in new_coins)
                logs.append(
                    f"  Reveal {bots[s].name}: [{new_str}]  "
                    f"sum_so_far={sum(revealed[s])}"
                )
            logs.append(f"  TE: {bots[0].name}={te[0]}  {bots[1].name}={te[1]}")

        # Determine maker for this round
        maker_seat = 0 if r % 2 == 1 else 1
        if invert_roles:
            maker_seat = 1 - maker_seat

        power_state.new_round(r)

        # Which powers are on the block (excluding consumed one-shots). The
        # slate itself was drawn at deal start; consumption is the only thing
        # play is allowed to remove from it.
        offered = [
            n for n in slate[r]
            if not power_state.consumed(n, config)
        ]

        if verbose:
            logs.append(f"\n  AUCTION (offered: {', '.join(offered) if offered else 'none'}):")

        # Build pre-auction obs
        obs_pre = tuple(
            build_obs(s, r, revealed, te, config, maker_seat,
                      power_state, auction_log, contracts, ())
            for s in (0, 1)
        )

        # Run auction
        won = run_auction(
            bots, obs_pre, offered, te, power_state,
            config, sym, tie_flip, r, auction_log, logs, verbose
        )

        # TRANSFORM: the winner holds an OPTION, and is asked to exercise it.
        # The power is consumed either way, so declining permanently removes
        # it from the deal -- that is what makes buying it as a defence work.
        for seat in (0, 1):
            if "TRANSFORM" not in won[seat]:
                continue
            decision_obs = build_obs(
                seat, r, revealed, te, config, maker_seat,
                power_state, auction_log, contracts, (),
            )
            fired = bots[seat].do_use_transform(decision_obs)
            if fired:
                apply_transform(hands, revealed)
            if verbose:
                logs.append(
                    f"    → {bots[seat].name} "
                    + ("FIRES TRANSFORM (hands swapped)" if fired
                       else "DECLINES TRANSFORM (hands unchanged, power spent)")
                )
            break  # once per deal, and a double swap would be a no-op

        # FORESIGHT is sampled after TRANSFORM so the leak describes the
        # hand the opponent actually ends the auction phase holding.
        leak = apply_foresight(won, revealed, config, sym)

        # Assert score invariance
        new_score = sum(hands[0]) + sum(hands[1])
        assert new_score == score, (
            f"Score changed from {score} to {new_score} after powers in round {r}"
        )

        if verbose:
            logs.append(
                f"\n  NEGOTIATION (Maker: {bots[maker_seat].name}):"
            )
            pw_str_0 = ", ".join(sorted(power_state.active(0, r))) or "none"
            pw_str_1 = ", ".join(sorted(power_state.active(1, r))) or "none"
            logs.append(
                f"    Powers: {bots[0].name}={{{pw_str_0}}}  "
                f"{bots[1].name}={{{pw_str_1}}}"
            )

        # Build post-auction obs (with FORESIGHT leak)
        obs_post = tuple(
            build_obs(s, r, revealed, te, config, maker_seat,
                      power_state, auction_log, contracts,
                      tuple(leak.get(s, ())))
            for s in (0, 1)
        )

        # Run negotiation
        contract = trade_round(bots, obs_post, config, rng, logs, verbose)
        contracts.append(contract)

        if verbose:
            long_name = bots[contract.long_seat].name
            short_name = bots[1 - contract.long_seat].name
            logs.append(
                f"    Contract: price={contract.price}  "
                f"long={long_name}  short={short_name}  "
                f"forced={'yes' if contract.forced else 'no'}"
            )

    # -- Settlement -----------------------------------------
    if verbose:
        logs.append(f"\n  {'-' * 58}")
        logs.append(f"  SETTLEMENT  (S = {score})")
        logs.append(f"  {'-' * 58}")

    pnl = [0.0, 0.0]

    # Raw PnL from contracts
    for c in contracts:
        raw = score - c.price
        pnl[c.long_seat] += raw
        pnl[1 - c.long_seat] -= raw
        if verbose:
            long_pnl = raw
            short_pnl = -raw
            logs.append(
                f"    R{c.round}: price={c.price}  S-price={raw:+d}  "
                f"long({bots[c.long_seat].name})={long_pnl:+d}  "
                f"short({bots[1-c.long_seat].name})={short_pnl:+d}"
            )

    if verbose:
        logs.append(
            f"    Raw PnL: {bots[0].name}={pnl[0]:+.1f}  "
            f"{bots[1].name}={pnl[1]:+.1f}"
        )

    # Maker obligation
    apply_maker_obligation(contracts, config, pnl, score, logs, verbose)

    # Forcing fees
    apply_forcing_fee(contracts, config, pnl, logs, verbose)

    # SUBSTITUTE settlement
    apply_settlement_powers(pnl, contracts, power_state, config, score)

    # TE salvage: unspent TE difference → PnL
    te_diff = (te[0] - te[1]) * config.TE_SALVAGE
    pnl[0] += te_diff
    pnl[1] -= te_diff

    if verbose:
        logs.append(
            f"    TE salvage: {bots[0].name} TE={te[0]}  "
            f"{bots[1].name} TE={te[1]}  - "
            f"{bots[0].name} gets {te_diff:+.2f}"
        )

    # Zero-sum assertion
    zs_err = abs(pnl[0] + pnl[1])
    if zs_err > config.ZERO_SUM_TOL:
        raise AssertionError(
            f"Deal is NOT zero-sum: {pnl[0]:+.9f} + {pnl[1]:+.9f} = "
            f"{pnl[0] + pnl[1]:.9f}"
        )

    if verbose:
        logs.append(f"    {'-' * 40}")
        logs.append(
            f"    FINAL PnL: {bots[0].name}={pnl[0]:+.2f}  "
            f"{bots[1].name}={pnl[1]:+.2f}  "
            f"{'✓ zero-sum' if zs_err < config.ZERO_SUM_TOL else '✗ NOT zero-sum'}"
        )

    result = DealResult(
        pnl=(pnl[0], pnl[1]),
        score=score,
        contracts=tuple(contracts),
        te_left=(te[0], te[1]),
        logs=logs,
    )

    return result, bot_a_wrap, bot_b_wrap


# ════════════════════════════════════════════════════════════════════
#  PLAY A MATCH (multiple deals + optional mirrors)
# ════════════════════════════════════════════════════════════════════

def play_match(
    bot_a_factory: Callable,
    bot_b_factory: Callable,
    config: GameConfig = DEFAULT_CONFIG,
    seed: int = 0,
    mirror: bool = True,
    n_deals: int | None = None,
    verbose: bool = False,
    bot_a_name: str = "Bot_A",
    bot_b_name: str = "Bot_B",
    fixed_coins: list[int] | None = None,
    first_maker: int | None = None,
) -> MatchResult:
    """Play a complete match: n_deals direct deals + n_deals mirrors.

    Args:
        bot_a_factory: Callable that returns a fresh bot A instance
        bot_b_factory: Callable that returns a fresh bot B instance
        config: Game configuration
        seed: Master random seed for the match
        mirror: Whether to play mirrored deals (hands swapped, roles inverted)
        n_deals: Deals per phase (default: config.DEALS_PER_PHASE)
        verbose: Generate step-by-step logs
        bot_a_name / bot_b_name: Display names
        fixed_coins: If set, use this exact coin vector for ALL deals
        first_maker: Override who opens round 1 (0 or 1)

    Returns:
        MatchResult with aggregate PnL and per-deal details
    """
    if n_deals is None:
        n_deals = config.DEALS_PER_PHASE

    match_rng = random.Random(derive_seed("match", seed))
    total_pnl = [0.0, 0.0]
    all_deals: list[DealResult] = []
    all_times_a: list[float] = []
    all_times_b: list[float] = []
    all_warnings_a: list[str] = []
    all_warnings_b: list[str] = []
    violations = [0, 0]
    clamps = [0, 0]

    # Determine inversion from first_maker override
    invert_base = False
    if first_maker is not None:
        # Round 1 default maker is seat 0; if user wants seat 1, invert
        invert_base = (first_maker == 1)

    for d in range(n_deals):
        # Generate or reuse coins
        if fixed_coins is not None:
            coins = list(fixed_coins)
        else:
            coins = make_coins(match_rng, config)

        deal_seed = derive_seed("phase", seed, d)

        # Legs: direct, then mirror (if enabled)
        legs = [(False, invert_base)]
        if mirror:
            legs.append((True, not invert_base))

        for swap, invert in legs:
            deal_label = (
                f"{'Mirror' if swap else 'Direct'} Deal "
                f"{len(all_deals) + 1}/{n_deals * (2 if mirror else 1)}"
            )

            if verbose:
                print(f"\n{'-' * 62}")
                print(f"  {deal_label}  -  Seed: {deal_seed}")
                print(f"{'-' * 62}")

            # A fresh instance for every deal. In-process that only resets
            # the object; module globals and class attributes survive, so
            # statelessness is really enforced by the sandbox re-executing
            # the submission's module between deals.
            result, wrap_a, wrap_b = play_deal(
                build_bot(bot_a_factory),
                build_bot(bot_b_factory),
                coins,
                config,
                deal_seed,
                swap,
                invert,
                verbose,
                bot_a_name,
                bot_b_name,
            )

            total_pnl[0] += result.pnl[0]
            total_pnl[1] += result.pnl[1]
            all_deals.append(result)
            all_times_a.extend(wrap_a.timer.call_times)
            all_times_b.extend(wrap_b.timer.call_times)
            all_warnings_a.extend(wrap_a.timer.warnings)
            all_warnings_b.extend(wrap_b.timer.warnings)
            violations[0] += wrap_a.timer.violations
            violations[1] += wrap_b.timer.violations
            clamps[0] += wrap_a.timer.clamps
            clamps[1] += wrap_b.timer.clamps

            if verbose:
                for line in result.logs:
                    print(line)

        # A bot past the violation limit has already forfeited; playing the
        # remaining deals would only burn wall clock killing its worker over
        # and over, and cannot change the result.
        if any(v > config.MAX_TIME_VIOLATIONS for v in violations):
            break

    # -- Forfeits -------------------------------------------
    # A bot that repeatedly overruns the hard per-call limit forfeits the
    # match. The penalty is a TRANSFER charged to the offending seat, so the
    # match stays zero-sum and a hanging bot can never profit from hanging.
    forfeited = [v > config.MAX_TIME_VIOLATIONS for v in violations]
    net = (int(forfeited[0]) - int(forfeited[1])) * config.FORFEIT_PNL
    if net:
        total_pnl[0] -= net
        total_pnl[1] += net
        loser = bot_a_name if forfeited[0] else bot_b_name
        msg = (
            f"FORFEIT: {loser} exceeded the {config.HARD_TIME_LIMIT_MS:.0f}ms "
            f"per-call limit {max(violations)} times "
            f"(limit {config.MAX_TIME_VIOLATIONS}) -> {config.FORFEIT_PNL:.0f} "
            f"PnL to the opponent"
        )
        (all_warnings_a if forfeited[0] else all_warnings_b).append(msg)

    return MatchResult(
        pnl=(total_pnl[0], total_pnl[1]),
        deals=all_deals,
        bot_a_times=all_times_a,
        bot_b_times=all_times_b,
        bot_a_warnings=all_warnings_a,
        bot_b_warnings=all_warnings_b,
        bot_a_violations=violations[0],
        bot_b_violations=violations[1],
        bot_a_clamps=clamps[0],
        bot_b_clamps=clamps[1],
        forfeits=(forfeited[0], forfeited[1]),
    )
