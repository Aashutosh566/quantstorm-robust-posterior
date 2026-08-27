"""
backtester.py — Divided Oracle: Production Backtester
======================================================

Duel two strategies with full step-by-step game visualization,
timing analysis, and constraint enforcement.

USAGE (modify the configuration section at the top of this file):
    python backtester.py

Or from command line with overrides:
    python backtester.py --bot1 strategies/rational.py --bot2 strategies/naive_ev.py
    python backtester.py --n_deals 5 --seed 123 --mirror
    python backtester.py --coins "+1,-1,+1,-1,..." --first_maker 0

FEATURES:
  * Step-by-step game log (every round, auction, negotiation turn)
  * Per-call timing with budget warnings
  * Full constraint enforcement (bids, quotes, counters validated)
  * Custom coin arrangements and first-maker override
  * Reproducible via seed
  * Summary statistics (mean PnL, std dev, win rate, time analysis)
  * Warning report for illegal bot actions (clamped, not crashed)
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import statistics
import sys
import time
from pathlib import Path

# Force UTF-8 encoding for stdout on Windows
if sys.stdout and getattr(sys.stdout, 'encoding', '').lower() != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

# Add sim_code directory to path
SIM_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SIM_DIR))

from engine import play_match
from game_config import GameConfig


# ════════════════════════════════════════════════════════════════════
#  CONFIGURATION — Modify these to set up your duel
# ════════════════════════════════════════════════════════════════════

# Paths to bot strategy files (relative to this file)
BOT1_PATH = "strategies/rational.py"
BOT2_PATH = "strategies/naive_ev.py"

# Match settings
N_DEALS = 10        # Deals per phase (mirror doubles to 2x)
USE_MIRROR = True   # Play mirrored deals (recommended for fairness)
SEED = 42           # Random seed for reproducibility (None = random)

# Optional: fixed coin vector (None = random per deal)
# Must be exactly 40 values, each +1 or -1
# Example: FIXED_COINS = [1,-1,1,1,-1,-1,1,-1,...] (40 values)
FIXED_COINS = None

# Optional: override who is Maker in round 1 (None = default, 0 or 1)
FIRST_MAKER = None

# Verbosity
VERBOSE = True      # Show step-by-step game log


# ════════════════════════════════════════════════════════════════════
#  BOT LOADER
# ════════════════════════════════════════════════════════════════════

from bot_loader import load_for_testing as load_bot_class


def validate_coins(coins_input: str | list | None, config: GameConfig) -> list[int] | None:
    """Parse and validate a custom coin vector.

    Accepts:
      - None (random coins)
      - List of ints (each +1 or -1)
      - Comma-separated string like "+1,-1,+1,-1,..."
    """
    if coins_input is None:
        return None

    if isinstance(coins_input, str):
        try:
            coins = [int(x.strip()) for x in coins_input.split(",")]
        except ValueError:
            raise ValueError(
                f"Invalid coin string. Use comma-separated +1/-1 values.\n"
                f"Example: '+1,-1,+1,-1,...' ({config.N_COINS} values)"
            )
    else:
        coins = list(coins_input)

    if len(coins) != config.N_COINS:
        raise ValueError(
            f"Coin vector must have exactly {config.N_COINS} values, "
            f"got {len(coins)}"
        )
    for i, c in enumerate(coins):
        if c not in (-1, 1):
            raise ValueError(
                f"Coin {i} is {c}, must be +1 or -1"
            )

    return coins


# ════════════════════════════════════════════════════════════════════
#  SUMMARY REPORT
# ════════════════════════════════════════════════════════════════════

def print_summary(result, bot_a_name: str, bot_b_name: str, elapsed_s: float,
                  mirror: bool = True):
    """Print a comprehensive match summary."""
    n = len(result.deals)
    pnl_a = [d.pnl[0] for d in result.deals]
    pnl_b = [d.pnl[1] for d in result.deals]

    wins_a = sum(1 for p in pnl_a if p > 0)
    wins_b = sum(1 for p in pnl_b if p > 0)
    draws = sum(1 for p in pnl_a if p == 0)

    print(f"\n{'-' * 62}")
    print(f"  MATCH SUMMARY")
    print(f"{'-' * 62}")
    print(f"  Deals played: {n}")
    print(f"  Total time:   {elapsed_s:.2f}s")
    print()

    # PnL summary
    print(f"  {'':20s} {'Aggregate':>12s} {'Mean/deal':>12s} {'Std Dev':>12s}")
    print(f"  {'-' * 56}")
    mean_a = statistics.mean(pnl_a) if pnl_a else 0
    mean_b = statistics.mean(pnl_b) if pnl_b else 0
    std_a = statistics.stdev(pnl_a) if len(pnl_a) > 1 else 0
    std_b = statistics.stdev(pnl_b) if len(pnl_b) > 1 else 0
    print(f"  {bot_a_name:20s} {result.pnl[0]:+12.2f} {mean_a:+12.2f} {std_a:12.2f}")
    print(f"  {bot_b_name:20s} {result.pnl[1]:+12.2f} {mean_b:+12.2f} {std_b:12.2f}")
    print()

    # Win rate
    print(f"  Record: {bot_a_name} {wins_a}W / {draws}D / {wins_b}L  "
          f"(win rate {wins_a / n * 100:.0f}%)" if n else "")
    print()

    # Timing analysis
    print(f"  TIMING ANALYSIS")
    print(f"  {'-' * 56}")
    for name, times in [(bot_a_name, result.bot_a_times),
                        (bot_b_name, result.bot_b_times)]:
        if not times:
            continue
        avg = statistics.mean(times)
        mx = max(times)
        total = sum(times)
        print(f"  {name:20s} avg={avg:.3f}ms  max={mx:.3f}ms  "
              f"total={total:.1f}ms  calls={len(times)}")
        if avg > 2.0:
            print(f"  !!!  {name} EXCEEDS time budget (2ms avg)!")
    print()

    # Warnings
    all_warnings = result.bot_a_warnings + result.bot_b_warnings
    if all_warnings:
        print(f"  !!!  WARNINGS ({len(all_warnings)} total)")
        print(f"  {'-' * 56}")
        # Show first 20, then count remaining
        for w in all_warnings[:20]:
            print(f"    • {w}")
        if len(all_warnings) > 20:
            print(f"    ... and {len(all_warnings) - 20} more")
    else:
        print(f"  * No warnings — both bots played legal at all times.")

    print(f"\n{'-' * 62}")

    # Per-deal breakdown
    print(f"\n  PER-DEAL BREAKDOWN")
    print(f"  {'Deal':>5s}  {'Type':>7s}  {bot_a_name:>12s}  {bot_b_name:>12s}  {'Winner':>12s}")
    print(f"  {'-' * 56}")
    for i, d in enumerate(result.deals):
        # The engine plays each deal's mirror immediately after it, so the
        # legs interleave: direct, mirror, direct, mirror. This used to
        # label the first half "Direct" and the second half "Mirror", which
        # mislabelled every row from the second deal onwards -- visible in
        # self-play, where deals 1 and 2 come out equal and opposite yet
        # both printed as "Direct".
        dtype = "Mirror" if (mirror and i % 2) else "Direct"
        winner = bot_a_name if d.pnl[0] > 0 else (bot_b_name if d.pnl[1] > 0 else "Draw")
        print(f"  {i + 1:5d}  {dtype:>7s}  {d.pnl[0]:+12.2f}  {d.pnl[1]:+12.2f}  {winner:>12s}")

    print()


# ════════════════════════════════════════════════════════════════════
#  MAIN
# ════════════════════════════════════════════════════════════════════

def parse_args():
    """Parse command-line arguments (override the config section above)."""
    parser = argparse.ArgumentParser(
        description="Divided Oracle Backtester — duel two strategies",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python backtester.py\n"
            "  python backtester.py --bot1 strategies/rational.py "
            "--bot2 strategies/naive_ev.py\n"
            "  python backtester.py --n_deals 5 --seed 123 --mirror\n"
            "  python backtester.py --coins '+1,-1,+1,-1,...'\n"
        ),
    )
    parser.add_argument("--bot1", type=str, default=None,
                        help="Path to bot 1 strategy file")
    parser.add_argument("--bot2", type=str, default=None,
                        help="Path to bot 2 strategy file")
    parser.add_argument("--n_deals", type=int, default=None,
                        help="Deals per phase (default from config)")
    parser.add_argument("--seed", type=int, default=None,
                        help="Random seed (None = use config default)")
    parser.add_argument("--mirror", action="store_true", default=None,
                        help="Use mirrored deals")
    parser.add_argument("--no_mirror", action="store_true", default=False,
                        help="Disable mirrored deals")
    parser.add_argument("--coins", type=str, default=None,
                        help="Fixed coin vector: '+1,-1,+1,...' (40 values)")
    parser.add_argument("--first_maker", type=int, choices=[0, 1], default=None,
                        help="Override who is Maker in round 1")
    parser.add_argument("--quiet", action="store_true", default=False,
                        help="Suppress step-by-step logs")
    parser.add_argument("--isolate", action="store_true", default=False,
                        help="Run both bots exactly as the tournament does: "
                             "separate processes, enforced per-call time "
                             "limit, module reloaded between deals")
    parser.add_argument("--validate", type=str, default=None, metavar="FILE",
                        help="Check one submission file would be accepted, "
                             "then exit. Nothing is executed.")
    return parser.parse_args()


def validate_one(filepath: str) -> int:
    """Run the submission gate over one file and report. Returns exit code.

    Nothing in the file is executed -- this is the same static check the
    tournament runs before it will touch a submission, so a pass here means
    the file will be accepted, not that it plays well.
    """
    from bot_loader import check_source

    result = check_source(filepath)
    print(f"\n{'-' * 62}")
    print(f"  SUBMISSION CHECK: {filepath}")
    print(f"{'-' * 62}")

    meta = result.metadata or {}
    for field in ("Name", "College", "Roll Number"):
        value = meta.get(field)
        print(f"  {field + ':':<14s}{value if value else '(missing)'}")

    for warning in result.warnings:
        print(f"  ! {warning}")

    if result.ok:
        print("\n  ACCEPTED — this file would be admitted to the tournament.")
        print(f"{'-' * 62}\n")
        return 0

    print(f"\n  REJECTED — {len(result.errors)} problem"
          f"{'s' if len(result.errors) != 1 else ''}:")
    for kind, error in zip(result.kinds, result.errors):
        print(f"    [{kind}] {error}")
    print(f"{'-' * 62}\n")
    return 1


def main():
    args = parse_args()

    if args.validate:
        sys.exit(validate_one(args.validate))

    # Resolve settings: CLI args override config-section defaults
    bot1_path = args.bot1 or BOT1_PATH
    bot2_path = args.bot2 or BOT2_PATH
    n_deals = args.n_deals or N_DEALS
    seed = args.seed if args.seed is not None else SEED
    if args.no_mirror:
        mirror = False
    elif args.mirror:
        mirror = True
    else:
        mirror = USE_MIRROR
    coins_input = args.coins or FIXED_COINS
    first_maker = args.first_maker if args.first_maker is not None else FIRST_MAKER
    verbose = VERBOSE and not args.quiet

    # Load config
    config = GameConfig()

    # Load bots
    print(f"\n{'-' * 62}")
    print(f"  DIVIDED ORACLE BACKTESTER")
    print(f"{'-' * 62}")

    try:
        BotA = load_bot_class(bot1_path)
        bot_a_name = getattr(BotA, "name", Path(bot1_path).stem)
        print(f"  Bot 1: {bot_a_name} ({bot1_path})")
    except Exception as e:
        print(f"  X Failed to load Bot 1 ({bot1_path}): {e}")
        sys.exit(1)

    try:
        BotB = load_bot_class(bot2_path)
        bot_b_name = getattr(BotB, "name", Path(bot2_path).stem)
        print(f"  Bot 2: {bot_b_name} ({bot2_path})")
    except Exception as e:
        print(f"  X Failed to load Bot 2 ({bot2_path}): {e}")
        sys.exit(1)

    # Validate coins
    fixed_coins = validate_coins(coins_input, config)

    # Print settings
    total_deals = n_deals * (2 if mirror else 1)
    print(f"  Deals: {total_deals} ({n_deals} direct"
          f"{' + ' + str(n_deals) + ' mirror' if mirror else ''})")
    print(f"  Seed:  {seed}")
    if fixed_coins:
        print(f"  Coins: FIXED ({sum(fixed_coins):+d} sum)")
    if first_maker is not None:
        print(f"  First Maker: Bot {'A' if first_maker == 0 else 'B'} "
              f"({bot_a_name if first_maker == 0 else bot_b_name})")
    print(f"{'-' * 62}")

    # Run the match. --isolate swaps the in-process classes for sandboxed
    # worker processes; everything downstream is identical, which is the
    # point -- if your bot behaves differently under --isolate, it is
    # relying on something the tournament will take away (cross-deal
    # memory, an unbounded call, a module-level cache).
    sandboxes = []
    if args.isolate:
        from sandbox import SandboxedBot
        print("  Isolation: ON (tournament conditions)")
        sandboxes = [
            SandboxedBot(bot1_path, bot_a_name, config),
            SandboxedBot(bot2_path, bot_b_name, config),
        ]
        BotA, BotB = sandboxes[0].new_deal, sandboxes[1].new_deal

    start_time = time.perf_counter()

    result = play_match(
        bot_a_factory=BotA,
        bot_b_factory=BotB,
        config=config,
        seed=seed if seed is not None else int(time.time()),
        mirror=mirror,
        n_deals=n_deals,
        verbose=verbose,
        bot_a_name=bot_a_name,
        bot_b_name=bot_b_name,
        fixed_coins=fixed_coins,
        first_maker=first_maker,
    )

    elapsed = time.perf_counter() - start_time
    for box in sandboxes:
        for problem in box.load_errors:
            print(f"  !!!  {box.name} could not be loaded: {problem}")
        for warning in box.limit_warnings:
            print(f"  !!!  {warning}")
        box.close()

    # Print summary
    print_summary(result, bot_a_name, bot_b_name, elapsed, mirror)


if __name__ == "__main__":
    main()
