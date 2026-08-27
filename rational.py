# Name: Quantstorm Reference Bot
# College: Quantstorm
# Roll Number: REF-000

"""
rational.py — Baseline: Rational Pricing Bot
==============================================

Strategy:
  • As Maker: centres quote on k_mine (broadcasts its hand honestly).
  • As Taker: reads the Maker's opening quote to infer their revealed sum,
    then estimates S = k_mine + inferred_opponent_sum.
  • Never bids on powers.

Why it's stronger than NaiveEV:
  • Uses the opponent's quote as a signal, worth +31.0 ± 7.0 ticks/match
    against an opponent identical in every other respect.
  • Optimal posterior given an honest opponent: E[S] = k_mine + k_theirs.

Why it's still beatable, in order of how much it costs:
  • Never acquires powers. This is the big one: value-bidding the auction
    is worth +56.1 ± 4.8 ticks/match against an otherwise identical
    non-bidder, so Rational concedes more by ignoring the auction than it
    gains from the whole pricing layer.
  • Opens at the widest legal width every round, and pays WIDTH_PREMIUM for
    it. Width is a measured tie, not a free lunch -- but it is a decision,
    and this bot does not make one.
  • Reads the opponent's quote at face value with no distortion correction.

WITHDRAWN. Earlier versions of this docstring claimed "a Maker that distorts
its quote can exploit this (measured at -240/match)". That number did not
survive replication and RULEBOOK.md §4 withdraws it: with the maker
obligation on, heavy compression measures +0.80 ± 1.51 against honest
quoting and MILD compression measures -3.4 ± 3.2. Distortion is priced
monotonically and does not pay. Do not build an entry around beating it.

This is the honest-play reference point, not a target to converge on.
"""

import random


class Bot:
    name = "Rational"

    def reset(self, seat, config, seed):
        self.seat = seat
        self.config = config
        self.rng = random.Random(seed)
        # Track per-round anchor (first observation of opponent's quote)
        # We latch the first quote midpoint per round and never re-anchor,
        # because later ranges are negotiated objects contaminated by both sides.
        self._anchor = {}

    def _get_anchor(self, obs, quote):
        """Infer opponent's revealed sum from the opening quote.

        Only the OPENING quote is a clean read of the maker's information.
        We latch it once per round and never update it.
        """
        r = obs.round
        if r not in self._anchor:
            if not obs.is_maker and quote is not None:
                # As taker, the quote midpoint ≈ maker's k_mine
                self._anchor[r] = (quote[0] + quote[1]) / 2
            else:
                self._anchor[r] = 0.0
        return self._anchor[r]

    def _value(self, obs, quote=None):
        """Estimate the hidden score S."""
        if obs.is_maker or quote is None:
            # As maker, we know only our own coins + any FORESIGHT leak
            return float(obs.k_mine + sum(obs.foresight))

        # As taker: S ≈ k_mine + opponent's revealed sum
        anchor = self._get_anchor(obs, quote)

        # If we have FORESIGHT, blend it with the anchor
        if obs.foresight:
            leak_sum = sum(obs.foresight)
            anchor = 0.5 * anchor + 0.5 * leak_sum

        return float(anchor + obs.k_mine)

    def bid(self, obs, offered):
        # Never bid — Rational is auction-passive
        return {}

    def quote(self, obs):
        # Centre on k_mine + foresight (honest quoting)
        v = round(obs.k_mine + sum(obs.foresight))
        cap = obs.spread_cap
        lo = v - cap // 2
        return (lo, lo + cap)

    def respond(self, obs, quote, turn):
        bid, ask = quote
        v = self._value(obs, quote)

        # Signed edges: positive means profitable
        edge_buy = v - ask    # profit from buying at ask
        edge_sell = bid - v   # profit from selling at bid

        # Accept if there's positive edge
        if edge_buy > 0 and edge_buy >= edge_sell:
            return "ACCEPT_BUY"
        if edge_sell > 0:
            return "ACCEPT_SELL"

        # Counter toward our value estimate
        w = max(0, (ask - bid) - self.config.MIN_REDUCTION)
        center = max(bid, min(round(v), ask - w))
        return ("COUNTER", center, center + w)

    def use_transform(self, obs):
        """Fire the swap only from a flat hand.

        Winning TRANSFORM buys the OPTION, not the swap. A hand of twenty
        -1s is superb -- total certainty, just sell it. The worthless hand
        is the balanced one, which tells you nothing the prior did not.
        So swap away the flat hand, and decline on a decisive one: the
        power is spent either way, which is what makes declining a defence.
        """
        return abs(obs.k_mine) <= 1
