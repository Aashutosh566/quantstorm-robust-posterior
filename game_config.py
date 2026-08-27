"""
game_config.py - Divided Oracle: Game Configuration & Data Types
================================================================

Every tunable parameter for the Divided Oracle game lives in this file.
No magic numbers appear anywhere else in the codebase.

Modify the GameConfig class to change game rules. Derived quantities
(spread caps, straddle rates, etc.) are computed automatically.

Data types (Obs, Contract, DealResult) use frozen dataclasses with
immutable fields to prevent bot tampering.

A GameConfig instance is handed to every bot, and the SAME instance is
reused for the whole tournament, so it is frozen after construction: any
attempt to assign to an attribute raises ConfigError. Before this was
enforced, a submission could set `config.TE_SALVAGE = 5.0` inside reset()
and silently rewrite the rules for every pairing played after it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from math import comb, sqrt
from types import MappingProxyType
from typing import Any, NamedTuple


class ConfigError(Exception):
    """Raised on an invalid configuration, or on an attempt to mutate one."""


@lru_cache(maxsize=512)
def _lattice_straddle(m: int, w: int) -> float:
    """P(a width-w quote centred on the posterior contains the score).

    `m` is the number of coins the Maker has not seen, so the residual is
    X = 2B - m for B ~ Binomial(m, 1/2) and has the same parity as m. A quote
    is centred by lo = centre - w // 2, so the window covers offsets
    -(w // 2) .. w - w // 2; X is symmetric about zero, so the opposite
    rounding gives the same probability.
    """
    if m <= 0:
        return 1.0
    total = 0
    for j in range(-(w // 2), w - w // 2 + 1):
        if (j - m) % 2:
            continue  # wrong parity: X can never take this value
        k = (j + m) // 2
        if 0 <= k <= m:
            total += comb(m, k)
    return total / (1 << m)


# ════════════════════════════════════════════════════════════════════
#  GAME CONFIGURATION
# ════════════════════════════════════════════════════════════════════

class GameConfig:
    """All tunable game parameters and derived quantities.

    An instance is passed to every bot via reset(seat, config, seed).
    Bots can read any attribute; the object is frozen after construction,
    so they cannot modify the game rules.

    To create a custom game, pass keyword overrides:
        config = GameConfig(N_COINS=20, N_PRIVATE=10, TE_BUDGET=20)
    """

    _frozen = False

    def __init__(self, **overrides):
        # Remembered so the object can be rebuilt in a worker process --
        # MappingProxyType is not picklable, so the frozen POWERS table
        # cannot travel through multiprocessing as ordinary state.
        self._overrides: dict[str, Any] = dict(overrides)

        # ── Asset ──────────────────────────────────────────
        self.N_COINS: int = 40            # Total coins (must == 2 * N_PRIVATE)
        self.N_PRIVATE: int = 20          # Coins dealt to each player
        self.N_ROUNDS: int = 5            # Rounds per deal
        self.REVEAL_PER_ROUND: int = 4    # Coins revealed per player per round

        # ── Match Structure ────────────────────────────────
        self.DEALS_PER_PHASE: int = 10    # Direct deals; mirror doubles to 20

        # ── Tactical Energy (TE) ───────────────────────────
        #
        # 24 TE against a five-slot slate buys about three powers at the
        # solved shade -- the ratio the design wants, where conceding an
        # auction is a real loss rather than a delay.
        #
        # It was 40 against a ten-slot slate, and that combination is what
        # made the auction the only axis that mattered: a value bidder beat
        # an identical non-bidder by +153.6 ticks/match while the whole
        # pricing layer was worth +12.4.
        self.TE_BUDGET: int = 24          # TE per deal (resets each deal)
        self.TE_SALVAGE: float = 0.08     # PnL per unspent TE point

        # ── Auction ────────────────────────────────────────
        # How equal bids resolve. "coin_flip" is the shipped rule.
        #
        # The old "refund" rule made denial FREE: bids are blind but
        # deterministic bots are predictable, so matching a known bid
        # exactly killed the auction at zero cost, and it was the only
        # defence against TRANSFORM. A coin flip prices contesting -- you
        # win half of them and you pay when you do.
        #
        # Two zero bids are never a tie: nobody wanted the power, and
        # awarding it for 0 TE would hand out a free power every round.
        self.TIE_RULE: str = "coin_flip"   # "coin_flip" | "refund"

        # ── Negotiation ────────────────────────────────────
        #
        # SIX turns, not four, and this is the single highest-leverage
        # balance number in the game.
        #
        # At four turns the mandatory 1-tick reduction converges the spread
        # almost immediately, so there was barely any room to be right or
        # wrong about price: reading the opponent's opening quote was worth
        # +10.6 ticks/match against a bidding opponent, inside the noise of a
        # single match. Six turns gives price discretion somewhere to live
        # AND dilutes the auction at the same time, because a fill-shifting
        # power is worth less when the counterparty has more chances to trade
        # before the forced fill:
        #
        #     N_TURNS   auction axis   reading axis   width axis
        #           4         +136.5         +10.6        +22.1
        #           6          +77.3         +24.6        +31.6
        #
        # It is the only knob measured that moves both axes the right way.
        # TE_SALVAGE and SPREAD_C both look like they should and both
        # BACKFIRE.
        self.N_TURNS: int = 6             # Turns in negotiation (1 open + 5 response)
        self.MIN_REDUCTION: int = 1       # Min spread shrink per counter

        # Do NOT raise this to widen the quote band. A wider band restores
        # the regime where a Maker can sit far from fair and never be
        # crossed: at SPREAD_C=0.75 the value of reading a quote collapses
        # from +10.6 to +4.2 ticks/match.
        self.SPREAD_C: float = 0.5        # Coefficient for spread cap formula

        # ── Fees & Obligations ─────────────────────────────
        self.FORCED_FILL_FEE: float = 2.0     # Forcer pays this to counterparty
        self.MAKER_OBLIGATION: float = 3.0    # Maker obligation transfer strength

        # Ticks the Maker pays the Taker per tick of opening spread above the
        # round's floor (final_cap). The Maker chooses its own width, and this
        # is the price of the option a wide one buys: it is crossed only on a
        # large error and it leaves room to retreat through.
        #
        # It exists because width was a non-decision. The obligation used to be
        # scored against the straddle rate achievable at the CAP, so quoting
        # narrower than the cap missed more often against an unchanged bar and
        # lost money for it -- width was dominated at the maximum and the only
        # thing left to choose in a quote was its centre. The obligation is now
        # scored at the width actually quoted (an honest Maker breaks even at
        # EVERY width) and this premium prices the width itself.
        #
        # RE-SOLVED ON THIS ENGINE. The previous value of 0.35 was solved in
        # the research harness (src/quantstorm) and copied here without being
        # re-derived, and the two engines do not agree. Measured on sim_code
        # at the old spec, opening at the floor minus opening at the cap ran
        # -31.3 ticks/match at 0.00, +21.2 at 0.35 and +58.7 at 0.60 -- so at
        # the shipped 0.35 the "genuine choice" was a solved constant and the
        # tight quote simply won.
        #
        # Re-swept on the current spec (six turns, drawn five-slot slate,
        # 24 TE).
        #
        # Coupled to the slate AND to N_TURNS. Re-solve all three together,
        # never one alone.
        self.WIDTH_PREMIUM: float = 0.22

        # ── Midpoint Rule ──────────────────────────────────
        # "last_quoter_sells" — last bot to COUNTER is short (seller)
        # "maker_sells"       — maker is always short
        # "coin_flip"         — random
        self.MIDPOINT_SIDE_RULE: str = "last_quoter_sells"

        # ── Powers ─────────────────────────────────────────
        # `rounds` is an ELIGIBILITY set, not a schedule. Each round the
        # engine DRAWS SLOTS_PER_ROUND powers from the ones eligible in that
        # round and not already consumed, and that draw is what goes on the
        # block. Both seats see the same draw, and the mirror leg replays it.
        #
        # Why it is drawn rather than published. With a fixed public table
        # the correct bid is the same integer in every deal, so the auction
        # reduced to recalling one constant: a bot with NO power valuation at
        # all -- "bid 8 TE on everything, every round" -- captured 93% of the
        # auction axis, and gating TRANSFORM on a flat hand, which the
        # rulebook calls the purest test in the game, measured -0.25 +/- 4.87
        # against not gating it. Drawing the slate means you cannot know
        # whether the power in front of you is the last one you will be
        # offered, so what a slot is worth depends on the state you are in.
        # That is the difference between an auction with depth and a lookup.
        #
        # Eligibility is bounded by what each power can still act on:
        # STEALTH_ROCK is persistent, so it is worthless drawn last and is
        # not eligible in round 5; TRANSFORM needs deal left to exploit a
        # swapped hand, so it stops after round 3.
        #
        # TE_SALVAGE is NOT the loudness knob, despite looking like it: a
        # power worth v ticks is priced at v / TE_SALVAGE energy, so the cost
        # of contesting one, in the ticks that get scored, is shade * v
        # regardless of the salvage rate. Measured on this engine, raising it
        # from 0.08 to 0.35 made the auction axis BIGGER (136.5 -> 146.9).
        # The knobs that work are the slot count, the budget, and N_TURNS.
        self.POWERS: dict[str, dict[str, Any]] = {
            "FORESIGHT": {
                "magnitude": 16,            # See 16 of opponent's revealed coins
                "rounds": (1, 2, 3, 4, 5),
                "once_per_deal": False,
            },
            "TRICK_ROOM": {
                "magnitude": 3,             # Shift forced fill 3 ticks your way
                "rounds": (1, 2, 3, 4, 5),
                "once_per_deal": False,
            },
            "SUBSTITUTE": {
                "magnitude": 2,             # Cap your loss at 2 ticks this round
                "rounds": (1, 2, 3, 4, 5),
                "once_per_deal": False,
            },
            "STEALTH_ROCK": {
                "magnitude": 2,             # Persistent fill shift 2 ticks (later rounds)
                "rounds": (1, 2, 3, 4),     # worthless drawn last: no rounds left
                "once_per_deal": True,
            },
            "TRANSFORM": {
                "magnitude": 0,             # Swap hands (value is state-dependent)
                "rounds": (1, 2, 3),        # needs deal left to use the new hand
                "once_per_deal": True,
            },
        }

        # How many powers actually go on the block each round, drawn from the
        # eligible set above. ONE, so a deal has five power-slots against a
        # budget that buys about three of them, and every auction is a
        # head-to-head contest for a prize you cannot decline and expect to
        # see again.
        #
        # It was 10 slots against 40 TE. That is what made the auction the
        # only axis that mattered.
        self.SLOTS_PER_ROUND: int = 1

        # "draw"  -- sample SLOTS_PER_ROUND from the eligible set each round
        # "fixed" -- offer the whole eligible set, i.e. the pre-rebalance
        #            behaviour, kept so the old spec stays reproducible
        self.SLATE_MODE: str = "draw"

        # Hard ceiling on how many powers may be on the block in one round.
        # In "fixed" mode this bounds the eligibility table itself; in "draw"
        # mode the draw already bounds it, and eligibility may be wider.
        self.POWERS_PER_ROUND: int = 2

        # ── Time Limits ────────────────────────────────────
        # Both are ENFORCED, not merely logged. A call that exceeds
        # HARD_TIME_LIMIT_MS is abandoned mid-flight, the worker process is
        # killed and restarted, and the action takes its documented fallback.
        self.TIME_BUDGET_MS: float = 2.0       # Average ms per call across a match
        self.HARD_TIME_LIMIT_MS: float = 50.0  # Absolute max ms per single call

        # ── Enforcement ────────────────────────────────────
        # A bot that blows the hard limit this many times in one match
        # forfeits the match. Each violation costs the offender the whole
        # remainder of that deal -- its worker is killed mid-call and has no
        # state to resume from -- so this is deliberately small. One or two
        # are survivable noise (a GC pause, a cold cache); six is a bot that
        # does not fit in the budget.
        self.MAX_TIME_VIOLATIONS: int = 5
        # PnL transferred from a forfeiting bot to its opponent, per match.
        # Charged to the offender specifically -- never to whoever happened
        # to be listed first -- and applied as a transfer, so the match stays
        # zero-sum.
        self.FORFEIT_PNL: float = 250.0
        # Memory cap for a bot worker, enforced by the OS: RLIMIT_AS on
        # POSIX, a Job Object ProcessMemoryLimit on Windows. See limits.py.
        self.BOT_MEMORY_LIMIT_MB: int = 512
        # Total CPU seconds one bot worker may burn across a whole match,
        # enforced as RLIMIT_CPU / PerProcessUserTimeLimit. It is a backstop
        # under the per-call limit, not a wall-clock deadline: a worker
        # waiting on the pipe or descheduled by the OS accrues none of it,
        # so this cannot punish a bot for the harness being busy.
        #
        # It used to be documented as a wall-clock match ceiling and wired
        # to nothing whatsoever.
        self.MATCH_TIMEOUT_S: float = 600.0

        # ── Internal ───────────────────────────────────────
        self.ZERO_SUM_TOL: float = 1e-9
        self.MAX_REASONABLE_VALUE: int = 10_000  # Clamp bot outputs beyond this

        # Apply any overrides
        for key, value in overrides.items():
            if not hasattr(self, key):
                raise ConfigError(f"Unknown config parameter: {key!r}")
            object.__setattr__(self, key, value)

        # Validate after overrides
        self._validate()

        # Deep-freeze the power table, then the object itself. Bots hold a
        # reference to this exact instance for the whole tournament.
        object.__setattr__(self, "POWERS", MappingProxyType({
            name: MappingProxyType({
                k: (tuple(v) if k == "rounds" else v)
                for k, v in spec.items()
            })
            for name, spec in self.POWERS.items()
        }))
        object.__setattr__(self, "_frozen", True)

    # ── Immutability ───────────────────────────────────────

    def __setattr__(self, key, value):
        if self._frozen:
            raise ConfigError(
                f"GameConfig is read-only: cannot set {key!r}. "
                f"Build a new GameConfig(**overrides) instead."
            )
        object.__setattr__(self, key, value)

    def __delattr__(self, key):
        raise ConfigError(f"GameConfig is read-only: cannot delete {key!r}.")

    def __reduce__(self):
        """Rebuild from overrides rather than from __dict__ when pickled.

        The default path would try to restore __dict__ onto a frozen object,
        and MappingProxyType cannot be pickled at all.
        """
        return (_rebuild_config, (self._overrides,))

    # ── Validation ─────────────────────────────────────────

    def _validate(self):
        """Check that all parameters describe a playable game."""
        if self.TIE_RULE not in ("coin_flip", "refund"):
            raise ConfigError(f"Unknown TIE_RULE: {self.TIE_RULE!r}")
        if self.MAX_TIME_VIOLATIONS < 1:
            raise ConfigError("MAX_TIME_VIOLATIONS must be >= 1")
        if self.FORFEIT_PNL < 0:
            raise ConfigError("FORFEIT_PNL must be non-negative")
        if self.HARD_TIME_LIMIT_MS < self.TIME_BUDGET_MS:
            raise ConfigError("HARD_TIME_LIMIT_MS must be >= TIME_BUDGET_MS")
        if self.N_COINS != 2 * self.N_PRIVATE:
            raise ValueError(
                f"N_COINS ({self.N_COINS}) must == 2 * N_PRIVATE ({self.N_PRIVATE})"
            )
        if self.REVEAL_PER_ROUND * self.N_ROUNDS != self.N_PRIVATE:
            raise ValueError(
                f"REVEAL_PER_ROUND * N_ROUNDS ({self.REVEAL_PER_ROUND * self.N_ROUNDS}) "
                f"must == N_PRIVATE ({self.N_PRIVATE})"
            )
        if self.N_TURNS < 2:
            raise ValueError("N_TURNS must be >= 2 (at least one response)")
        if self.MIN_REDUCTION < 1:
            raise ValueError("MIN_REDUCTION must be >= 1")
        if self.TE_BUDGET < 0:
            raise ValueError("TE_BUDGET must be non-negative")
        if self.TE_SALVAGE < 0:
            raise ValueError("TE_SALVAGE must be non-negative")
        if self.DEALS_PER_PHASE < 1:
            raise ValueError("DEALS_PER_PHASE must be >= 1")
        if self.MAKER_OBLIGATION < 0:
            raise ValueError("MAKER_OBLIGATION must be non-negative")
        if self.WIDTH_PREMIUM < 0:
            raise ValueError("WIDTH_PREMIUM must be non-negative")
        if self.FORCED_FILL_FEE < 0:
            raise ValueError("FORCED_FILL_FEE must be non-negative")
        if self.MIDPOINT_SIDE_RULE not in ("last_quoter_sells", "maker_sells", "coin_flip"):
            raise ValueError(f"Unknown MIDPOINT_SIDE_RULE: {self.MIDPOINT_SIDE_RULE!r}")
        if self.POWERS_PER_ROUND < 1:
            raise ValueError("POWERS_PER_ROUND must be >= 1")
        if self.SLATE_MODE not in ("draw", "fixed"):
            raise ConfigError(f"Unknown SLATE_MODE: {self.SLATE_MODE!r}")
        if not (1 <= self.SLOTS_PER_ROUND <= self.POWERS_PER_ROUND):
            raise ValueError(
                f"SLOTS_PER_ROUND ({self.SLOTS_PER_ROUND}) must be between 1 "
                f"and POWERS_PER_ROUND ({self.POWERS_PER_ROUND})"
            )
        # Validate the power specs FIRST. A malformed spec has to report what
        # is actually wrong with it -- if the slate check runs first, a table
        # with a typo'd key fails with "nothing to draw from" in some
        # unrelated round, which is true but useless.
        for name, spec in self.POWERS.items():
            if not name.isupper() or not name.replace("_", "").isalpha():
                raise ValueError(f"Power name must be UPPER_SNAKE: {name!r}")
            if "magnitude" not in spec or "rounds" not in spec:
                raise ValueError(f"Power {name!r} missing 'magnitude' or 'rounds'")
            if "once_per_match" in spec:
                raise ConfigError(
                    f"Power {name!r}: 'once_per_match' was renamed to "
                    f"'once_per_deal' -- the limit resets every deal, not "
                    f"every match."
                )
            for r in spec["rounds"]:
                if not (1 <= r <= self.N_ROUNDS):
                    raise ValueError(f"Power {name!r}: round {r} outside 1..{self.N_ROUNDS}")

        for r in range(1, self.N_ROUNDS + 1):
            eligible = [n for n, s in self.POWERS.items() if r in s["rounds"]]
            if self.SLATE_MODE == "fixed" and len(eligible) > self.POWERS_PER_ROUND:
                raise ValueError(
                    f"Round {r} offers {len(eligible)} powers ({eligible}), above "
                    f"POWERS_PER_ROUND={self.POWERS_PER_ROUND}. A crowded slate is "
                    f"what made conceding an auction free."
                )
        # A round whose pool is smaller than SLOTS_PER_ROUND -- including an
        # empty one -- is legal, and draw_slate offers whatever is there. It
        # is not an error: an ablation that grants exactly one power in
        # exactly one round is how every power in the table was calibrated,
        # and rejecting it would make the spec unmeasurable.

    # ── Derived Quantities ─────────────────────────────────

    def final_cap(self, r: int) -> int:
        """Max spread at the final turn of negotiation for round r.

        The width schedule runs BACKWARDS: round r is sized on the sigma of
        round N_ROUNDS + 1 - r, so the band is widest in round 1 and tightest
        in round 5.

            final_cap(r) = max(1, round(4 * SPREAD_C * sqrt(N_ROUNDS + 1 - r)))

        The band is loosest when nobody knows much and clamps down as the
        reveals accumulate, so the rounds in which a quote carries the most
        information are also the rounds in which it has the least room to
        hide. Early rounds are cheap to be wrong in and the wide band prices
        that; by round 5 both hands are nearly determined and the quote must
        be sharp.

        Wide spreads do not make the coins stop mattering; they convert the
        game into forced midpoint fills where the quote barely matters.
        Reading the opponent's quote is worth MORE when the spread is tight,
        because a narrow quote cannot hide the maker's hand.
        """
        self._check_round(r)
        rr = self.N_ROUNDS + 1 - r
        return max(1, round(4 * self.SPREAD_C * sqrt(rr)))

    def spread_cap(self, r: int) -> int:
        """Max opening spread for round r.

        Set so mandatory reduction lands exactly on final_cap by the last turn:
            spread_cap = final_cap + (N_TURNS - 1) * MIN_REDUCTION
        """
        return self.final_cap(r) + (self.N_TURNS - 1) * self.MIN_REDUCTION

    def unknown_to_both(self, r: int) -> int:
        """Coins neither player has seen after round r's reveal."""
        self._check_round(r)
        return self.N_COINS - 2 * self.REVEAL_PER_ROUND * r

    def residual_sd(self, r: int) -> float:
        """Std dev of the score a Maker cannot see at round r."""
        self._check_round(r)
        return sqrt(self.N_COINS - self.REVEAL_PER_ROUND * r)

    def open_width_floor(self, r: int) -> int:
        """Narrowest legal opening quote in round r.

        The floor is the round's final_cap -- the width the negotiation
        converges to anyway -- so a Maker may open anywhere between "already
        converged" and the cap, and nowhere tighter.

        The floor is load-bearing, not decoration. The obligation pays
        lam * (1 - p) on a straddle and charges lam * p on a miss, and p goes
        to zero with the width: an unfloored zero-width quote would collect
        nearly the whole obligation whenever it happened to land and pay
        nearly nothing when it did not.
        """
        return self.final_cap(r)

    def straddle_prob(self, r: int, width: int = None, unseen: int = None) -> float:
        """Probability an honest Maker's width-tick quote contains the score.

        Exact, not the normal approximation, and the difference is an exploit
        rather than a rounding detail. The residual S - k_mine is a sum of
        N_COINS - REVEAL_PER_ROUND * r fair coins, so it is always EVEN: an
        integer window of odd length holds one more even value at some
        alignments than others, and a width-5 window holds exactly as many as a
        width-4 one. Against a smooth erf bar those lattice steps are mispriced
        by up to seven points of straddle rate -- which, once the Maker picks
        its own width, is a parity arbitrage worth about a tick a deal for
        nothing but preferring odd widths.

        `width` defaults to the round's opening cap. `unseen` overrides how
        many coins the Maker cannot see, which is how a Maker holding a
        FORESIGHT leak prices its OWN straddle rate rather than the baseline's.
        The obligation pays the difference between the two.
        """
        self._check_round(r)
        w = self.spread_cap(r) if width is None else int(width)
        m = self.N_COINS - self.REVEAL_PER_ROUND * r if unseen is None else int(unseen)
        return _lattice_straddle(max(0, m), max(0, w))

    def baseline_straddle(self, r: int) -> float:
        """Straddle rate of an honest Maker quoting the full opening cap.

        The round's headline number. The obligation itself scores against
        straddle_prob() at the width actually quoted, so a Maker is never
        charged for choosing a different one.
        """
        return self.straddle_prob(r, self.spread_cap(r))

    def offered_powers(self, r: int) -> list[str]:
        """Powers ELIGIBLE for auction in round r (before one-shot consumption).

        In "fixed" mode this is the slate. In "draw" mode it is the pool the
        slate is drawn from, and is wider than what actually goes on the
        block -- use draw_slate() for that.
        """
        self._check_round(r)
        return [
            name for name, spec in self.POWERS.items()
            if r in spec["rounds"]
        ]

    def draw_slate(self, rng) -> dict[int, tuple[str, ...]]:
        """The whole deal's slate, drawn up front from `rng`.

        Returns {round: powers on the block}. One-shot powers already
        consumed are filtered by the engine afterwards, not here -- because
        consumption depends on how the deal is PLAYED, and the draw must not.

        Drawing every round at deal start, from a stream keyed only on the
        deal seed, is what keeps the mirror honest. Draw lazily per round
        instead and the stream desynchronises the moment the two legs
        consume a one-shot in different rounds, so the mirror stops replaying
        the same luck and two instances of the same bot no longer net to
        zero -- the fairness property the mirror exists to provide.
        """
        if self.SLATE_MODE == "fixed":
            return {
                r: tuple(self.offered_powers(r))
                for r in range(1, self.N_ROUNDS + 1)
            }

        # A once-per-deal power is placed at most once in the drawn slate.
        # Without this a deal can draw TRANSFORM in two rounds, and if the
        # first one sells, the second round has nothing on the block at all
        # -- a dead auction nobody chose.
        placed_once: set[str] = set()
        out: dict[int, tuple[str, ...]] = {}
        for r in range(1, self.N_ROUNDS + 1):
            pool = [n for n in self.offered_powers(r) if n not in placed_once]
            drawn = rng.sample(pool, min(self.SLOTS_PER_ROUND, len(pool)))
            placed_once |= {n for n in drawn if self.POWERS[n]["once_per_deal"]}
            out[r] = tuple(drawn)
        return out

    def sigma_score(self) -> float:
        """Std dev of the hidden score S = sum of all N_COINS coins."""
        return sqrt(self.N_COINS)

    def _check_round(self, r: int):
        if not (1 <= r <= self.N_ROUNDS):
            raise ValueError(f"Round {r} outside valid range 1..{self.N_ROUNDS}")

    def __repr__(self) -> str:
        return (
            f"GameConfig(N_COINS={self.N_COINS}, N_ROUNDS={self.N_ROUNDS}, "
            f"TE_BUDGET={self.TE_BUDGET}, DEALS_PER_PHASE={self.DEALS_PER_PHASE})"
        )


# ════════════════════════════════════════════════════════════════════
#  DEFAULT CONFIGURATION
# ════════════════════════════════════════════════════════════════════

def _rebuild_config(overrides: dict) -> GameConfig:
    """Unpickle hook for GameConfig -- see GameConfig.__reduce__."""
    return GameConfig(**overrides)


DEFAULT_CONFIG = GameConfig()


# ════════════════════════════════════════════════════════════════════
#  DATA TYPES  (frozen = immutable = cheat-proof)
# ════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class Obs:
    """Everything a bot may legally condition on.

    All fields are immutable. Bots cannot modify this object.
    Fields are populated fresh for every bot call by the engine.

    Key properties:
        obs.k_mine   — sum of your revealed coins (derived)
    """
    seat: int                               # 0 or 1
    round: int                              # 1..N_ROUNDS
    my_revealed: tuple[int, ...]            # Your revealed coins so far (+1/-1 each)
    te_mine: int                            # Your remaining Tactical Energy
    te_theirs: int                          # Opponent's remaining TE
    spread_cap: int                         # Max opening spread this round
    final_cap: int                          # Max final spread this round
    is_maker: bool                          # Are you the Maker this round?
    powers_mine: frozenset                  # Powers you hold this round
    powers_theirs: frozenset                # Powers opponent holds this round
    auction_log: tuple                      # Public tape: dicts of round/seat/power/cost
    contracts: tuple                        # Past contracts this deal (Contract objects)
    foresight: tuple                        # FORESIGHT leak: opponent coins you can see
    n_unknown_both: int                     # Coins neither player has seen
    n_turns: int                            # Total negotiation turns allowed

    @property
    def k_mine(self) -> int:
        """Sum of your revealed coins."""
        return sum(self.my_revealed)


class Contract(NamedTuple):
    """One round's trading outcome, as handed to bots in ``obs.contracts``.

    A NamedTuple rather than a frozen dataclass so that the first four
    fields stay reachable positionally. ``obs.contracts`` used to be
    flattened to bare ``(round, price, long_seat, forced)`` 4-tuples on the
    way into an Obs, which silently withheld the five remaining fields the
    rulebook documents -- ``open_bid`` and ``open_ask`` among them, which
    §9 calls the only clean read of a Maker's information. Passing the
    Contract itself restores them without breaking a bot that indexed the
    old shape, because ``c[0]``..``c[3]`` are unchanged.
    """
    round: int              # Which round (1-5)
    price: int              # Execution price
    long_seat: int          # Seat that is long (buyer)
    forced: bool            # True if no acceptance → midpoint fill
    forcer: int = -1        # Seat that countered on final turn (-1 if not forced)
    shift: int = 0          # Power-induced price shift (forced fills only)
    maker_seat: int = -1    # Who was maker this round
    open_bid: int = 0       # Maker's opening bid
    open_ask: int = 0       # Maker's opening ask


@dataclass
class DealResult:
    """Outcome of one deal."""
    pnl: tuple                          # (bot_a_pnl, bot_b_pnl)
    score: int                          # True score S = sum(all coins)
    contracts: tuple                    # Tuple of Contract objects
    te_left: tuple                      # (TE remaining for A, for B)
    logs: list = field(default_factory=list)   # Step-by-step text log


@dataclass
class MatchResult:
    """Outcome of a full match (multiple deals + optional mirrors)."""
    pnl: tuple                          # Aggregate (bot_a_pnl, bot_b_pnl)
    deals: list = field(default_factory=list)  # List of DealResult
    bot_a_times: list = field(default_factory=list)  # Per-call times (ms) for bot A
    bot_b_times: list = field(default_factory=list)  # Per-call times (ms) for bot B
    bot_a_warnings: list = field(default_factory=list)
    bot_b_warnings: list = field(default_factory=list)
    bot_a_violations: int = 0           # Calls over the hard per-call limit
    bot_b_violations: int = 0
    bot_a_clamps: int = 0               # Legal-but-clamped outputs (budget scaling)
    bot_b_clamps: int = 0
    forfeits: tuple = (False, False)    # Did (A, B) forfeit this match?
