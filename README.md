# QuantStorm_RobustPosterior

## 🎯 Overview

A competitive trading bot for the **QuantStorm 2026 Divided Oracle** competition. This bot was developed over a 36-hour challenge and successfully beat all 10 benchmark bots.

| Metric | Value |
|--------|-------|
| **Final Rank** | 137  |
| **Best Score** | +77 |
| **Key Achievement** | Beat all 10 benchmark bots |
| **Duration** | 36 hours (24 + 12 extension) |

---

## 📂 Repository Contents

| File | Description |
|------|-------------|
|`RULEBOOK.md`| All rules are writtten in detail|
| `RobustPosterior.py` | My trading bot |
| `backtester.py` | Official backtester to duel bots |
| `rational`+`naive_ev`+`adaptive_bidder` | Baseline strategies to test against |
| `RULEBOOK.md` | Official game rules |
| `STRATEGY.md` | My strategy documentation |
| `MATHEMATICS.md` | Mathematical foundations |

---

## 🚀 Quick Start

```bash
# Run my bot against a baseline
python backtester.py --bot1 RobustPosterior.py --bot2 rational.py

# Test under tournament conditions
python backtester.py --bot1 quantstorm_robust_posterior.py --bot2 rational.py --isolate

# Validate submission
python backtester.py --validate quantstorm_robust_posterior.py



---

### ✨ What I added:
- A **"Challenge: Try to Beat My Bot!"** section
- A command for people to run against your bot
- A friendly invite to share strategies
- It makes your repo **engaging and interactive** for visitors

---
### If you beat my bot, let me know! I'd love to see your strategy.
---