# Name: Quantstorm Reference Bot
# College: Quantstorm
# Roll Number: REF-000

"""
naive_ev.py — Baseline: Naive Expected-Value Bot
==================================================

Strategy:
  • Prices the score as E[S | my coins] = sum(my_revealed).
  • Ignores the opponent's opening quote entirely.
  • Never bids on powers.

This is the zero-shot archetype. If you can't beat it, you ARE it.
The tournament gate filters out entries that lose to NaiveEV.

Why it's weak, worst first:
  • Never bids, so the opponent buys every power it wants at its own price.
    Worth +56.1 ± 4.8 ticks/match to the opponent -- nearly twice what
    ignoring the quote costs.
  • Ignores the opponent's opening quote, which is the only clean read of
    their hand in the game. Worth +31.0 ± 7.0 ticks/match.
  • Opens at the maximum width every round and pays WIDTH_PREMIUM for it
    without getting anything for the option.
  • Doesn't adjust for FORESIGHT leaks or any power effects.

Beating it requires both halves. A bot that reads perfectly and never bids
(see rational.py) still loses to one that prices naively and bids sensibly.
"""

import random


class Bot:
    name = "NaiveEV"

    def reset(self, seat, config, seed):
        self.seat = seat
        self.config = config
        self.rng = random.Random(seed)

    def bid(self, obs, offered):
        # Never bid on anything — this is the zero-shot baseline
        return {}

    def quote(self, obs):
        # Centre on E[S | my coins] = k_mine (opponent coins are mean-zero to us)
        v = obs.k_mine + sum(obs.foresight)  # use FORESIGHT if we got it free
        cap = obs.spread_cap
        lo = round(v) - cap // 2
        return (lo, lo + cap)

    def respond(self, obs, quote, turn):
        bid, ask = quote
        v = obs.k_mine + sum(obs.foresight)

        # Accept if our value estimate is outside the range
        if v > ask:
            return "ACCEPT_BUY"
        if v < bid:
            return "ACCEPT_SELL"

        # Counter toward our estimate
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
