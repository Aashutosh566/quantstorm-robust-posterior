# QuantStorm_RobustPosterior — Mathematical Foundations

## Author
**Aashutosh Kumar**  
Netaji Subhas University of Technology (NSUT)  
Roll Number: 2025UME7607  

---

## Table of Contents
1. [The Game as a Mathematical Problem](#1-the-game-as-a-mathematical-problem)
2. [Bayesian Posterior Estimation](#2-bayesian-posterior-estimation)
3. [Exact Lattice Expected Value](#3-exact-lattice-expected-value)
4. [Maker Obligation Edge](#4-maker-obligation-edge)
5. [TE Shadow Pricing](#5-te-shadow-pricing)
6. [Auction Theory: First-Price Bidding](#6-auction-theory-first-price-bidding)
7. [Negotiation as Optimal Stopping](#7-negotiation-as-optimal-stopping)
8. [TRANSFORM Decision Theory](#8-transform-decision-theory)
9. [Summary of Mathematical Edges](#9-summary-of-mathematical-edges)

---

## 1. The Game as a Mathematical Problem

### The Hidden Score
```text
The game revolves around a hidden score `S` defined as:
S = Σ cᵢ for i = 1 to 40, cᵢ ∈ {+1, -1}

Each coin is independently `+1` or `-1` with probability 1/2. Therefore:
E[S] = 0
Var[S] = 40 × Var[cᵢ] = 40 × 1 = 40
σ(S) = √40 ≈ 6.32
```

### Information Structure

At round `r`:
- **Your revealed coins:** 4r coins, sum = `k_mine`
- **Opponent's revealed coins:** 4r coins (unknown except via FORESIGHT)
- **Total revealed:** 8r coins
- **Remaining unseen:** 40 - 8r coins

### The Estimation Problem

We want to estimate `S` given:
1. Our revealed coins (`k_mine`)
2. Any FORESIGHT leak (opponent's revealed coins)
3. Opponent's opening quote (signal about their hand)

This is fundamentally a **Bayesian estimation problem** with noisy signals.

---

## 2. Bayesian Posterior Estimation

### The Basic Estimator

Without any information about the opponent, the best estimate is:
E[S | my_coins] = k_mine + E[opponent_coins]= k_mine + 0 (opponent coins are mean-zero to us)

### Incorporating FORESIGHT

When we hold FORESIGHT and see `f` opponent coins:
E[S | k_mine, foresight] = k_mine + sum(foresight)

But this only samples `min(16, 4r)` of the opponent's `4r` revealed coins, so it's a partial signal.

### Incorporating Opponent's Quote

The opponent's opening quote midpoint `(bid + ask) / 2` is a noisy estimate of their revealed sum. Let this be `q`.

We combine our signals using **inverse-variance weighting**:
Combined Estimate = (x₁/σ₁² + x₂/σ₂²) / (1/σ₁² + 1/σ₂²)

Where:
- `x₁ = k_mine + foresight_sum` (our direct observation)
- `x₂ = q` (opponent's quote midpoint, adjusted for overlap)

### The Reliability-Weighted Posterior

In our bot, we use a simpler but effective reliability-weighted blend:
mean = k_mine + foresight_sum + reliability × opponent_anchor_residual

Where:
reliability = max(0.50, min(0.78, 0.78 - 0.045 × (width - final_cap)))

**Why this works:**
- A wider quote (larger width) is less trustworthy → lower reliability
- The reliability is bounded to avoid overconfidence
- The `0.045` coefficient was calibrated empirically

### Standard Deviation Update
sd = sqrt(unseen) × (1 - 0.18 × reliability)

**Rationale:**
- Base uncertainty: `sqrt(unseen)` (random walk of remaining coins)
- Higher reliability → more certainty → lower sd
- The `0.18` factor was calibrated to prevent extreme confidence

### Handling FORESIGHT Overlap

The opponent's quote already reflects their revealed coins. If FORESIGHT reveals some of those same coins, we have overlap:
overlap = min(anchor_round, obs.round) / obs.round
residual_anchor = anchor_mid - overlap × foresight_sum

This prevents **double-counting** information.

---

## 3. Exact Lattice Expected Value

### The Distribution of Remaining Coins

At round `r`, we know:
- Our revealed coins: `4r` coins, sum = `k_mine`
- Remaining unseen: `U = 40 - 4r - |foresight|` coins

Each unseen coin is `+1` or `-1` with probability 1/2. The sum of `U` unseen coins follows a **binomial distribution**:

P(sum = s) = C(U, (s + U)/2) / 2^U

### Exact EV Calculation

For a contract at price `p`, the expected PnL is:
EV(long) = Σ [P(S = s) × (s - p)]
EV(short) = Σ [P(S = s) × (p - s)]

Using the exact binomial distribution:

```python
EV = Σ [comb(unseen, plus) / 2^unseen × pnl(score, price)]
```

Where:

score = k_mine + foresight_sum + 2 × plus - unseen + shift
shift adjusts for the difference between our estimate and our belief

SUBSTITUTE Adjustment
When holding SUBSTITUTE, losses are capped at -2 ticks:
```python
pnl = max(pnl, -2.0)
```

This is equivalent to a put option on our position, truncating the loss distribution from below.

Why Exact Computation Matters
Approximating the binomial with a normal distribution introduces errors, especially in the tails. The exact lattice computation:

Preserves parity: The sum of coins has the same parity as the count

Captures tails: Extreme outcomes are correctly weighted

No approximation error: The EV is exactly correct

---

## 4. Maker Obligation Edge

### The Maker Obligation

The engine charges the Maker based on the probability their opening quote contains S:
if open_bid ≤ S ≤ open_ask:  Taker pays Maker  λ × (1 - p_w)
otherwise:                    Maker pays Taker  λ × p_w
and always:                   Maker pays Taker  η × (w - floor)
Where:

λ = MAKER_OBLIGATION = 3.0

η = WIDTH_PREMIUM = 0.22

p_w = straddle_prob(r, w) — probability a width-w quote contains S

### The Information Asymmetry

p_default = straddle_prob(r, w) is computed by the engine assuming the Maker has seen default_unseen coins:
default_unseen = N_COINS - REVEAL_PER_ROUND × r
But when we hold FORESIGHT and see f opponent coins:
effective_unseen = default_unseen - f
Our actual unseen count is lower than what the engine assumes. Therefore:
p_true(w) > p_default(w)

### The Provable Edge

The expected transfer from the Maker obligation is:
```python
E[transfer] = MAKER_OBLIGATION × (p_true(w) - p_default(w))- WIDTH_PREMIUM × (w - final_cap)
```

Since p_true(w) > p_default(w), the first term is strictly positive whenever we hold FORESIGHT. This is free EV — we get paid for being better informed than the engine gives us credit for.
### Optimal Width Selection

We choose the width maximizing:
```python
net_edge(w) = MAKER_OBLIGATION × (p_true(w) - p_default(w))  - WIDTH_PREMIUM × (w - final_cap)
```

Without FORESIGHT: p_true = p_default, so net_edge(w) = -WIDTH_PREMIUM × (w - final_cap), which is maximized at w = final_cap (minimum width).

With FORESIGHT: We search over all widths to find the one maximizing the net edge.

## 5. TE Shadow Pricing

### The Opportunity Cost of TE

Every TE point has two uses:

Spend now: Bid on a power in this round's auction

Save for later: Bid on a future power, or bank as salvage value
The salvage value is:
TE_SALVAGE = 0.08 ticks/point
So 1 tick = 12.5 TE.

### The Dynamic Exchange Rate

The true cost of spending TE is not just the salvage value — it's the opportunity cost of not having that TE for a future power:
rate = TE_PER_TICK × (1 - TE_RESERVE_STRENGTH × scarcity)
where:
scarcity = expected_future_demand_te / te_mine

### Expected Future Demand

We estimate future demand by assuming each future round offers a power worth the average tick value:
```python
avg_power_ticks = sum(POWER_VALUES) / len(POWER_VALUES)
expected_future_demand_te = rounds_left_after_this × avg_power_ticks × TE_PER_TICK
```

### Why It Matters

Without shadow pricing, we might:

Overbid early: Spend too much TE on a marginal power, leaving nothing for a critical later power

Underbid late: Not realize that TE is now cheap (nothing left to save for), and miss buying a power that would have won the deal

Shadow pricing ensures we:

Bid less when TE is scarce (early rounds, many future powers)

Bid more when TE is plentiful (late rounds, few future powers)

---

## 6. Auction Theory: First-Price Bidding

### The First-Price Auction
In a first-price auction:

The highest bidder wins
The winner pays their own bid
The loser pays nothing

### The Shading Problem
If you bid your true value v, your profit is:
Profit = v - v = 0

You capture zero surplus by bidding your true value. Therefore, you must shade your bid down.

### Optimal Shading
In a first-price auction with independent private values, the optimal bid is:
b = v - (v - v_second) × P(you win | bid = b)
For our bot, we use a simple shading factor:
```python
shaded = floor(0.60 × fair_te)
```

### The Shading Factor

The 0.60 factor was calibrated empirically. It represents the fraction of true value we bid. This captures a reasonable surplus while remaining competitive.

Why 0.60?

Too high (>0.80): We overpay, capturing little surplus
Too low (<0.40): We lose too many auctions, giving up the power's value
0.60: Balanced between winning auctions and capturing surplus

### Opponent-Aware Bidding

We also adjust our bid based on the opponent's observed bidding behavior:
contest = floor(opponent_anchor) + 1
if contest <= 0.72 × fair_te:
    shaded = max(shaded, contest)

This ensures we don't lose an auction we should win, while avoiding overpaying for contested powers.

---

## 7. Negotiation as Optimal Stopping

### The Optimal Stopping Problem

The negotiation is a sequential decision problem:

At each turn, we either accept the current quote or counter
If we counter, the spread shrinks by at least 1 tick
On the final turn, if we counter, we force a midpoint fill and pay a fee

This is an optimal stopping problem: when should we stop negotiating and accept?

### Acceptance Thresholds

Our bot uses a probability-based threshold:
```python 
threshold = 0.12 + 0.04 × sd
```

Why this works:

Higher uncertainty (large sd) → wider threshold → accept less readily
Lower uncertainty (small sd) → tighter threshold → accept more readily
The 0.12 base ensures we don't accept trades with negligible edge
### The Final Turn Decision
On the final turn (turn 6), we compare three options:
```python
buy_ev = position_ev(obs, ask, long=True)
sell_ev = position_ev(obs, bid, long=False)
force_ev = position_ev(obs, forced_price, long=False) - FORCED_FILL_FEE
```

We choose the action with the highest expected value.

### The Forced Fill

If no acceptance by the final turn:
price = (bid + ask) // 2 + shift

Where shift accounts for TRICK_ROOM and STEALTH_ROCK effects. The // is floor division, ensuring integer prices.

---

## 8. TRANSFORM Decision Theory

### The Option to Swap

Winning TRANSFORM gives us the option to swap our entire hand with the opponent's. The power is consumed either way.

### When to Exercise

We exercise the swap only when the opponent's hand appears more decisive:
```python
return abs(opponent_estimate) > abs(k_mine) + 1.25
```

Rationale:
If our hand is flat (k_mine ≈ 0), it's worthless — swap for a better hand
If our hand is decisive (k_mine far from 0), it's valuable — keep it
If the opponent's hand is more decisive than ours, swapping improves our position

### The Denial Value

Sometimes, buying TRANSFORM and declining is the right move — it denies the opponent the option to swap. This is valuable when:
Our hand is strong
The opponent's hand appears flat (they'd want to swap)

### Decision Boundary
Our decision is based on the difference between our hand strength and the opponent's:
Exercise if: |opponent| > |k_mine| + 1.25
Decline if:  |opponent| ≤ |k_mine| + 1.25

The 1.25 margin accounts for estimation noise in the opponent's hand.

---

## 9. Summary of Mathematical Edges

|**Edge**|**Mathematical Foundation**|**Why It Works**|
|---------|------------------------------|--------------------------------------|
|**Robust Posterior**|**Bayesian estimation with reliability weighting**|**Combines noisy signals optimally**|
|**Exact Lattice EV**|**Binomial distribution exact computation**|**No approximation error in pricing**|
|**Maker Obligation**|**Edge Information asymmetry in straddle probability**|**Engine underprices our true certainty**|
|**TE Shadow Pricing**|**Dynamic opportunity cost of capital**|**Prevents budget misallocation**|
|**Auction Shading**|**First-price auction theory**|**Captures surplus by bidding below value**|
|**Adaptive Thresholds**|**Confidence intervals scaled by uncertainty**|**Accepts trades only when statistically profitable**|
|**TRANSFORM Decision**|**Option pricing under asymmetric information**|**Maximizes expected hand value**|

### Conclusion
The bot's success comes from applying rigorous mathematical principles to every decision:

Estimation: Bayesian posterior with reliability weighting
Pricing: Exact lattice EV computation
Edge Exploitation: Information asymmetry in the Maker obligation
Resource Allocation: TE shadow pricing with scarcity adjustment
Auctions: First-price shading with opponent modeling
Negotiation: Optimal stopping with probability-based thresholds

These mathematical foundations transform the game from a series of heuristics into a provably optimal decision-making process.

---