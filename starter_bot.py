# Name: [Your Name Here]
# College: [Your College Here]
# Roll Number: [Your Roll Number Here]

"""
starter_bot.py — Divided Oracle: Strategy Template
====================================================

Fill in the methods below to create your trading strategy.
Your class MUST be named 'Bot'.

╔══════════════════════════════════════════════════════════════╗
║  RULES                                                      ║
║  ────────────────────────────────────────────────────────── ║
║  • reset() is called with a FRESH INSTANCE before every     ║
║    deal. ALL per-deal state must be set here.                ║
║  • You MAY remember things WITHIN a deal (across 5 rounds). ║
║  • You may NOT remember things BETWEEN deals.               ║
║  • Time limit: 2ms average per call across a match.         ║
║  • Allowed imports: math, random, statistics, collections,  ║
║    heapq, bisect, itertools, functools, typing.             ║
║  • No network access, no filesystem access.                 ║
╚══════════════════════════════════════════════════════════════╝

OBSERVATION (obs) FIELDS:
─────────────────────────
    obs.seat           int          Your seat (0 or 1)
    obs.round          int          Current round (1–5)
    obs.my_revealed    tuple        Your revealed coins so far (+1/−1 each)
    obs.k_mine         int          Sum of your revealed coins (property)
    obs.te_mine        int          Your remaining Tactical Energy
    obs.te_theirs      int          Opponent's remaining TE
    obs.spread_cap     int          Max opening spread this round
    obs.final_cap      int          The round's spread FLOOR: the narrowest
                                    legal OPENING quote, and the width below
                                    which a counter is never *forced* to
                                    shrink. Countering tighter is allowed.
    obs.is_maker       bool         Are you the Maker this round?
    obs.powers_mine    frozenset    Powers you hold this round
    obs.powers_theirs  frozenset    Powers opponent holds this round
    obs.auction_log    tuple        Public tape: dicts with keys
                                    round / seat / power / cost
    obs.contracts      tuple        Contract objects. Fields: round, price,
                                    long_seat, forced, forcer, shift,
                                    maker_seat, open_bid, open_ask
    obs.foresight      tuple        FORESIGHT leak: opponent coins you can see
    obs.n_unknown_both int          Coins neither player has seen
    obs.n_turns        int          Total negotiation turns allowed

CONFIG FIELDS (available via self.config after reset):
──────────────────────────────────────────────────────
    config.N_COINS           40     Total coins
    config.N_PRIVATE         20     Coins per player
    config.N_ROUNDS          5      Rounds per deal
    config.REVEAL_PER_ROUND  4      Coins revealed per round
    config.TE_BUDGET         24     TE per deal
    config.TE_SALVAGE        0.08   PnL per unspent TE
    config.N_TURNS           6      Negotiation turns
    config.MIN_REDUCTION     1      Min spread shrink per counter
    config.FORCED_FILL_FEE   2.0    Forcing fee
    config.MAKER_OBLIGATION  3.0    Maker obligation strength
    config.POWERS            dict   Power definitions (ELIGIBILITY,
                                    not a schedule -- see below)
    config.SLOTS_PER_ROUND   1      Powers drawn per round
    config.WIDTH_PREMIUM     0.22   Cost per tick of width above floor
    config.spread_cap(r)     int    Max opening spread for round r
    config.final_cap(r)      int    Spread floor for round r

THE FIVE POWERS:
────────────────
    FORESIGHT       See 16 of opponent's revealed coins. ~1.22 ticks/win.
    TRICK_ROOM      Shift forced fill 3 ticks your way. ~1.37 ticks/win.
    SUBSTITUTE      Cap your loss at 2 ticks this round. ~1.07 ticks/win.
    STEALTH_ROCK    Persistent: later forced fills shift 2 ticks. ~1.47/win.
                    Once per DEAL. Rounds 1–4 only — it is worthless drawn
                    last, because no rounds remain for it to act on.
    TRANSFORM       Winning it buys the OPTION to swap hands; use_transform()
                    decides. Once per DEAL. Rounds 1–3 only.

HOW THE AUCTION RESOLVES:
─────────────────────────
    • First price: the higher bid wins and PAYS ITS OWN BID.
    • Equal non-zero bids → coin flip, and the winner still pays.
    • Both bid 0 → nobody wins it.
    • Bids totalling more than obs.te_mine are scaled down proportionally.
    • Unspent TE is worth 0.08 PnL per point of advantage, so energy has a
      real opportunity cost: a power is worth buying only if its edge beats
      what the energy is worth banked.

METHODS YOU MUST IMPLEMENT: reset, bid, quote, respond, use_transform.
A submission missing any of them is rejected before the tournament runs.
"""

import random


class Bot:
    """Your trading bot. Implement the methods below."""

    # Give your bot a unique name (shown in logs and leaderboard)
    name = "StarterBot"

    def reset(self, seat: int, config, seed: int) -> None:
        """Called before every deal with a FRESH INSTANCE.

        Set up all per-deal state here. Do NOT use __init__ for game state.

        Args:
            seat:   Your seat number (0 or 1)
            config: GameConfig with all game parameters and helper methods
            seed:   Deterministic random seed for this deal
        """
        self.seat = seat
        self.config = config
        self.rng = random.Random(seed)
        # ──────────────────────────────────────────────────
        # TODO: Add your per-deal state here.
        # Examples:
        #   self.opponent_sum_estimate = 0.0
        #   self.total_spent_te = 0
        #   self.round_history = []
        # ──────────────────────────────────────────────────

    def bid(self, obs, offered: list) -> dict:
        """Submit blind Tactical Energy bids on offered powers.

        Called once per round, BEFORE the negotiation.
        Both players bid simultaneously (blind).

        Args:
            obs:     Current observation (your revealed coins, TE, etc.)
            offered: List of power names available this round
                     e.g. ["FORESIGHT", "TRICK_ROOM", "SUBSTITUTE"]

        Returns:
            Dict mapping power names to bid amounts (non-negative ints).
            Total bids MUST NOT exceed obs.te_mine (otherwise all your bids will be set to 0 for the current round).
            Powers you don't bid on go to the opponent if they bid ≥ 1.
            Equal non-zero bids → coin flip; the winner pays.

        Two things worth thinking about before you write this:

          • You pay your own bid, so bidding your true value captures zero
            surplus by construction. Shade.
          • A power is worth what it does for you MINUS what it does for
            the opponent if they get it. Those are different numbers, and
            for TRANSFORM they have opposite signs.

        Example:
            return {"FORESIGHT": 4, "TRICK_ROOM": 2}
        """
        # TODO: Implement your bidding strategy
        return {}

    def quote(self, obs) -> tuple:
        """Maker: provide opening two-sided quote [bid, ask].

        Called only when obs.is_maker is True.

        TWO decisions, not one. The centre says where you think the score is.
        The WIDTH says how sure you are, and it is yours to choose anywhere in
        [obs.final_cap, obs.spread_cap]:

          • wide  — you are crossed only on a large error, and you keep more
                    turns of range to retreat through. You pay
                    config.WIDTH_PREMIUM a tick for that option, every round.
          • tight — no premium, and the largest obligation payout when you are
                    right, because the payout is λ × (1 − p) and p is the
                    straddle rate your width can achieve. But a tight quote is
                    what lets a Taker who knows more than you act on it.

        The obligation is scored at the width you chose, so quoting honestly
        breaks even at ANY width: nothing here pushes you to one end. Use
        config.straddle_prob(r, width) to price it, and pass `unseen=` to price
        YOUR straddle rate rather than the baseline's when you hold a FORESIGHT
        leak — the obligation pays you the difference between the two.

        Args:
            obs: Current observation

        Returns:
            (bid, ask) where bid ≤ ask and
            obs.final_cap ≤ ask − bid ≤ obs.spread_cap

        Example:
            # Centre on your revealed coin sum, at the tightest legal width
            v = obs.k_mine
            w = obs.final_cap
            return (v - w // 2, v + (w - w // 2))
        """
        # Default: centre on k_mine at maximum width. Both halves of that are
        # choices, and neither is the right answer -- see above.
        v = obs.k_mine
        cap = obs.spread_cap
        return (v - cap // 2, v + (cap - cap // 2))

    def respond(self, obs, quote: tuple, turn: int):
        """Respond to a quote: accept or counter.

        Called on turns 2 through config.N_TURNS (2..6) of the negotiation,
        alternating between Taker and Maker.

        Args:
            obs:   Current observation
            quote: Current (bid, ask) range
            turn:  Turn number (2..config.N_TURNS)

        Returns one of:
            "ACCEPT_BUY"                — buy at ask (you go long)
            "ACCEPT_SELL"               — sell at bid (you go short)
            ("COUNTER", new_bid, new_ask) — tighter range
                • Must be inside current (bid, ask)
                • Must shrink spread by ≥ config.MIN_REDUCTION, but is
                  never forced below obs.final_cap

        ⚠️  If turn == config.N_TURNS (final turn) and you COUNTER:
            → Contract forces at the midpoint
            → YOU pay the forcing fee (config.FORCED_FILL_FEE = 2.0 ticks)
            → YOU become the short (seller)

        Example:
            bid, ask = quote
            v = obs.k_mine   # your estimate of the score
            if v > ask + 1:
                return "ACCEPT_BUY"
            if v < bid - 1:
                return "ACCEPT_SELL"
            w = max(0, (ask - bid) - self.config.MIN_REDUCTION)
            center = max(bid, min(round(v), ask - w))
            return ("COUNTER", center, center + w)
        """
        bid, ask = quote
        v = obs.k_mine

        # Simple logic: accept if your estimate is outside the range
        if v > ask:
            return "ACCEPT_BUY"
        if v < bid:
            return "ACCEPT_SELL"

        # Counter toward your estimate
        w = max(0, (ask - bid) - self.config.MIN_REDUCTION)
        center = max(bid, min(round(v), ask - w))
        return ("COUNTER", center, center + w)

    def use_transform(self, obs) -> bool:
        """You won TRANSFORM. Do you actually want to swap hands?

        Called immediately after the auction, only for the seat that won
        TRANSFORM that round. Return True to swap both private hands
        wholesale (revealed coins included), False to leave the board alone.

        The power is CONSUMED either way. That is the whole point: buying it
        and declining removes it from the deal, so it is how you protect a
        hand you like from an opponent who wants to take it.

        Which way round to think about it: a hand of twenty −1s is a superb
        hand — total certainty, you simply sell it. The worthless hand is
        the BALANCED one, summing to about zero, which tells you nothing the
        prior did not already say. That is the hand worth trading away.

        Returns:
            True to fire the swap, False to decline.
            Anything that is not a bool is coerced; a crash or a timeout
            here is treated as False.
        """
        # TODO: decide for yourself. A reasonable starting point:
        return abs(obs.k_mine) <= 1
