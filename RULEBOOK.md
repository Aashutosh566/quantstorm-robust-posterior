# Divided Oracle — Official Rulebook

**QuantStorm 2026, Round 1.** This document is the complete specification.
Everything the engine does is described here. Where this document and the code
disagree, the code is authoritative and the discrepancy is a bug — report it.

**If you are in any doubt about a rule, a piece of game logic or a constraint,
ask the hosts.** Use the [WhatsApp
community](https://chat.whatsapp.com/ELJQfcO8VUT3nmwhdXvhmw) — questions there
are answered for everyone, and asking is always better than guessing and
building on a wrong assumption.

**Stay active in the community for the whole competition.** Rules may be
clarified or changed during the event, and any such change is announced there.
It is your responsibility to keep up with announcements.

---

## Contents

1. [Problem statement](#1-problem-statement)
2. [The asset](#2-the-asset)
3. [Structure of play](#3-structure-of-play)
4. [Phase 1 — the auction](#4-phase-1--the-auction)
5. [The five powers](#5-the-five-powers)
6. [Phase 2 — the negotiation](#6-phase-2--the-negotiation)
7. [Settlement](#7-settlement)
8. [The bot interface](#8-the-bot-interface)
9. [The `Obs` object](#9-the-obs-object)
10. [The `GameConfig` object](#10-the-gameconfig-object)
11. [Legality, clamping and crash fallbacks](#11-legality-clamping-and-crash-fallbacks)
12. [Submission rules](#12-submission-rules)
13. [Tournament format](#13-tournament-format)
14. [The provided code](#14-the-provided-code)
15. [Complete parameter reference](#15-complete-parameter-reference)

---

## 1. Problem statement

You are writing a trading bot. It plays a two-player, zero-sum game of
incomplete information against another entrant's bot.

A hidden number `S` exists. You and your opponent each hold half the
information needed to compute it. Over five rounds you learn more of your half,
you bid for powers that change what you can see or how contracts settle, and
you negotiate contracts on `S` before you know it.

At the end of a deal, `S` is revealed and every contract settles. If you bought
below `S` or sold above it, you profit. Your opponent loses exactly what you
gain.

**Your objective is to maximise your PnL against the field.** There is no other
objective. The game is strictly zero-sum: the engine asserts on every deal that
the two seats' PnL sums to zero within `1e-9`.

Two things determine your result, and both are load-bearing:

* **Pricing.** Estimating `S` from what you have seen and from what your
  opponent's quotes reveal, then trading only when you have an edge.
* **The auction.** Deciding what each power is worth in your current state and
  what to pay for it against a budget that cannot buy everything.

---

## 2. The asset

A hidden score `S` is the sum of **40 coins**, each independently `+1` or `-1`
with probability 1/2.

```
S = Σ cᵢ        for i = 1..40        S ∈ [-40, +40]
```

* 20 coins are dealt to you, 20 to your opponent.
* Nothing is public. You never see your opponent's coins except through a
  `FORESIGHT` leak.
* Each round, **4 more of your own coins are revealed to you**. After round 5
  you have seen all 20 of yours and none of theirs.

`obs.k_mine` is the sum of your revealed coins. `config.residual_sd(r)` and
`config.unknown_to_both(r)` are available if you want them; what they mean is
in §10.

---

## 3. Structure of play

* A **deal** is 5 rounds.
* A **match** is a number of deals, played in two phases: `N` direct deals,
  then `N` **mirror** deals.
* Each round is: *reveal → auction → negotiation → one contract*.

```
4 new coins revealed  →  blind TE auction  →  negotiation (≤6 turns)  →  1 contract
```

Five contracts are formed per deal, one per round. All five settle at once at
the end of the deal.

### Maker and Taker

One player is the **Maker** each round; the other is the **Taker**. The Maker
opens the negotiation with a two-sided quote.

* Bot A is Maker in rounds 1, 3, 5.
* Bot B is Maker in rounds 2, 4.

In the mirror phase this is inverted.

### The mirror

Every deal is played twice: once as dealt, then immediately again with the
private hands swapped and the Maker roles inverted, on the same coin vector.
The two legs therefore interleave — direct, mirror, direct, mirror — rather
than running as two separate phases.

This is a **fairness** device, not a variance-reduction device. Both bots play
both hands from both seats, so no result depends on who was dealt what. Two
instances of the same bot score exactly `0.0` over a mirrored match.

Randomness that must be replayed rather than redrawn — which seat wins a tied
auction, which coins `FORESIGHT` reveals, and which powers are drawn onto the
slate — is keyed on the deal seed alone, not on the mirror leg. Everything else
is redrawn.

---

## 4. Phase 1 — the auction

### Tactical Energy

You start **each deal** with **24 Tactical Energy (TE)**.

* It does **not** carry over between deals.
* It does **not** replenish between rounds within a deal. The five rounds of
  one deal share a single 24-point budget.
* **Unspent TE settles as PnL at 0.08 ticks per point**, on the *difference*
  between the two players' unspent balances (see §7).

One tick is therefore worth 12.5 TE, and your whole budget is worth 1.92 ticks.

### The slate is drawn, not published

**One power goes on the block each round.** Which one is **drawn at random**
from the powers eligible in that round. You do not know in advance which power
you will be offered.

| Power | Eligible in rounds |
|---|---|
| `FORESIGHT` | 1, 2, 3, 4, 5 |
| `TRICK_ROOM` | 1, 2, 3, 4, 5 |
| `SUBSTITUTE` | 1, 2, 3, 4, 5 |
| `STEALTH_ROCK` | 1, 2, 3, 4 |
| `TRANSFORM` | 1, 2, 3 |

Rules of the draw:

* The whole deal's slate is drawn **before round 1**, from a stream keyed only
  on the deal seed. Nothing either bot does can change it.
* Both players face the **same** slate. The mirror leg replays it exactly.
* A **once-per-deal** power (`STEALTH_ROCK`, `TRANSFORM`) is placed in the
  slate **at most once per deal**.
* You learn the round's power when `bid()` is called, via the `offered`
  argument. You are never told future rounds' draws.
* If a once-per-deal power was already **won** in an earlier round it is
  removed from the block; because of the placement rule above this cannot
  happen, so every round has exactly one power on the block.

A deal therefore contains **5 power-slots** against a budget that buys about
three of them.

### How the auction resolves

Both bots submit blind, simultaneous TE bids on the offered power.

1. **First price.** The higher bid wins and **pays its own bid**. The loser
   pays nothing.
2. **Equal non-zero bids** are settled by a **coin flip**. The winner still
   pays its bid.
3. **Both bid zero** → nobody wins the power. No coin flip, no free power.
4. If the winner's bid exceeds its remaining TE at the moment of resolution,
   the power is **not awarded** and no TE is spent.
5. Wins are broadcast on the public auction tape. Your opponent learns that you
   hold the power **and what you paid for it** — the winning bid is recorded on
   the tape as `cost`. What stays private is a *losing* bid, and whatever the
   power showed you.

### Bid legality

`bid()` returns a `dict[str, int]` mapping power name to TE.

* Keys not in `offered` are **discarded** (with a warning).
* Non-numeric values are **discarded**.
* Negative values are **clamped to 0**.
* Values are clamped to `MAX_REASONABLE_VALUE = 10000`.
* If your bids **total** more than `obs.te_mine`, **every bid in the vector is
  set to 0** — you contest nothing that round. It is counted as a clamp. The
  engine does not rescale your vector to fit and does not guess which part of
  it you meant: **track your own budget.**
* Returning `{}` bids nothing.

Overbidding is therefore not a cheap way to spell "all-in". A vector that does
not fit does not win a smaller version of itself; it wins nothing, and if your
opponent bid anything at all they take the power uncontested.

---

## 5. The five powers

A power is held for the round in which it is won, except `STEALTH_ROCK`, which
persists. Winning a power is announced; its effect is not.

### `FORESIGHT` — magnitude 16

You are secretly shown **up to 16 of your opponent's currently-revealed coins**,
sampled without replacement. In round `r` your opponent has revealed `4r` coins,
so you see `min(16, 4r)` of them.

They arrive in `obs.foresight` as a tuple of `+1`/`-1` values, for **this round
only**.

`obs.foresight` is `()` when you do not hold `FORESIGHT`.

### `TRICK_ROOM` — magnitude 3

If the round's negotiation ends in a **forced midpoint fill**, the fill price
shifts **3 ticks in your favour**. It has no effect on a round that ends in an
acceptance.

### `SUBSTITUTE` — magnitude 2

Your loss on **this round's contract only** is capped at **2 ticks**. Profit is
uncapped. The refund is paid by your counterparty, so the deal stays zero-sum.

Applied at settlement, after `S` is known.

### `STEALTH_ROCK` — magnitude 2, persistent, once per deal

Every **forced midpoint fill for the remainder of the deal**, including the
round it was won in, shifts **2 ticks in your favour**. Not eligible in round 5,
where it would have nothing left to act on.

### `TRANSFORM` — once per deal

Winning `TRANSFORM` gives you the **option** to swap your entire 20-coin private
hand with your opponent's, revealed coins included. It does not fire
automatically.

The engine calls `use_transform(obs)`. Return `True` to swap, `False` to
decline. **The power is consumed either way**, so buying it and declining
permanently removes it from the deal.

The swap exchanges the full hands, so `S` is unchanged — it repartitions the
coins between the seats.

### Combining fill shifts

`TRICK_ROOM` and `STEALTH_ROCK` both shift forced fills. If both players hold
shift powers, the shifts **cancel exactly**:

```
shift = Σ (+magnitude for each shift power held by the short seat)
      + Σ (−magnitude for each shift power held by the long seat)

forced_fill_price = (bid + ask) // 2 + shift
```

The sign is applied so that each holder's shift moves the price in their own
favour. The engine computes the total magnitude a seat controls by summing the
magnitudes of the shift powers that seat holds; `engine.py` does this in
`shift_sources`, which you cannot import — `engine` is not on the permitted
list — so reimplement the sum from the table above if you need it.

---

## 6. Phase 2 — the negotiation

Up to **6 turns**. Turn 1 is the Maker's opening quote; turns 2–6 alternate,
starting with the Taker.

### Turn 1 — the opening quote

The Maker returns `(bid, ask)` with `bid <= ask`.

**You choose the width.** The opening spread must satisfy:

```
obs.final_cap  <=  ask - bid  <=  obs.spread_cap
```

| Round | Floor (`final_cap`) | Opening cap (`spread_cap`) |
|---|---|---|
| 1 | 4 | 9 |
| 2 | 4 | 9 |
| 3 | 3 | 8 |
| 4 | 3 | 8 |
| 5 | 2 | 7 |

**The band narrows as the deal progresses.** Round 1 is the widest, round 5 the
tightest. Both values are computed by the engine and handed to you on `obs` —
read them off `obs.final_cap` and `obs.spread_cap` rather than hard-coding the
table.

A width outside the band is **re-centred** onto the nearer edge: the midpoint is
preserved and the width is set to the floor or the cap.

Width is priced at settlement — see the maker obligation in §7.

### Turns 2–6 — accept or counter

The responder returns one of:

| Return value | Meaning |
|---|---|
| `"ACCEPT_BUY"` | You buy at the current `ask`. You are long, counterparty short. Contract price = `ask`. |
| `"ACCEPT_SELL"` | You sell at the current `bid`. You are short, counterparty long. Contract price = `bid`. |
| `("COUNTER", new_bid, new_ask)` | You propose a new range and pass the turn. |

A counter must satisfy all of:

1. `new_bid <= new_ask` — otherwise the two are swapped.
2. `bid <= new_bid` and `new_ask <= ask` — the new range must lie **inside** the
   current one. Values outside are clamped in.
3. The spread must **shrink by at least `MIN_REDUCTION = 1` tick**, but is
   **never forced below the round's floor**. Formally, the maximum permitted
   new width is:

   ```
   max_width = min(ask - bid, max(final_cap, (ask - bid) - MIN_REDUCTION))
   ```

   A counter wider than this is re-centred within the current range at
   `max_width`.

Consequence: if the Maker opens **at the floor**, no counter can shrink it. The
price is take-it-or-leave-it for the whole round, and the Taker's only choices
are accept, or force the midpoint.

Opening **at the cap** with mandatory reduction every turn lands exactly on the
floor by turn 6.

### The midpoint trap

If turn 6 ends with no acceptance, the contract **auto-executes at the midpoint
of the final range**, adjusted by any fill shift:

```
price = (bid + ask) // 2 + shift
```

`//` is floor division; the price is always an integer.

**The bot that quoted last is the seller (short).** If nobody ever countered,
the Maker quoted last and is the seller.

**Forcing is not free.** The player who **countered instead of accepting on the
final turn** pays the counterparty a **2-tick forcing fee**. This is the
`forcer`, recorded on the contract.

---

## 7. Settlement

At the end of a deal all 40 coins are revealed, `S` is computed, and all five
contracts settle simultaneously.

### 7.1 Contract PnL

For each contract at price `p`:

```
long seat :  S - p
short seat:  p - S
```

### 7.2 Maker obligation

For each round, the Maker's **opening quote** is scored against `S`.

Let `w = open_ask - open_bid` be the opening width, `floor = final_cap(r)`, and
`p_w = straddle_prob(r, w)` the probability that an honestly-centred quote of
width `w` contains `S` in round `r`.

```
if open_bid <= S <= open_ask:   Taker pays Maker   3.0 * (1 - p_w)
otherwise:                      Maker pays Taker   3.0 * p_w
and always:                     Maker pays Taker   0.22 * (w - floor)
```

`3.0` is `MAKER_OBLIGATION`; `0.22` is `WIDTH_PREMIUM`. Both transfers are
zero-sum.

`p_w` is whatever `config.straddle_prob(r, w)` returns. It is computed exactly,
not from a normal approximation, so read it from the config rather than
reimplementing it.

`config.straddle_prob(r, w, unseen=n)` returns the same quantity for a Maker who
can see all but `n` of the coins. The obligation always scores against the
default `unseen`.

### 7.3 Forcing fees

For each forced contract with a recorded forcer:

```
forcer pays counterparty  2.0
```

### 7.4 `SUBSTITUTE`

For each round in which a seat held `SUBSTITUTE`, if that seat's raw contract
PnL for that round is below `-2.0`, the counterparty refunds the difference.

### 7.5 TE salvage

```
seat 0 receives  0.08 * (te_left[0] - te_left[1])
seat 1 receives  0.08 * (te_left[1] - te_left[0])
```

### 7.6 Zero-sum assertion

The engine raises `AssertionError` if `|pnl[0] + pnl[1]| > 1e-9`. Every rule
above is a transfer; nothing is created or destroyed.

---

## 8. The bot interface

Your file must define a class named exactly `Bot`. It is constructed with **no
arguments**. All five methods below are **required** — a submission missing any
one is rejected before the tournament runs.

```python
class Bot:
    name = "YourBotName"          # optional, used for display only

    def reset(self, seat: int, config: GameConfig, seed: int) -> None:
        """Called once at the start of every deal, before round 1.

        seat   : 0 or 1. Your seat for this deal.
        config : the frozen GameConfig. Read it; you cannot modify it.
        seed   : an int you may use to seed your own random.Random.

        ALL per-deal state must be established here.
        """

    def bid(self, obs: Obs, offered: list[str]) -> dict[str, int]:
        """Blind TE bids for this round's auction.

        offered : the powers on the block this round (a list you may mutate;
                  it is a copy).
        Return  : {power_name: te_amount}. Return {} to bid nothing.
                  A power you omit is free to your opponent.
        """

    def quote(self, obs: Obs) -> tuple[int, int]:
        """Your opening quote. Called only when obs.is_maker is True.

        Return (bid, ask) with
            obs.final_cap <= ask - bid <= obs.spread_cap
        """

    def respond(self, obs: Obs, quote: tuple[int, int], turn: int):
        """Your move in the negotiation. Called on turns 2..N_TURNS.

        quote : the (bid, ask) currently on the table.
        turn  : the turn number, 2..6. turn == config.N_TURNS is the last one;
                countering there forces the midpoint fill and costs you the
                2-tick forcing fee.

        Return "ACCEPT_BUY", "ACCEPT_SELL", or ("COUNTER", new_bid, new_ask).
        """

    def use_transform(self, obs: Obs) -> bool:
        """Called only if you won TRANSFORM this round.

        Return True to swap hands, False to decline. The power is consumed
        either way.
        """
```

### Statelessness — enforced

You may remember anything you like **across the 5 rounds of one deal**.

You may **not** carry state between deals, and this is enforced rather than
requested:

* Your module is re-executed from source before every deal, so module-level
  globals, class attributes and caches all start empty.
* A fresh `Bot` instance plays each deal.
* The standard-library modules you are allowed to import are **restored to
  their original contents** between deals, and so is `random`'s global
  generator. Parking a value on `functools`, on `collections.Counter`, or in
  the RNG stream does not survive.
* Every such attempt is **recorded against your entry** and surfaced to a human
  reviewer.
* The graded run goes further and gives you a brand-new interpreter per deal.

There is no hiding place, and nothing to gain from looking for one.

Run `python backtester.py --isolate` to duel under exactly these conditions. If
your bot scores differently with `--isolate`, that is a bug in your bot, and it
will behave the isolated way on the day.

---

## 9. The `Obs` object

A frozen dataclass. All fields are immutable; you cannot modify it. A fresh one
is built for every call.

| Field | Type | Meaning |
|---|---|---|
| `seat` | `int` | Your seat, 0 or 1 |
| `round` | `int` | Current round, 1–5 |
| `my_revealed` | `tuple[int, ...]` | Your revealed coins so far, each `+1`/`-1`. Length `4 * round`. |
| `te_mine` | `int` | Your remaining TE |
| `te_theirs` | `int` | Opponent's remaining TE |
| `spread_cap` | `int` | Maximum opening spread this round |
| `final_cap` | `int` | Minimum opening spread this round, and the width below which a counter is never *forced* to shrink. |
| `is_maker` | `bool` | Are you the Maker this round |
| `powers_mine` | `frozenset[str]` | Powers active for you this round |
| `powers_theirs` | `frozenset[str]` | Powers active for your opponent this round |
| `auction_log` | `tuple[dict, ...]` | Public tape. Each entry `{"round", "seat", "power", "cost"}`. Includes both seats' wins and what was paid. |
| `contracts` | `tuple[Contract, ...]` | Contracts formed in this deal so far |
| `foresight` | `tuple[int, ...]` | Your `FORESIGHT` leak this round; `()` if you do not hold it |
| `n_unknown_both` | `int` | Coins neither player has seen: `40 - 8 * round` |
| `n_turns` | `int` | Total negotiation turns allowed (6) |

Derived property:

| Property | Meaning |
|---|---|
| `obs.k_mine` | `sum(obs.my_revealed)` — the sum of your revealed coins |

### `Contract`

A `NamedTuple`, as found in `obs.contracts`. Fields are readable by name, and
the first four are also readable positionally (`c[0]` is `c.round`):

| Field | Type | Meaning |
|---|---|---|
| `round` | `int` | Which round, 1–5 |
| `price` | `int` | Execution price |
| `long_seat` | `int` | Seat that is long (buyer) |
| `forced` | `bool` | `True` if the contract was a forced midpoint fill |
| `forcer` | `int` | Seat that countered on the final turn; `-1` if not forced |
| `shift` | `int` | Power-induced price shift applied; forced fills only |
| `maker_seat` | `int` | Who was Maker that round |
| `open_bid` | `int` | The Maker's opening bid |
| `open_ask` | `int` | The Maker's opening ask |

`open_bid` and `open_ask` are the only clean read of a Maker's information.
Later ranges are negotiated objects contaminated by both sides.

---

## 10. The `GameConfig` object

Handed to you in `reset()`. **Frozen** — any attempt to assign an attribute
raises `ConfigError`. The same instance is reused for the whole tournament.

Read any attribute in §15. The useful methods:

| Method | Returns |
|---|---|
| `config.final_cap(r)` | Minimum opening spread / negotiation floor for round `r` |
| `config.spread_cap(r)` | Maximum opening spread for round `r` |
| `config.residual_sd(r)` | Standard deviation of the score you cannot see after round `r` |
| `config.unknown_to_both(r)` | Coins neither player has seen after round `r` |
| `config.straddle_prob(r, width=None, unseen=None)` | Exact probability an honestly-centred quote of that width contains `S`. `width` defaults to `spread_cap(r)`; `unseen` overrides the number of coins you cannot see. |
| `config.baseline_straddle(r)` | `straddle_prob(r, spread_cap(r))` |
| `config.offered_powers(r)` | Powers **eligible** in round `r`. This is the pool the slate is drawn from, **not** the slate. Use the `offered` argument of `bid()` for what is actually on the block. |
| `config.sigma_score()` | `sqrt(40) ≈ 6.32` |

---

## 11. Legality, clamping and crash fallbacks

Nothing your bot returns can crash the engine. Illegal values are clamped to
the nearest legal value and a warning is recorded against you. Warnings do not
cost PnL directly, but a clamped action is almost never the action you meant.

### If a call fails

A call "fails" if it raises, returns the wrong type, or exceeds the hard time
limit.

| Method | Fallback |
|---|---|
| `reset()` | Every remaining call in the deal takes its own fallback below. This is the most expensive failure in the table: a `reset()` that raises forfeits the whole deal's play, so keep it short and keep it total. A constructor (`__init__`) that raises is treated the same way. |
| `bid()` | You bid `0` on everything this round |
| `quote()` | A quote centred on `0` at the round's **maximum** spread |
| `respond()` | `"ACCEPT_BUY"` at the opponent's current ask |
| `use_transform()` | `False` — decline. You can lose option value you paid for, but you can never be tricked into giving away a hand you liked. |

A quote is additionally slid back inside the range the score can actually take
(`|S| <= 40`) if it lies entirely outside it. A price the game can never reach
is not a price, and the counterparty would simply fill you at it.

A call that exceeds the **hard** time limit is abandoned where it stands, takes
its fallback, and **ends your participation in that deal** — every remaining
action in that deal also takes its fallback.

### Clamping rules, in full

**`bid()`**
* Non-`dict` return → `{}`.
* Keys not in `offered` → discarded.
* Non-numeric values → discarded.
* Negative → clamped to `0`.
* Above `10000` → clamped to `10000`.
* Total above `obs.te_mine` → **every bid becomes `0`**. Counted as a clamp.
* `te_mine <= 0` → any non-zero bid total becomes `0`, same rule.

**`quote()`**
* Non-indexable or fewer than 2 values → default quote.
* Values clamped to `±10000`.
* `bid > ask` → swapped.
* Width outside `[final_cap, spread_cap]` → re-centred on the nearer edge,
  preserving the midpoint.

**`respond()`**
* Unrecognised string → `"ACCEPT_BUY"`.
* Unparseable return → `"ACCEPT_BUY"`.
* First element not `"COUNTER"` → `"ACCEPT_BUY"`.
* Values clamped to `±10000`.
* `new_bid > new_ask` → swapped.
* Range outside the current range → clamped inside it.
* Width above `min(ask-bid, max(final_cap, (ask-bid) - 1))` → re-centred at that
  width.

**`use_transform()`**
* Any non-boolean return is coerced; a failure returns `False`.

### Time and resource limits

| Limit | Value | Enforced? |
|---|---|---|
| Average per call, across a match | **2 ms** | No — guidance only |
| Absolute maximum, any single call | **50 ms** | Yes — counts a violation |
| Hard-limit violations before forfeit | **5** (the 6th forfeits) | Yes |
| Forfeit penalty | **250 PnL**, transferred to your opponent | Yes |
| Memory per worker | **512 MB** | Yes — by the OS |
| CPU seconds per worker, per match | **600 s** | Yes — by the OS |

The **2 ms average is a design target, not a rule**. Nothing in the engine
scores it and no penalty follows from missing it. It is the budget the game was
balanced around, and a bot that sits near it will never come close to the 50 ms
limit that does bite. The local backtester prints a warning when your average
exceeds it; the tournament does not.

Your bot runs in its own process with a timer around every call.

* The figure you are **charged** against the 50 ms limit is measured inside your
  own process, around your own call. It excludes IPC and the harness's own
  scheduling, so you are never billed for the tournament being busy on other
  pairings. It is **elapsed time inside your worker**, not CPU time: a garbage
  collection pause or a page fault during your call counts toward it.
* The figure that gets you **killed** is your CPU time, watched from outside,
  and that one is CPU rather than elapsed — a descheduled worker accrues none of
  it. A call burning more than roughly twice the hard limit in CPU has its
  worker destroyed. A bot stuck inside a single C-level operation
  (`math.factorial(10**6)`, a 30-million-element sort, a catastrophic regex)
  cannot be interrupted from within, because there is no point between
  bytecodes at which to raise anything.
* `MATCH_TIMEOUT_S` is a CPU-second backstop across the whole match, not a
  wall-clock deadline.

**What an overrun costs you depends on the platform.** On Linux and macOS an
in-worker timer aborts the call at 50 ms; the action takes its fallback and
**the rest of that deal is forfeited to fallbacks as well**, because the call
was abandoned mid-frame. Windows has no equivalent timer, so the call runs to
completion and only the violation is recorded. The graded run is containerised,
so assume the stricter behaviour: **a local Windows run is more forgiving than
the tournament, and is not a safe calibration for timing.**

A forfeit is charged to the offending seat specifically, never to whoever was
listed first, and is applied as a transfer so the match stays zero-sum. An
infinite loop is a fast, expensive loss — not a hung tournament.

### Memory — enforced by the operating system

Your worker gets **512 MB**, capped by the OS rather than by Python:
`RLIMIT_AS` on Linux and macOS, a Job Object process memory limit on Windows.
Allocating past it raises `MemoryError` in your own frame and is scored like
any other crash.

Your worker also **cannot start child processes**, so you cannot obtain more
memory or CPU by delegating.

### Return only plain values

Your methods must return the documented types — a `dict`, a two-integer tuple,
a string, a bool. The worker reduces every return value to plain integers,
strings, tuples and dicts before it crosses back to the engine. Anything that
cannot be reduced is treated as a crashed call and takes the fallback above.

This only matters if you return something exotic: a custom class, a generator,
an array-like object, or an integer wider than 64 bits. Return the documented
type and you will never notice.

### Note on garbage collection

The 50 ms limit exists to stop infinite loops, deliberate stalling and
catastrophically unoptimised code — not to punish ordinary variance. But be
clear about what the clock measures: it is elapsed time inside your worker, so
a GC pause landing inside one of your calls does count toward that call.

In practice this is not something to engineer around. A bot working at this
scale holds a small heap, where collection pauses are microseconds rather than
tens of milliseconds, and the 50 ms limit sits far above anything a reasonable
bot spends in a call. The five-violation allowance exists precisely to absorb
occasional noise — a cold cache, an unlucky sweep — before anything is
forfeited.

**You do not need to call `gc.disable()`**, and doing so is more likely to hurt
you than help: you keep the same 512 MB cap and give up the collector that
keeps you under it.

---

## 12. Submission rules

### File format

One `.py` file. It must begin with these three lines, filled in:

```python
# Name: Your Name
# College: Your College
# Roll Number: Your Roll Number
```

Missing, empty, or still showing a template placeholder such as `[Your Name]`
gets the file rejected. This is checked, not merely requested.

### Your entry must be your own

**Copying another participant's code is not allowed.** Submitting someone
else's bot, in whole or in part, or two entries sharing the same
implementation, is grounds for rejection or disqualification.

**This extends past the source text.** Lifting someone else's work and retyping
it, renaming it, or restructuring it is still copying. So is taking the
specific tuned internals of another entry — the exact weights in a valuation
table, the same thresholds, the same magic constants — even inside code you
wrote yourself. Two entries carrying the same hand-tuned numbers is not
something that happens by chance, and it will be read as copying.

**Discussion between participants is allowed**, and encouraged. Talking through
the maths, arguing about what a power is worth, comparing approaches and
helping someone debug are all fine. What separates that from copying is that
you go away and do your own work afterwards — your own implementation, and
your own numbers, measured by you.

Two things are explicitly **not** evidence against you:

* **The code shipped in this repository.** `starter_bot.py` and the three
  provided strategies are yours to copy freely, constants included — that is
  what they are for.
* **Values that follow from the spec.** Anything derivable from this rulebook
  or from `game_config.py` will naturally match across entries, and matching
  there means both of you did the arithmetic correctly.

Where a case is unclear, **the hosts decide**, and their judgement is final.
If you are unsure whether something you are about to do crosses the line, ask
before you submit rather than after.

### Permitted imports

Only these modules:

```
math   random   statistics   collections   heapq   bisect   itertools   functools   typing
```

`from __future__ import annotations` is permitted as well. It is a compiler
directive rather than a library, and on Python 3.12+ it is redundant, but it is
common enough in ordinary code that refusing it only cost entries.

`numpy`, `scipy`, `pandas`, and anything touching the network, filesystem or OS
are forbidden.

### Forbidden constructs

* Calls: `__import__`, `eval`, `exec`, `compile`, `open`, `input`, `globals`,
  `vars`, `breakpoint`, `memoryview`
* Attributes: `__class__`, `__bases__`, `__mro__`, `__subclasses__`,
  `__globals__`, `__builtins__`, `__code__`, `__closure__`, `__dict__`,
  `__getattribute__`, `__reduce__`, `__reduce_ex__`, `__loader__`, `__spec__`,
  `f_back`, `f_locals`, `f_globals`, `f_builtins`, `tb_frame`, `tb_next`,
  `gi_frame`, `cr_frame`, `_getframe`, `__traceback__`

All of these are refused by a static scan **before** your file is executed.

At runtime a second layer backs the scan up: the module cache is purged, the
builtins are rewritten, and an audit hook — which CPython provides no way to
uninstall — refuses imports outside the allowlist, `eval`/`exec`/`compile`,
`open`, `input`, and every operation that reaches the network, filesystem or
OS. It sits below the Python object graph, so it catches those however they
were reached.

`collections.namedtuple` and `typing.NamedTuple` both work, despite building
themselves with `eval` internally, and both keep working under
`from __future__ import annotations` — which forces the annotations to be
evaluated back from strings, one more thing that looks like `compile()` from
the outside. Annotation expressions are the only strings a submission may
compile; anything that could call something is still refused.

This is not a puzzle to solve. An entry that trips these checks is **rejected,
not penalised** — you will simply not be in the tournament.

### If you find a hole

If you find a way past the sandbox, **report it to the organisers.** There is no
credit or reward for doing so — reporting it simply costs you nothing.

Using one in a submission does cost you. A finding used in a submission is
**disqualification**, not a high score: the graded run is executed in a
container with no network, a read-only filesystem and OS-enforced resource
caps, and every anomaly it records is reviewed by a human. The only thing
hunting for holes can do to your result is end it.

### Check your submission

```bash
python backtester.py --validate strategies/my_bot.py
```

Nothing is executed by this check. It verifies metadata, imports, forbidden
constructs, the presence of a `Bot` class, and all five required methods.

---

## 13. Tournament format

Up to three stages. What each one measures, and what it does with the number:

| Stage | Field | What is measured | What it decides |
|---|---|---|---|
| 1 — Gate | all entries | PnL over mirrored deals against the gate strategy | pass / fail |
| 2 — Swiss *(may be skipped)* | those that passed | match wins, then cumulative PnL, then Buchholz cut 1 | who continues |
| 3 — Round robin | those that continued | total PnL over your `n − 1` matches | **the final ranking** |

### Stage 1 — Gate

A pure 1v1 against **one gate strategy**, over multiple mirrored matches. Beat
it and you are in the tournament; fail and you are out.

**Which strategy the gate uses is not published.** Do not tune against a
specific opponent — the gate is a competence floor, and the way to clear it is
to price and bid well against anything.

### Stage 2 — Swiss

**This stage may be skipped.** Whether it runs at all depends on how many
submissions are received: with a small field every entry goes straight to the
round robin, and Swiss only exists to cut a field too large to play in full.

If it runs: multiple rounds of fixed-length matches, tiebreak on cumulative
PnL, then Buchholz cut 1. Swiss is a **cut**, not a ranking — nothing from it
carries into the final standings.

### Stage 3 — Round robin

Every surviving entry plays every other. With `n` entries that is `n − 1`
matches each.

**Your score is the sum of your PnL across all `n − 1` matches.** Highest total
wins. That is the whole ranking rule — no normalisation, no opponent
adjustment, no similarity or novelty term. Every entry plays the same set of
opponents, so the totals are directly comparable.

Ties are broken on head-to-head PnL.

### Repeated matchups

A matchup may be played **more than once** to reduce the influence of luck. If
so, every additional repetition is added into the same PnL total.

Two guarantees on this:

* **Every matchup is repeated the same number of times.** No pairing gets more
  or fewer runs than any other.
* **The count is chosen on feasibility alone** — how much compute and time the
  round robin has — and is fixed before the round robin is played. It is never
  chosen with reference to any entry's results, and no entry's schedule is
  altered by how it is performing.

### Scoring is PnL and nothing else

There is no novelty, diversity or similarity adjustment at any stage. Two
entries that play identically and earn identically are ranked identically —
neither is penalised for the resemblance.

AI assistance is permitted. So is arriving at the same answer as everyone else:
if a line of play is optimal, play it. The three provided strategies are worked
examples rather than a house style, but copying one wholesale is a bad idea only
because it will be beaten by entries that improve on it, not because of any
penalty.

None of this licenses copying another participant's code, which is a separate
rule and is not allowed — see §12. Reaching the same conclusion independently
is fine; taking someone else's implementation is not.

---

## 14. The provided code

```
.
├── RULEBOOK.md          this document
├── README.md            quick start
├── FAQs.txt             updated as the competition goes on
├── starter_bot.py       annotated template -- copy this, it becomes your entry
├── backtester.py        duel two bots, and check a submission would be accepted
├── strategies/
│   ├── naive_ev.py      baseline: prices on its own coins, never bids
│   ├── rational.py      baseline: reads the opponent's quote, never bids
│   └── adaptive_bidder.py   reference: reads quotes AND values the auction
├── engine.py            the game engine (do not modify)
├── game_config.py       every tunable parameter (do not modify)
├── bot_loader.py        the submission gate
├── sandbox.py           process isolation, statelessness enforcement
├── policy.py            import allowlist and the runtime audit hook
└── limits.py            OS-level memory, CPU and process caps
```

**Your submission is one file.** Copy `starter_bot.py`, fill it in, and submit
that single `.py`. Nothing else you write is collected.

### Duel two bots

```bash
python backtester.py --bot1 strategies/adaptive_bidder.py --bot2 strategies/rational.py
```

Prints a per-round log, per-call timings, summary statistics, and every warning
your bot generated.

| Flag | Effect |
|---|---|
| `--bot1 FILE`, `--bot2 FILE` | The two bots to duel |
| `--n_deals N` | Deals per phase; the mirror doubles it |
| `--seed N` | Reproduce an exact match |
| `--mirror` | Play mirrored deals (the default) |
| `--no_mirror` | Direct deals only |
| `--coins "+1,-1,..."` | Force an exact 40-coin vector for every deal |
| `--first_maker 0` / `1` | Override who opens round 1 |
| `--quiet` | Suppress the per-round log |
| `--isolate` | **Reproduce tournament conditions exactly**: separate processes, enforced time and memory limits, module reloaded between deals |
| `--validate FILE` | Check one submission would be accepted, then exit |

Run with no arguments and it duels the two baselines against each other.

### Check your file would be accepted

```bash
python backtester.py --validate strategies/my_bot.py
```

Nothing in the file is executed. This runs the same static gate the tournament
runs — metadata, imports, forbidden constructs, the `Bot` class, and all five
required methods. Exit code 0 means accepted, 1 means rejected with the reasons
printed.

A pass means the file will be *admitted*, not that it plays well.

### Before you submit

```bash
python backtester.py --bot1 strategies/my_bot.py --bot2 strategies/rational.py --isolate
```

`--isolate` runs your bot the way the tournament does. **If your score changes
with `--isolate`, that is a bug in your bot**, and it will behave the isolated
way on the day. It means you are relying on something the tournament takes
away: state carried between deals, a call over the time limit, or a
module-level cache.

---

## 15. Complete parameter reference

Every value below is an attribute of the `GameConfig` you are handed. Read them
from the config rather than hard-coding them.

### Asset and structure

| Parameter | Value | Meaning |
|---|---|---|
| `N_COINS` | 40 | Total coins |
| `N_PRIVATE` | 20 | Coins dealt to each player |
| `N_ROUNDS` | 5 | Rounds per deal |
| `REVEAL_PER_ROUND` | 4 | Coins revealed to you per round |
| `DEALS_PER_PHASE` | 10 | Default direct deals; the mirror doubles it |

### Tactical Energy

| Parameter | Value | Meaning |
|---|---|---|
| `TE_BUDGET` | 24 | TE per deal; does not carry over or replenish |
| `TE_SALVAGE` | 0.08 | PnL per point of unspent-TE advantage |

### Auction

| Parameter | Value | Meaning |
|---|---|---|
| `TIE_RULE` | `"coin_flip"` | Equal non-zero bids: coin flip, winner pays |
| `SLATE_MODE` | `"draw"` | The slate is drawn from the eligibility pool |
| `SLOTS_PER_ROUND` | 1 | Powers on the block each round |
| `POWERS_PER_ROUND` | 2 | Hard ceiling on powers per round |

### Negotiation

| Parameter | Value | Meaning |
|---|---|---|
| `N_TURNS` | 6 | Turns per negotiation: 1 open + 5 responses |
| `MIN_REDUCTION` | 1 | Minimum spread shrink per counter |
| `SPREAD_C` | 0.5 | Coefficient the engine uses to derive `final_cap` |
| `MIDPOINT_SIDE_RULE` | `"last_quoter_sells"` | On a forced fill, the last bot to quote is short |

### Fees and obligations

| Parameter | Value | Meaning |
|---|---|---|
| `FORCED_FILL_FEE` | 2.0 | Paid by the bot that countered on the final turn |
| `MAKER_OBLIGATION` | 3.0 | Strength of the opening-quote obligation |
| `WIDTH_PREMIUM` | 0.22 | Paid by the Maker per tick of opening spread above the floor |

### Powers

| Power | Magnitude | Eligible rounds | Once per deal |
|---|---|---|---|
| `FORESIGHT` | 16 | 1, 2, 3, 4, 5 | no |
| `TRICK_ROOM` | 3 | 1, 2, 3, 4, 5 | no |
| `SUBSTITUTE` | 2 | 1, 2, 3, 4, 5 | no |
| `STEALTH_ROCK` | 2 | 1, 2, 3, 4 | yes |
| `TRANSFORM` | 0 | 1, 2, 3 | yes |

`TRANSFORM`'s magnitude is 0 because its value is entirely state-dependent.

### Limits and enforcement

| Parameter | Value | Meaning |
|---|---|---|
| `TIME_BUDGET_MS` | 2.0 | Average ms per call across a match |
| `HARD_TIME_LIMIT_MS` | 50.0 | Absolute maximum for any single call |
| `MAX_TIME_VIOLATIONS` | 5 | Hard-limit overruns tolerated per match |
| `FORFEIT_PNL` | 250.0 | Transferred to the opponent on forfeit |
| `BOT_MEMORY_LIMIT_MB` | 512 | Address-space cap per bot process |
| `MATCH_TIMEOUT_S` | 600.0 | CPU-second backstop for a whole match (see §11) |
| `MAX_REASONABLE_VALUE` | 10000 | Bot outputs beyond this are clamped |
| `ZERO_SUM_TOL` | 1e-9 | Tolerance on the zero-sum assertion |

### Derived per-round values

| Round | `final_cap` | `spread_cap` |
|---|---|---|
| 1 | 4 | 9 |
| 2 | 4 | 9 |
| 3 | 3 | 8 |
| 4 | 3 | 8 |
| 5 | 2 | 7 |

`residual_sd(r)` and `unknown_to_both(r)` are also on the config; see §10.

---

**Still unsure about anything?** If a rule, a piece of game logic or a
constraint is unclear — or if the document and the engine seem to disagree —
ask the hosts in the [WhatsApp
community](https://chat.whatsapp.com/ELJQfcO8VUT3nmwhdXvhmw) rather than
guessing.

Good luck.
