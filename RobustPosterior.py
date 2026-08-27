# Name: Aashutosh Kumar
# College: Netaji Subhas University of Technology (NSUT)
# Roll Number: 2025UME7607
# Email: 102ashutoshkumar@gmail.com

import math
from typing import Dict, Tuple

_BASE_VALUE = {
    "FORESIGHT": (0.76, 1.16, 1.48, 1.97, 2.02),
    "TRICK_ROOM": (1.14, 0.00, 0.00, 0.60, 0.52),
    "SUBSTITUTE": (1.46, 1.15, 0.95, 0.57, 0.29),
    "STEALTH_ROCK": (1.51, 0.75, 0.75, 0.75, 0.00),
    "TRANSFORM": (1.58, 1.24, 1.31, 0.00, 0.00),
}

class Bot:
    name = "QuantStorm_Aashutosh"

    def reset(self, seat, config, seed):
        self.seat = seat
        self.config = config
        self.open_anchor = {}

    # Score belief 

    def _record_opening_quote(self, obs, quote, turn):
        # At turn 2 the quote is the Maker's opening.  Later quotes are
        # negotiated objects, so treating them as hand information double
        # counts our own previous action and is easily exploitable.
        if turn == 2 and not obs.is_maker and obs.round not in self.open_anchor:
            bid, ask = quote
            self.open_anchor[obs.round] = ((bid + ask) / 2.0, ask - bid)

    def _latest_anchor(self, obs):
        rounds = [r for r in self.open_anchor if r <= obs.round]
        if not rounds:
            return None
        r = max(rounds)
        mid, width = self.open_anchor[r]
        return r, mid, width

    def _belief(self, obs):
        """Conservative posterior mean and standard deviation of final S."""
        direct = sum(obs.foresight)
        base = obs.k_mine + direct
        unseen = self.config.N_COINS - len(obs.my_revealed) - len(obs.foresight)
        anchor = self._latest_anchor(obs)
        if anchor is None or len(obs.foresight) >= 4 * obs.round:
            return float(base), math.sqrt(max(0, unseen))

        anchor_round, anchor_mid, width = anchor
        # The opening midpoint estimates the opponent's revealed sum.  Some of that information overlaps the FORESIGHT sample;
        #  remove its expected overlap before blending.  A wide quote is trusted less.
        
        overlap = min(anchor_round, obs.round) / float(obs.round)
        residual_anchor = anchor_mid - overlap * direct
        reliability = max(0.50, min(0.78, 0.78 - 0.045 * (width - obs.final_cap)))
        mean = base + reliability * residual_anchor
        sd = math.sqrt(max(0, unseen)) * (1.0 - 0.18 * reliability)
        return float(mean), float(sd)

    def _position_ev(self, obs, price, long):
        """Exact lattice EV for a contract, with our SUBSTITUTE protection."""
        direct = sum(obs.foresight)
        base = obs.k_mine + direct
        unseen = self.config.N_COINS - len(obs.my_revealed) - len(obs.foresight)
        mean, _ = self._belief(obs)
        shift = mean - base
        denom = 1 << unseen
        total = 0.0
        for plus in range(unseen + 1):
            score = base + 2 * plus - unseen + shift
            pnl = score - price if long else price - score
            if "SUBSTITUTE" in obs.powers_mine:
                pnl = max(pnl, -2.0)
            total += math.comb(unseen, plus) * pnl
        return total / denom

    def _forced_shift(self, obs):
        mine = (3 if "TRICK_ROOM" in obs.powers_mine else 0)
        mine += 2 if "STEALTH_ROCK" in obs.powers_mine else 0
        theirs = (3 if "TRICK_ROOM" in obs.powers_theirs else 0)
        theirs += 2 if "STEALTH_ROCK" in obs.powers_theirs else 0
        return mine - theirs

    # Auction

    def _forced_rate(self, obs):
        forced = sum(1 for contract in obs.contracts if contract.forced)
        return (1.0 + forced) / (5.0 + len(obs.contracts))

    def _power_value(self, obs, power):
        round_index = obs.round - 1
        base = _BASE_VALUE.get(power, (0.0,) * 5)[round_index]
        if base <= 0:
            return 0.0

        if power == "FORESIGHT":
            if obs.is_maker:
                leak = min(16, self.config.REVEAL_PER_ROUND * obs.round)
                default_unseen = self.config.N_COINS - self.config.REVEAL_PER_ROUND * obs.round
                effective = max(0, default_unseen - leak)
                maker_edge = 0.0
                for width in range(obs.final_cap, obs.spread_cap + 1):
                    edge = self.config.MAKER_OBLIGATION * (
                        self.config.straddle_prob(obs.round, width, unseen=effective)
                        - self.config.straddle_prob(obs.round, width)
                    ) - self.config.WIDTH_PREMIUM * (width - obs.final_cap)
                    maker_edge = max(maker_edge, edge)
                return max(base, base + maker_edge)
            return base

        if power in ("TRICK_ROOM", "STEALTH_ROCK"):
            # The calibration is the baseline; current forced-fill history
            # only makes a bounded adjustment around it.
            return base * (0.70 + 1.50 * self._forced_rate(obs))

        if power == "SUBSTITUTE":
            _, sd = self._belief(obs)
            return base * min(1.25, max(0.75, sd / 4.5))

        if power == "TRANSFORM":
            anchor = self._latest_anchor(obs)
            opponent = anchor[1] if anchor is not None else 0.0
            own = obs.k_mine
            # Fire from a flat hand; allow only a small denial value when an
            # earlier opening quote says the opponent is especially flat.
            if abs(own) <= 1:
                return base * (1.0 + min(0.20, abs(opponent) * 0.05))
            if anchor is not None and abs(opponent) <= 1.5:
                return 0.18 * base
            return 0.0

        return base

    def _opponent_anchor(self, obs):
        observed = [entry["cost"] for entry in obs.auction_log if entry["seat"] != self.seat]
        rounds_left = self.config.N_ROUNDS - obs.round + 1
        neutral = obs.te_theirs / max(2, rounds_left)
        if not observed:
            return neutral
        return 0.55 * neutral + 0.45 * (sum(observed) / len(observed))

    def bid(self, obs, offered) -> Dict[str, int]:
        if not offered or obs.te_mine <= 0:
            return {}
        power = offered[0]
        value = self._power_value(obs, power)
        if value <= 0:
            return {}

        rounds_left = self.config.N_ROUNDS - obs.round + 1
        reserve = 2 if rounds_left >= 4 else 0
        affordable = max(0, obs.te_mine - reserve)
        fair_te = value / self.config.TE_SALVAGE
        shaded = int(math.floor(0.60 * fair_te))
        contest = int(math.floor(self._opponent_anchor(obs))) + 1
        # Beat the observed opponent tendency only if that price remains well
        # inside our value; otherwise the first-price auction is a concession.
        if contest <= 0.72 * fair_te:
            shaded = max(shaded, contest)
        amount = min(shaded, affordable, obs.te_theirs + 1)
        return {power: int(amount)} if amount > 0 else {}

    #  Negotiation 

    def _maker_width(self, obs):
        if not obs.foresight:
            return obs.final_cap
        default_unseen = self.config.N_COINS - self.config.REVEAL_PER_ROUND * obs.round
        effective = max(0, default_unseen - len(obs.foresight))
        best_width, best_edge = obs.final_cap, -1e9
        for width in range(obs.final_cap, obs.spread_cap + 1):
            edge = self.config.MAKER_OBLIGATION * (
                self.config.straddle_prob(obs.round, width, unseen=effective)
                - self.config.straddle_prob(obs.round, width)
            ) - self.config.WIDTH_PREMIUM * (width - obs.final_cap)
            if edge > best_edge:
                best_width, best_edge = width, edge
        return best_width

    def quote(self, obs) -> Tuple[int, int]:
        mean, _ = self._belief(obs)
        width = self._maker_width(obs)
        bid = int(round(mean)) - width // 2
        return int(bid), int(bid + width)

    def _final_action(self, obs, bid, ask):
        buy = self._position_ev(obs, ask, True)
        sell = self._position_ev(obs, bid, False)
        forced_price = (bid + ask) // 2 + self._forced_shift(obs)
        force = self._position_ev(obs, forced_price, False) - self.config.FORCED_FILL_FEE
        if buy >= sell and buy >= force:
            return "ACCEPT_BUY"
        if sell >= force:
            return "ACCEPT_SELL"
        width = max(obs.final_cap, ask - bid - self.config.MIN_REDUCTION)
        mean, _ = self._belief(obs)
        new_bid = max(bid, min(int(round(mean)) - width // 2, ask - width))
        return ("COUNTER", int(new_bid), int(new_bid + width))

    def respond(self, obs, quote, turn):
        bid, ask = quote
        self._record_opening_quote(obs, quote, turn)
        if turn == obs.n_turns:
            return self._final_action(obs, bid, ask)

        buy = self._position_ev(obs, ask, True)
        sell = self._position_ev(obs, bid, False)
        mean, sd = self._belief(obs)
        threshold = 0.12 + 0.04 * sd
        if buy > threshold and buy >= sell:
            return "ACCEPT_BUY"
        if sell > threshold:
            return "ACCEPT_SELL"

        width = min(ask - bid, max(obs.final_cap, ask - bid - self.config.MIN_REDUCTION))
        new_bid = max(bid, min(int(round(mean)) - width // 2, ask - width))
        return ("COUNTER", int(new_bid), int(new_bid + width))

    def use_transform(self, obs):
        anchor = self._latest_anchor(obs)
        opponent = anchor[1] if anchor is not None else 0.0
        # The auction cost is sunk.  Exercise only if the posterior says the
        # opposing hand is meaningfully more decisive than ours.
        return abs(opponent) > abs(obs.k_mine) + 1.25
