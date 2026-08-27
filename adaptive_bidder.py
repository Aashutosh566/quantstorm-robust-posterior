# Name: Quantstorm Reference Bot
# College: Quantstorm
# Roll Number: REF-000

"""
adaptive_bidder.py — Strategy 1: State-Conditional Power Bidder
================================================================

Core idea: Value each power based on the CURRENT game state, not
a fixed table. Shade bids at 0.60 of fair value (first-price auction).

Key mechanics:
  • Powers are bid at their calibrated tick value FOR THIS ROUND / te_salvage
  • Shading at 0.60 captures surplus in a first-price auction
  • Honest quoting at the tightest legal width, with full quote reading
  • TRANSFORM is bid from BOTH sides -- see below

Why "state-conditional" finally means something
-----------------------------------------------
This bot's name was aspirational for a while. When every power was worth
about a tick in every round, conditioning on state had nothing to condition
on, and a flat 8-TE constant bid gave up only 10.9 ticks/match to it. On the
current spec the powers have opposed slopes -- FORESIGHT climbs through the
deal, SUBSTITUTE falls -- and using the surface instead of a constant is
worth +27.5 +/- 4.7 ticks/match. See POWER_VALUES below.

What an auction bid is actually worth
-------------------------------------
The quantity that matters in an auction is not "what is this power worth
to me", it is "how much better off am I holding it than watching the
opponent hold it". For four of the five powers those are the same number,
because a power you do not win simply does not fire.

TRANSFORM is not like that. It is a transfer: the swap that gains a flat
hand +1.70 costs the decisive hand the same 1.70. So this bot bids on it
in both states, for opposite reasons:

    flat hand      -> win it and FIRE it      (take a better hand)
    decisive hand  -> win it and DECLINE it   (keep the hand you have)

Declining works as a defence because the power is consumed either way: a
TRANSFORM you bought and did not use cannot then be bought by the opponent.

Denial is NOT free, and the arithmetic has moved
------------------------------------------------
Winning TRANSFORM measures +1.24 to +1.58 ticks depending on the round.
TE_SALVAGE = 0.08 means one tick costs 12.5 TE, so the whole 24-TE budget
is worth 1.92 ticks -- and the swap is worth most of that. On the old
40-TE spec the budget was worth 3.2 ticks against a swap worth ~0.85, and
denial was obviously bad arithmetic. It is no longer obvious. DENIAL_WEIGHT
is still shipped at zero because that is what was last measured, but it was
measured on the old spec and this bot is the wrong place to trust a stale
number. Re-measure it.

This bot denies only when it can SEE that the opponent is flat. The
opponent's opening quote from an earlier round is centred on their own
revealed sum, and TRANSFORM is drawable in rounds 1-3, so from round 2 the
read may be available. That is the whole shape of the decision: contest when
your read says the swap is actually coming, concede when it is not.

Note the slate is DRAWN, not published: TRANSFORM appears in about one deal
in three, and you cannot know in round 1 whether it is coming at all. A
budget plan that assumes it will be there is planning for a deal you are
mostly not in.
"""

import random
from math import sqrt


# Calibrated tick values per power AND PER ROUND (measured, not chosen).
#
# The per-round column is the whole point, and it is new. The powers used to
# be calibrated to roughly one tick each and quoted as a single number, which
# meant the correct bid was the same integer in every state -- so a bot with
# no valuation model at all, bidding a flat 8 TE on everything, gave up only
# 10.9 ticks/match to a "value" bidder. There was nothing to value.
#
# Measured on the current spec (grant the power to one seat, in one round,
# free; both seats otherwise the same bot, so a mirrored match nets exactly
# zero and the residual IS the edge), the powers pull in OPPOSITE directions:
#
#     FORESIGHT   +0.76 -> +2.02   climbs: more of their hand is revealed
#     SUBSTITUTE  +1.46 -> +0.29   falls: less deal left to protect
#
# In round 1 SUBSTITUTE is worth twice FORESIGHT; by round 5 FORESIGHT is
# worth seven times SUBSTITUTE. Using this surface instead of the row means
# is worth +18.3 +/- 4.6 ticks/match, and instead of a flat constant bid,
# +27.5 +/- 4.7. Re-derive it if the spec moves.
POWER_VALUES = {
    "FORESIGHT":    {1: 0.76, 2: 1.16, 3: 1.48, 4: 1.97, 5: 2.02},
    "TRICK_ROOM":   {1: 1.14, 2: 0.00, 3: 0.00, 4: 0.60, 5: 0.52},
    "SUBSTITUTE":   {1: 1.46, 2: 1.15, 3: 0.95, 4: 0.57, 5: 0.29},
    "STEALTH_ROCK": {1: 1.51, 2: 0.75, 3: 0.75, 4: 0.75, 5: 0.00},
    "TRANSFORM":    {1: 1.58, 2: 1.24, 3: 1.31, 4: 0.00, 5: 0.00},
}

#: Fraction of fair value to bid in a first-price auction.
#:
#: 0.60, re-solved by best-response sweep on the current spec. It was 0.30,
#: solved on a spec that no longer ships, and every reference bot but one
#: carried the same 0.30 -- a field unanimous on one wrong constant, which a
#: challenger at 0.45 beat by +79.2 ticks/match.
#:
#: The correct shade DEPENDS ON YOUR VALUE MODEL, which is the part worth
#: internalising. Solved against a field pricing powers off the row means it
#: comes out near 0.45; solved against a field using the per-round surface
#: below -- which values a round-5 FORESIGHT at 2.02 rather than 1.48 -- it
#: moves to 0.55-0.65, because a better estimate is worth bidding closer to.
#: Copying this number without the model it belongs to is how you end up
#: 26 ticks/match behind.
#:
#: The basin is broad rather than a knife edge: across 0.55-0.65 no
#: deviation gains more than noise. Only being badly wrong is punished
#: (0.35 against a 0.45 field runs -26.3).
SHADE = 0.60

#: How flat a revealed sum has to be before the hand is worth trading away.
FLAT_THRESHOLD = 1
#: How flat the OPPONENT has to look, by our read of their earlier quote,
#: before we will pay to keep our hand away from them. The read is noisy,
#: so this is looser than the threshold we apply to ourselves.
OPP_FLAT_THRESHOLD = 2.0
#: What denial is worth when the read says the swap really is coming, as a
#: multiple of the swap's own value.
#:
#: Shipped at ZERO. On the OLD spec that was a measured result: against
#: every opponent tested -- including one that pays 18 TE for the swap and
#: one that snipes it for 1 TE -- paying to deny scored at or below simply
#: conceding.
#:
#:     denial weight   0.0     0.5     1.0     3.0
#:     vs 1-TE sniper -4.5    -6.9   -12.7   -30.8   ticks/match
#:
#: Treat that as a stale measurement, not a settled question. It was taken
#: at TE_BUDGET=40 with a published slate, where the budget was worth 3.2
#: ticks against a swap worth ~0.85. At 24 TE the budget is worth 1.92 and
#: the swap measures 1.24-1.58, so the margin that made denial obviously bad
#: is gone. Nobody has re-run the sweep. This is the most clearly open
#: question in the reference set, and it is left here on purpose.
DENIAL_WEIGHT = 0.0


class Bot:
    name = "AdaptiveBidder"

    def reset(self, seat, config, seed):
        self.seat = seat
        self.config = config
        self.rng = random.Random(seed)
        self._anchor = {}
        self._opp_anchor = {}   # rounds where we read THEIR opening quote

    def _get_anchor(self, obs, quote):
        r = obs.round
        if r not in self._anchor:
            if not obs.is_maker and quote is not None:
                self._anchor[r] = (quote[0] + quote[1]) / 2
                self._opp_anchor[r] = self._anchor[r]
            else:
                self._anchor[r] = 0.0
        return self._anchor[r]

    def _opponent_k(self, obs):
        """Estimate the opponent's revealed sum from their earlier quote.

        An honest Maker centres its opening quote on its own revealed sum,
        so that midpoint IS the estimate. Only rounds where we were the
        Taker carry a reading; our own quotes tell us nothing new.

        Returns None before we have seen the opponent make a quote, and it
        is a genuinely noisy read -- a Maker is under no obligation to
        centre honestly, and a distorting one will feed this exactly the
        number that makes us misplay.
        """
        earlier = [k for k in self._opp_anchor if k < obs.round]
        if not earlier:
            return None
        return self._opp_anchor[max(earlier)]

    def _value(self, obs, quote=None):
        if obs.is_maker or quote is None:
            return float(obs.k_mine + sum(obs.foresight))
        anchor = self._get_anchor(obs, quote)
        if obs.foresight:
            anchor = 0.5 * anchor + 0.5 * sum(obs.foresight)
        return float(anchor + obs.k_mine)

    def _power_value(self, obs, name):
        """What `name` is worth in THIS round, in ticks.

        A power off the surface entirely (a rotated spec, a power added after
        this bot was written) falls back to 0.5 rather than to a confident
        number -- being wrong low costs an auction, being wrong high costs
        the budget for the rest of the deal.
        """
        return POWER_VALUES.get(name, {}).get(obs.round, 0.5)

    def _transform_value(self, obs):
        """What winning TRANSFORM is worth to us right now, in ticks.

        Three cases, and the third is the one worth arguing about:

          flat hand           -> the swap itself, at full value
          decisive + no read  -> nothing. Paying to deny a swap that
                                 probably is not coming loses money; see
                                 the module docstring for the arithmetic.
          decisive + they
          look flat too       -> denial, priced to actually win the auction
                                 rather than to lose it slightly cheaper
        """
        swap = self._power_value(obs, "TRANSFORM")
        if abs(obs.k_mine) <= FLAT_THRESHOLD:
            return swap

        opp_k = self._opponent_k(obs)
        if opp_k is not None and abs(opp_k) <= OPP_FLAT_THRESHOLD:
            return swap * DENIAL_WEIGHT
        return 0.0

    def bid(self, obs, offered):
        if not offered or obs.te_mine <= 0:
            return {}

        out = {}
        for name in offered:
            if name == "TRANSFORM":
                v = self._transform_value(obs)
            else:
                v = self._power_value(obs, name)

            if v <= 0:
                continue

            # Convert ticks to TE: fair_te = ticks / te_salvage
            fair_te = v / self.config.TE_SALVAGE
            bid_amount = max(0, min(int(fair_te * SHADE), obs.te_mine))
            out[name] = bid_amount

        return out

    def quote(self, obs):
        v = round(obs.k_mine + sum(obs.foresight))
        # Width stance: the FLOOR. Tightest legal opening -- pays no
        # width premium and collects the largest obligation payout when
        # it lands, at the cost of being crossable by someone who knows
        # more. Take-it-or-leave-it: there is nothing left to shrink.
        cap = obs.final_cap
        lo = v - cap // 2
        return (lo, lo + cap)

    def respond(self, obs, quote, turn):
        bid, ask = quote
        v = self._value(obs, quote)

        edge_buy = v - ask
        edge_sell = bid - v

        # SUBSTITUTE makes downside cheap — cross on thinner edge
        thresh = 0.0
        if "SUBSTITUTE" in obs.powers_mine:
            thresh -= 1.0

        if edge_buy > thresh and edge_buy >= edge_sell:
            return "ACCEPT_BUY"
        if edge_sell > thresh:
            return "ACCEPT_SELL"

        w = max(0, (ask - bid) - self.config.MIN_REDUCTION)
        center = max(bid, min(round(v), ask - w))
        return ("COUNTER", center, center + w)

    def use_transform(self, obs):
        """Fire from a flat hand, decline from a decisive one.

        The two branches of bid() land here: the flat hand bought a swap and
        takes it; the decisive hand bought a veto and exercises it by doing
        nothing, having spent the power to keep its own coins.
        """
        return abs(obs.k_mine) <= FLAT_THRESHOLD
