# traded: monthly momentum paper-trading research bot (Alpaca, Raspberry Pi)

A small, conservative research project. It collects daily ETF bars, tests one simple long-only
monthly momentum rule, and checks how execution behaves in an Alpaca **paper** account.

* **Paper only.** `TradingClient(..., paper=True)` is hard-coded and the base URL is checked at
  start-up. Live trading is not implemented, and nothing can switch the bot to it.
* **Dry run by default.** Proposed orders go into SQLite. Nothing is sent to Alpaca until you set
  `submit_paper_orders = true` **and** review and validate the universe (see the checklist at the end).
* **No profitability claim.** This is an experiment harness. A backtest is a simulation built on
  many assumptions, and paper fills are simulated too.

---

## 1. Architecture

```
            systemd timer (weekdays 09:50 & 11:30 New York)
                              |
                    python -m momentum_bot run
                              |
   +--------------------------+---------------------------+
   | 1. data.update_bars      | 2. signals.generate_signal | 3. execution.Executor.run
   |  raw + adjusted bars     |  only if last completed    |  only on the session after
   |  idempotent upserts      |  session is a month-end    |  a month-end signal
   |  quality checks          |  frozen experiment params  |  reconcile -> sells -> buys
   +------------+-------------+-------------+--------------+-------------+
                |                           |                            |
                +-------------- SQLite (data/momentum.sqlite) -----------+
                                            |
                         reports/*.csv  (export / backtest)
```

| Module | Responsibility |
|---|---|
| `config.py` | Loads and validates TOML, hashes the frozen experiment, gates paper orders |
| `alpaca_api.py` | The only code that imports alpaca-py: paper-only client, timeouts, bounded retries, pacing |
| `calendar_utils.py` | NYSE sessions, early closes and month-ends (`exchange_calendars`, XNYS) |
| `data.py` | Bar ingestion, de-duplication, missing/stale/inconsistent checks |
| `strategy.py` | Pure signal and allocation functions, shared by backtest and live |
| `signals.py` | Persists monthly signals and enforces frozen parameters |
| `execution.py` | Rebalance state machine, reconciliation, duplicate-safe orders, recovery |
| `backtest.py` | Sequenced daily simulation with benchmarks and metrics |
| `reports.py`, `notify.py`, `logutil.py`, `universe.py`, `cli.py` | CSV export, webhook, logs with secret redaction, asset checks, commands |

### Assumptions
* Raspberry Pi OS **Bookworm 64-bit** (Python 3.11, systemd 252). Python ≥ 3.11 is required for `tomllib`.
* The paper account is **used only by this bot**. Unknown open orders or positions outside the
  universe block trading, because the bot cannot reason about state it did not create.
* The universe is US-listed ETFs that trade during regular hours. All orders are whole-share
  `market` orders with `time_in_force=day` and `extended_hours=False`.
* Account values come from Alpaca. The bot never spends more than `min(cash, non_marginable_buying_power)`,
  so margin is never used, even though paper accounts usually report a margin multiplier above 1.

### Verification status of Alpaca APIs (please read)
The network policy in the environment where this was written blocked `docs.alpaca.markets`.
The API usage was therefore checked against:
1. **The installed `alpaca-py` 0.44.0 source** (latest on PyPI at the time of writing). This covers class names, request
   fields (`StockBarsRequest.adjustment/feed`, `MarketOrderRequest.client_order_id`,
   `GetOrdersRequest`, `get_order_by_client_id`, `get_clock`, `get_account_configurations`),
   the model fields used, the paper endpoint `https://paper-api.alpaca.markets`, and the SDK's
   built-in retry on HTTP 429/504. The tests in `tests/test_alpaca_wrapper.py` drive the real SDK
   with a mocked HTTP layer to confirm request shapes.
2. **Search-result excerpts from Alpaca's documentation and forum.** These cover the adjustment options
   (`raw`/`split`/`dividend`/`all`) and the statement that SIP data older than 15 minutes can be queried on
   all plans, while recent SIP needs a paid subscription.

**Not verified. Check these yourself before relying on them:**
* The exact formula Alpaca uses for dividend adjustment (multiplicative factor versus subtracting the
  cash amount). See §5 for why this matters and how the bot limits the impact.
* Alpaca's current rate limit (assumed to be the documented default of 200 requests/min) and current plan terms.
* That Alpaca still rejects a reused `client_order_id`. The duplicate-prevention design relies on it,
  but the bot also looks orders up before any resubmission, so it does not depend on that alone.
* alpaca-py does not set HTTP timeouts. The bot adds one by wrapping the client's private `_session`.
  Re-check this after upgrading alpaca-py (`tests/test_alpaca_wrapper.py` will fail if it breaks).

---

## 2. Directory structure

```
traded/
├── README.md
├── requirements.txt          # alpaca-py, exchange_calendars (pinned)
├── requirements-dev.txt      # + pytest
├── .env.example              # copy to .env (git-ignored)
├── config/config.toml        # all editable settings, no secrets
├── momentum_bot/
│   ├── __main__.py  cli.py   config.py  alpaca_api.py  calendar_utils.py
│   ├── data.py      strategy.py  signals.py  execution.py  backtest.py
│   └── reports.py   notify.py    logutil.py  universe.py   db.py
├── tests/                    # mocked APIs only; sockets are disabled during tests
├── deploy/systemd/           # momentum-bot.service + momentum-bot.timer
├── data/                     # SQLite database (git-ignored)
├── reports/                  # CSV output (git-ignored)
└── logs/                     # rotating log file (git-ignored)
```

---

## 3. Raspberry Pi setup

```bash
sudo apt update && sudo apt install -y git python3-venv python3-dev
timedatectl status                      # "System clock synchronized: yes" is required
git clone <your repo url> ~/traded && cd ~/traded
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements-dev.txt     # numpy/pandas install from aarch64 wheels; no compiling
cp .env.example .env && chmod 600 .env
nano .env                               # paste PAPER keys from the Alpaca paper dashboard
python -m pytest -q                     # all tests use mocks; no orders, no network
```

Memory: a full run peaks at roughly 150–250 MB, mostly from importing pandas. The backtest keeps
about 10 symbols × 10 years of daily bars in plain dicts, which is a few MB.

### Commands

| Purpose | Command |
|---|---|
| Create database | `python -m momentum_bot init` |
| Show config and read-only account status | `python -m momentum_bot doctor` |
| Check universe assets (read-only) | `python -m momentum_bot validate-universe` |
| Download history / update incrementally | `python -m momentum_bot download` |
| Data-quality report | `python -m momentum_bot quality` |
| Month-end signal (latest, or a specific month-end) | `python -m momentum_bot signal [--date 2026-09-30]` |
| Backtest, development period | `python -m momentum_bot backtest --period dev` |
| Backtest, untouched evaluation period | `python -m momentum_bot backtest --period eval --confirm-evaluation-period` |
| Dry run: record proposed orders for the latest signal now | `python -m momentum_bot dry-run` |
| Execute today's scheduled rebalance (dry run unless enabled) | `python -m momentum_bot trade` |
| Daily job (download + signal + trade) | `python -m momentum_bot run` |
| State summary | `python -m momentum_bot status` |
| Export every table to CSV | `python -m momentum_bot export` |

**Paper trading** uses the same `run`/`trade` commands after you complete the checklist and set
`submit_paper_orders = true`. No separate command exists, so typing the wrong one cannot submit orders.

### systemd

```bash
# edit User= and paths first if your user is not "pi"
sudo cp deploy/systemd/momentum-bot.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now momentum-bot.timer
systemctl list-timers momentum-bot.timer
journalctl -u momentum-bot.service -n 200      # logs (also in logs/bot.log, rotated 5 × 2 MB)
sudo systemctl start momentum-bot.service      # run once by hand
```

The timer fires on weekdays at 09:50 and 11:30 **New York time** (DST-aware). Every run is
idempotent. A second run on the same day resumes an unfinished rebalance or does nothing.
On a non-session day the run downloads nothing new and exits.

**Notifications (optional):** set `NOTIFY_WEBHOOK_URL` in `.env`, for example `https://ntfy.sh/<private-topic>`.
The bot POSTs plain text for blocked, missed, completed and needs-attention rebalances and for
data failures. A failed notification never affects trading.

---

## 4. Strategy (frozen per experiment)

* **Universe:** 5–10 unleveraged ETFs from `config.toml`. The default is a *diversified example*
  (`SPY IWM EFA EEM AGG TLT GLD VNQ`), not a recommendation. Config validation rejects
  known leveraged/inverse tickers, and `validate-universe` also flags names containing
  "2X", "3X", "Ultra", "Inverse", "Bear", "Bull", "Daily", "VIX" and similar.
* **Signal date S:** the last NYSE session of each month (holidays and early closes come from XNYS).
  The signal uses the completed close of S, which counts as completed 60 minutes after the official close.
* **Lookback:** `r = adj_close(S) / adj_close(B) − 1`, where B is the last session of the month 12 months
  earlier (month-end to month-end).
* **Qualify** if `r > 0`. **Weight** = `min(10%, 50% / N)` of account equity for each of the N qualifiers.
  The rest stays in cash. Non-qualifiers are exited. If N = 0, the target is all cash.
* **Execution:** only during the **next** regular session after S, between open + 15 min and
  close − 30 min. Share counts are `floor(weight × equity / latest trade price)`, using the latest
  trade fetched at execution time. No historical price is reused for sizing.
* **Data gate:** for every universe symbol, the bars at B and S must exist, no session in the window may
  be missing, and no adjusted daily move may exceed 40%. If **any** symbol fails, the whole month is
  blocked and nothing trades. Trading on a partial universe could force exits based on missing data.
* **Freezing:** the universe, strategy and data parameters are hashed and stored the first time an
  experiment name is used. If they change and the `[experiment] name` does not, the bot refuses to run.

---

## 5. Data: storage, adjustments, feeds

### What is stored (SQLite, `data/momentum.sqlite`)
`bars` (OHLCV, symbol, session date, feed, adjustment, provider bar timestamp, provider, retrieval
time), `data_quality`, `experiments`, `universe_validations`, `signal_runs` and `signals` (lookback
dates, prices, returns, weights, strategy version), `rebalances`, `orders` (proposed and submitted,
client and broker IDs, statuses, fill quantity and average price), `fills`, `account_snapshots`,
`position_snapshots`, `open_order_snapshots` (equity, cash, exposure, pending notional), `events`
(errors and operational events), and `backtest_runs`.

* **Timestamps:** every instant is stored as ISO-8601 **UTC**. Bar dates are the **exchange-local
  session date**. Alpaca stamps daily bars at midnight New York time (04:00 or 05:00 UTC); the bot converts
  those to the New York date and rejects any bar that does not land on an XNYS session.
* **Idempotency:** the primary key `(symbol, session_date, feed, adjustment)` plus `ON CONFLICT DO UPDATE`
  means repeated downloads never create duplicate rows. Duplicates *inside* one API response are dropped
  and logged. Bars for sessions that are not yet complete are never stored.
* **Execution prices stay separate from research prices:** `adjustment='raw'` rows are used for sizing
  sanity checks (the latest trade must be within 15% of the last raw close). `adjustment='all'` rows are
  used only by the signal and the backtest.

### How adjustments work here
* `raw`: prices as traded. Downloaded incrementally. A change to an existing raw bar is
  logged as a `raw_revision` quality event.
* `all`: Alpaca's split- **and** distribution-adjusted series. When a new split or distribution occurs,
  Alpaca rescales **all earlier** bars. Stored adjusted rows therefore go stale, so every update
  **re-downloads and overwrites the last 400 calendar days** of adjusted bars. That window covers the
  12-month lookback, and every signal is computed from a single consistent snapshot. Older adjusted rows
  stay as originally retrieved (`retrieved_at_utc` shows when), so a full re-download (delete the `all`
  rows and run `download`) is the right refresh before a serious backtest.
* **Why this has no look-ahead:** with multiplicative adjustment factors, a corporate action *after* S
  scales adj_close(S) and adj_close(B) by the same factor, so their ratio, and therefore the signal,
  is unchanged. If Alpaca's dividend adjustment is subtractive rather than multiplicative (unverified,
  see §1), later distributions would shift historical ratios slightly. That bias is small for these
  ETFs but not zero.
* **Backtest:** fills and valuations both use the `all` series. Distributions are embedded in the price
  path as if reinvested and are **not** also credited as cash, so nothing is double-counted.

### IEX vs consolidated (SIP) data
* **SIP** (consolidated tape) combines trades from **all** US exchanges and off-exchange venues. It gives the
  true daily OHLCV. On Alpaca's free plan, SIP can be queried only for data **older than 15 minutes**.
  Real-time SIP needs a paid subscription (Algo Trader Plus).
* **IEX** is a single exchange, typically a low single-digit percentage of US volume. Its daily
  bars can differ from consolidated bars: different high/low, much lower volume, and the
  close is IEX's last trade, not the official closing auction. IEX real-time data is free.
* This bot defaults to `bars_feed = "sip"`, since it only requests completed sessions, safely past the
  15-minute delay, and `price_feed = "iex"` for the latest-trade sizing price at execution, since that
  needs real-time data. If your account cannot query SIP history, set `bars_feed = "iex"`, start a new
  experiment name, and expect shorter history (about 2020 onward versus about 2016) and noisier bars.
* Alpaca history depth is limited (roughly 2016+ for SIP). Long-horizon research is therefore limited.

---

## 6. Backtest

* Sequenced daily loop (see `backtest.py` docstring): execute at today's open the decision from an
  earlier month-end close → accrue cash interest → mark to close → form a new signal if today is a
  month-end. The signal function ignores data after S; tests check this.
* **Costs:** `commission_per_order`, `commission_bps`, and `slippage_bps` charged against you on every
  fill (default 5 bps; Alpaca charges no commission on US ETFs, but the setting stays configurable).
* **Benchmarks** (same execution engine, costs and monthly schedule):
  1. Static **equal weight, 100% invested** across the same universe.
  2. Static equal weight **scaled to the strategy's realised average exposure**. This deliberately
     uses hindsight; it is a comparison point only.
  3. **Cash** earning `cash_rate_annual` (default **0.0%**; set it to, e.g., a T-bill yield to compare).
     The same rate is applied to idle cash in every portfolio.
* **Metrics:** net return, annualized return (only for periods of at least 1 year), annualized volatility,
  max drawdown, turnover (total and annualized, as traded notional / average equity), average exposure,
  order count, commissions, slippage cost, and total costs.
  Written to `reports/backtest_<period>_{summary,equity,trades}.csv`. The summary CSV's first line is
  labeled *SIMULATED HISTORICAL BACKTEST*. Paper results live in separate tables and CSVs
  (`paper_orders.csv`, `paper_fills.csv`, `account_snapshots.csv`).
* **Development vs evaluation:** `dev_start..dev_end` is for exploration. `eval_start..` stays
  untouched. Viewing it requires `--confirm-evaluation-period`, and every view is logged and counted.
  Nothing optimizes parameters automatically.

### Limitations you should keep in mind
* **Survivorship and selection bias:** the universe was chosen today, from funds that exist today
  and are known to be liquid. Funds that closed or shrank are invisible. With 5–10 hand-picked ETFs,
  results mostly reflect that choice.
* Small sample: about 12 rebalances per year. Even 10 years gives about 120 decisions, so statistical power is low.
* Whole-share rounding is applied to *adjusted* prices in the backtest, which differ from the prices
  quoted at the time.
* Fills at the open ± a fixed slippage ignore spreads, auctions and market impact. Paper fills are also
  simulated and can be more optimistic than real fills.
* Taxes, borrowing costs (none, by design) and cash-sweep interest are not modeled except via `cash_rate_annual`.
* The adjusted history snapshot depends on when you downloaded it (see §5).

---

## 7. Execution and recovery

On each `trade` run, for the signal whose execution session is today:

1. **Gate:** paper mode requires `submit_paper_orders = true`, `universe.reviewed = true`, a matching
   `review_hash`, and a passing `validate-universe` record for the current universe. Otherwise the run
   is a dry run.
2. **Window:** the broker clock says the market is open, today is an XNYS session, the time is inside
   open + 15 min to close − 30 min, and the local clock is within 120 s of the broker clock.
3. **Reconcile:** existing orders for this rebalance are refreshed by `client_order_id`. A snapshot is
   stored (account, positions, open orders, exposure). Blockers: account not ACTIVE or trading-blocked,
   negative cash, any short position, positions outside the universe, open orders not created by this
   bot or belonging to another rebalance, stale raw data, or unvalidated universe.
4. **Prices:** latest trade per symbol. A symbol is skipped (no trade at all) if the trade is missing,
   older than 30 minutes, or more than 15% away from the last raw close.
5. **Sells first:** a sell is never larger than the held quantity, so the bot cannot open a short. It waits for every sell
   to reach a terminal state. Any rejected sell stops the rebalance in `needs_attention` and no buys are placed.
6. **Buys:** account and positions are re-read and the latest prices fetched again. Planned quantities
   account for pending orders. They are reduced until cost × (1 + 1% buffer) fits within
   `min(cash, non_marginable_buying_power)` minus pending buys. Each buy is then re-checked against the
   10% per-symbol and 50% total caps, counting existing positions and pending orders.
7. **Finish:** final snapshot; status `completed`, or `completed_with_issues` if there were partial fills,
   rejections or skipped symbols. The residual is **not** chased with extra orders.

**Duplicate prevention and failures**
* Client order IDs are deterministic: `mb-<experiment hash>-<YYYYMM>-<SYMBOL>-<B|S>`. The order intent is
  written to SQLite **before** the network call.
* If a submit times out or the network fails, the bot looks the order up by client ID and **never**
  resubmits blindly. Found → state synced. Not found → resubmitted once with the **same** ID
  (at most 2 attempts), which the broker would reject as a duplicate if the first had arrived.
  If the lookup itself fails, the run stops and places no further orders.
* Read-only calls are retried up to `max_retries` times with exponential backoff (capped at 30 s), and all
  calls are paced at `min_seconds_between_calls` (0.35 s ≈ 170/min). Order submission is never retried by the bot.
  alpaca-py itself retries HTTP 429/504; that is safe because of the deterministic IDs.
* **Restarts:** rebalance state (`planned → selling → buying → completed`) lives in SQLite. A rerun
  resumes from the stored state. A completed rebalance is final and is never repeated. An unfinished one
  whose session has passed becomes `needs_attention`. A signal whose execution session was missed
  becomes `missed`. The bot **never** trades late.
* A file lock prevents two bot processes from running at once.

---

## 8. Tests

`python -m pytest -q` runs 57 tests in a few seconds. Sockets are disabled for every test, the
broker and data feed are in-memory fakes, and the real alpaca-py client is exercised only with a
mocked HTTP layer.

| Area | Tests |
|---|---|
| Missing / stale / insufficient / inconsistent data | `test_strategy.py`, `test_data.py` |
| Look-ahead prevention, next-session execution | `test_future_data_is_ignored`, `test_execution_at_next_open_not_signal_close`, `test_no_trade_on_signal_date_close` |
| Allocation caps | `test_allocation_caps`, `test_paper_rebalance_sells_before_buys_and_caps` |
| Duplicate orders, timeouts | `test_timeout_after_accept_*`, `test_timeout_before_accept_*`, `test_lookup_failure_*` |
| Pending orders | `test_pending_orders_survive_restart_*`, `test_pending_buy_counts_toward_exposure` |
| Partial fills, rejections | `test_partial_fill_recorded`, `test_rejected_sell_stops_buys` |
| API failures | `test_account_api_failure_*`, `test_api_failure_during_download_*`, wrapper retry tests |
| Restart recovery | `test_restart_does_not_repeat_*`, `test_crash_between_intent_and_response_*`, `test_missed_window_*` |
| Safety gates | dry-run default, review hash, universe validation, clock skew, paper URL, foreign orders/positions |

---

## 9. Not implemented / unfinished (clearly labeled)

* **Not implemented on purpose:** live trading, fractional shares, limit orders, extended hours,
  shorting, margin, options, and leveraged/inverse products.
* **Residual chasing:** after partial fills or a skipped symbol, the bot does not place follow-up orders.
  The gap stays until next month's rebalance.
* **Late execution:** if the Pi is down on the execution session, that month is `missed`. Raising
  `max_execution_delay_sessions` allows later sessions, but that path has had less testing.
* **Blocked-signal retry** is only possible before the execution session opens, via `signal --date`.
* **Dividend cash in the paper account** is not reconciled against the research series. Equity
  snapshots capture it indirectly.
* **Corporate-action events** (splits on the execution day) are only guarded by the 15% price-deviation
  check. No corporate-actions API is used.
* **Universe review** checks are heuristic (denylist plus name keywords). They do not replace reading
  each fund's prospectus.
* **Backtest** has no intraday data, no spread model and no tax model. Adjusted-price share rounding is approximate.

---

## 10. Checklist before setting `submit_paper_orders = true`

- [ ] Run `python -m pytest -q`: all tests pass on the Pi.
- [ ] Confirm `.env` holds **paper** keys only, `chmod 600 .env`, and `git status` does not show `.env`.
- [ ] Run `python -m momentum_bot doctor`: account status ACTIVE and clock synced. Consider disabling
      shorting and setting the margin multiplier to 1 in the paper dashboard as an extra layer.
- [ ] Ensure the paper account is dedicated to this bot: no manual positions or open orders
      (reset the paper account if needed).
- [ ] Review every universe symbol yourself: unleveraged, not inverse, liquid, US-listed, and what it holds.
- [ ] Run `python -m momentum_bot validate-universe`. It must pass. Then set `reviewed = true` and paste the printed `review_hash`.
- [ ] Set a fresh `[experiment] name` for this configuration and don't change parameters mid-experiment.
- [ ] Run `download` and then `quality`: no missing or stale data. Check a few closes against another source.
- [ ] Decide `bars_feed` (SIP vs IEX) consciously, and confirm your plan can access it.
- [ ] Run `backtest --period dev` and read the assumptions and limitations. Do **not** tune on the eval period.
- [ ] Let at least one month-end pass in dry-run mode under systemd. Inspect `dry_run_orders.csv`,
      `signals.csv` and the logs, and check the proposed quantities by hand against the caps.
- [ ] Enable and test notifications (`NOTIFY_WEBHOOK_URL`), and know how to read `journalctl -u momentum-bot`.
- [ ] Know how to stop it: `sudo systemctl disable --now momentum-bot.timer`, then set `submit_paper_orders = false`.
- [ ] Only then set `submit_paper_orders = true`. After the first paper rebalance, compare `paper_orders.csv`
      and `paper_fills.csv` with the Alpaca dashboard.
