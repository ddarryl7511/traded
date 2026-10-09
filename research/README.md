# Research loop

```
research  ->  predict  ->  backtest (dev)  ->  backtest (eval, once)  ->  paper  ->  review  ->  ledger
   ^                                                                                              |
   +--------------------------------- next idea reads the ledger first ---------------------------+
```

What is enforced in code (not by these prompts):

| Rule | Where |
|---|---|
| No backtest without a pre-registered prediction | `backtest` -> `ledger.require` |
| Prediction can't be edited or deleted | SQLite triggers on `hypotheses` |
| Max 5 variations per idea | `predict` |
| Every config ever backtested counts as a trial (Deflated Sharpe) | `ledger.trials` |
| 2x costs + 1-session-late fills must still have Sharpe > 0 | `backtest` verdict |
| Eval period: only after dev PASS, once per experiment | `backtest --period eval` |
| Kill switch: drawdown / daily loss / manual `halt`, latched until `resume` | `execution.kill_switch` |

## 1. Research agents (Claude Code or the homelab `research-plan` agent)

Run each role **separately** (do not let them read each other's memos). Paste into a fresh session
in `~/traded` (or on the Pi):

```
You are the [Price Analyst | Macro Watcher | Skeptic]. Look only at [price/volume trends |
rates, inflation and sector rotation | reasons ideas fail] for the US ETF universe in
config/config.toml. The bot rebalances monthly, long-only, max 10%/ETF, max 50% invested.
First run `python -m momentum_bot ledger --json` and do not re-propose anything with status
failed/retired unless you can say what changed in market conditions. Propose at most 3 testable
ideas. For each: the idea in one sentence, why it might work, the market conditions it needs,
and what result would prove it wrong. Save to research/<role>-<YYYY-MM-DD>.md.
Do not read other memos in research/.
```

The Skeptic gets the other memos *after* they are written and argues why each will fail.

## 2. Pre-register (before any backtest)

Pick one idea, implement it as a new strategy version, give it a fresh `[experiment] name`, then copy
`mom12-v1-2026.toml` to `<experiment>.toml`, fill it in **before looking at any result**, and run:

```bash
python -m momentum_bot predict research/<experiment>.toml
python -m momentum_bot backtest --period dev        # prints stress, worst months, walk-forward, DSR, VERDICT
```

FAIL -> optionally ONE change (new experiment name, new prediction) and retest. The cap is 5 per idea.
PASS -> `backtest --period eval --confirm-evaluation-period` (one shot) -> paper.

## 3. Review (weekly timer, or by hand)

```bash
python -m momentum_bot review --send
python -m momentum_bot ledger --set-status paused --lessons "lagged backtest by 3% in choppy market"
```

Critic prompt for an AI pass over the numbers:

```
You are the Critic. Read journal/review-*.md from the past month and `python -m momentum_bot
ledger --json`. Compare paper results to the same-window backtest and the original prediction.
Say which strategies are worse live and by how much, what the losing periods had in common,
any repeated mistake, and whether anything should be paused. Be blunt. Record conclusions with
`python -m momentum_bot ledger --experiment <name> --lessons "..."` (and --set-status if needed).
```
