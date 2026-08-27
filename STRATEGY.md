# QuantStorm_RobustPosterior — Strategy Documentation

## Author
|**Name:**| Aashutosh Kumar  |
|**College:**| Netaji Subhas University of Technology (NSUT)|  
|**Roll Number:**| 2025UME7607|  
|**Email:**| 102ashutoshkumar@gmail.com|

---

## Overview

This bot was developed for **QuantStorm 2026** — a two-player, zero-sum game of incomplete information. The objective is to maximize PnL against a field of competing bots.

|**Final Result:**| Rank 137  |
|**Key Achievement:**| Beat all 10 benchmark bots with +77 Score during the 36-hour challenge extension.|
|**Duration**|36 hours(24 + 12 extension)|

---

## The Game: Divided Oracle

- A hidden score `S` is the sum of 40 coins (each `+1` or `-1`)
- Each player holds 20 coins, revealed 4 per round over 5 rounds
- Players bid Tactical Energy (TE) on 5 powers in auctions
- Players negotiate contracts on `S` before it is revealed
- The game is strictly zero-sum

---

## Core Strategy Components

### 1. Robust Posterior Belief System

**Mathematical Foundation:**

The bot estimates the final score `S` using a conservative posterior:

```python
mean = k_mine + foresight_sum + reliability × opponent_anchor_residual
sd = sqrt(unseen) × (1 - 0.18 × reliability)

---

### 2. Exact Lattice EV Calculation

**Mathematical Foundation:**

Instead of approximating expected value, the bot computes it exactly using combinatorial probabilities:

```python
EV = Σ [comb(unseen, plus) / 2^unseen × pnl(score, price)]

---

### 3. Maker Obligation Edge Exploitation

**Mathematical Foundation:**

The engine charges Maker obligation based on straddle_prob(r, w) — the probability an honestly-centered quote of width w contains S. The edge comes from:

```python
E[transfer] = MAKER_OBLIGATION × (p_true(w) - p_default(w))

---

### 4. TE Shadow Pricing

**Mathematical Foundation:**

Tactical Energy has opportunity cost beyond its flat salvage value:

```python
rate = TE_PER_TICK × (1 - TE_RESERVE_STRENGTH × scarcity)

---

### 5. Auction Power Valuation

####calibrated base value for each round each power each value 

```python
shaded = floor(0.60 × fair_te)

## Usage 

duel against baseline
**python backtester.py --bot1 RobustPosterior.py --bot2 rational.py**

validate submission
**python backtester.py --validate RobustPosterior.py**

Test under tournament conditions
**python backtester.py --bot1 RobustPosterior.py --bot2 strategies/rational.py --isolate**

## Conclusion

This bot represents the culmination of 36 hours of intense iteration — from catastrophic failures to beating all benchmark bots. The mathematical rigor (exact EV, provable edges, dynamic pricing) is the foundation of its success.
**The key insight:** In a zero-sum game of incomplete information, the edges come from:**
|**Information asymmetry**|
|**Exact computation**|
|**Budget discipline **|
|**Adaptive behaviour**|



