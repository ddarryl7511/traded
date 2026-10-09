"""Configuration loading, validation and experiment hashing."""
from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

STRATEGY_VERSION = "monthly-momentum-12m-longonly-v1"

# Known leveraged / inverse / volatility products. Not exhaustive: the
# validate-universe command also checks asset names returned by Alpaca.
LEVERAGED_OR_INVERSE = {
    "TQQQ", "SQQQ", "QLD", "QID", "PSQ", "SPXL", "SPXS", "UPRO", "SPXU", "SSO", "SDS", "SH",
    "SPDN", "TNA", "TZA", "URTY", "SRTY", "UWM", "TWM", "RWM", "DDM", "DXD", "UDOW", "SDOW",
    "DOG", "SOXL", "SOXS", "TECL", "TECS", "FAS", "FAZ", "LABU", "LABD", "NUGT", "DUST",
    "JNUG", "JDST", "UCO", "SCO", "BOIL", "KOLD", "TMF", "TMV", "TBT", "UBT", "TBF", "TTT",
    "YINN", "YANG", "EDC", "EDZ", "UVXY", "SVXY", "VXX", "VIXY", "UVIX", "SVIX", "TSLL",
    "NVDL", "NVDU", "FNGU", "FNGD", "BULZ", "BERZ", "WEBL", "WEBS", "CURE", "DRN", "DRV",
    "ERX", "ERY", "GUSH", "DRIP", "AGQ", "ZSL", "UGL", "GLL", "EUO", "ULE", "YCS", "YCL",
}
# Name fragments that suggest leveraged/inverse/volatility products. ("SHORT" is deliberately
# omitted because it would flag ordinary short-duration bond funds; review names yourself.)
NAME_RED_FLAGS = ("1.5X", "2X", "3X", "-1X", "-2X", "-3X", "ULTRA", "INVERSE", "LEVERAGED", "BEAR",
                  "BULL", "DAILY", "VIX", "VOLATILITY")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    experiment_name: str
    symbols: tuple[str, ...]
    universe_reviewed: bool
    universe_review_hash: str
    lookback_months: int
    max_weight_per_symbol: float
    max_total_exposure: float
    bars_feed: str
    price_feed: str
    history_start: date
    adjusted_refresh_days: int
    completed_bar_delay_minutes: int
    max_missing_sessions_in_lookback: int
    max_abs_daily_return: float
    submit_paper_orders: bool
    start_delay_minutes: int
    stop_before_close_minutes: int
    max_execution_delay_sessions: int
    cash_buffer_pct: float
    max_price_deviation: float
    poll_interval_seconds: float
    phase_timeout_minutes: float
    http_timeout_seconds: float
    min_seconds_between_calls: float
    max_retries: int
    dev_start: date
    dev_end: date
    eval_start: date
    eval_end: date | None
    initial_capital: float
    commission_per_order: float
    commission_bps: float
    slippage_bps: float
    cash_rate_annual: float
    database: Path
    reports_dir: Path
    log_file: Path
    max_drawdown: float = 0.08
    max_daily_loss: float = 0.02
    raw: dict = field(default_factory=dict, compare=False, repr=False)

    # ---- derived values -------------------------------------------------
    def frozen_params(self) -> dict:
        """Everything that defines the experiment. Hashed and stored on first use."""
        return {
            "strategy_version": STRATEGY_VERSION,
            "symbols": sorted(self.symbols),
            "lookback_months": self.lookback_months,
            "max_weight_per_symbol": self.max_weight_per_symbol,
            "max_total_exposure": self.max_total_exposure,
            "bars_feed": self.bars_feed,
            "signal_adjustment": "all",
            "max_missing_sessions_in_lookback": self.max_missing_sessions_in_lookback,
            "max_abs_daily_return": self.max_abs_daily_return,
        }

    def experiment_hash(self) -> str:
        blob = json.dumps(self.frozen_params(), sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()

    def to_json(self) -> str:
        d = asdict(self)
        d.pop("raw", None)
        return json.dumps(d, default=str, sort_keys=True)


def universe_hash(symbols) -> str:
    return hashlib.sha256(",".join(sorted(s.upper() for s in symbols)).encode()).hexdigest()[:16]


def _date(v, name) -> date:
    try:
        return date.fromisoformat(str(v))
    except ValueError as exc:
        raise ConfigError(f"{name} must be YYYY-MM-DD, got {v!r}") from exc


def load_config(path: str | Path) -> Config:
    path = Path(path).resolve()
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    root = path.parent.parent if path.parent.name == "config" else path.parent

    def sect(name):
        if name not in raw:
            raise ConfigError(f"missing [{name}] section")
        return raw[name]

    e, u, s, d, t, b, p = (sect(n) for n in
                           ("experiment", "universe", "strategy", "data", "trading", "backtest", "paths"))

    def rel(v):
        q = Path(v)
        return q if q.is_absolute() else root / q

    cfg = Config(
        experiment_name=str(e["name"]),
        symbols=tuple(str(x).upper().strip() for x in u["symbols"]),
        universe_reviewed=bool(u.get("reviewed", False)),
        universe_review_hash=str(u.get("review_hash", "")),
        lookback_months=int(s["lookback_months"]),
        max_weight_per_symbol=float(s["max_weight_per_symbol"]),
        max_total_exposure=float(s["max_total_exposure"]),
        bars_feed=str(d["bars_feed"]).lower(),
        price_feed=str(d["price_feed"]).lower(),
        history_start=_date(d["history_start"], "history_start"),
        adjusted_refresh_days=int(d["adjusted_refresh_days"]),
        completed_bar_delay_minutes=int(d["completed_bar_delay_minutes"]),
        max_missing_sessions_in_lookback=int(d["max_missing_sessions_in_lookback"]),
        max_abs_daily_return=float(d["max_abs_daily_return"]),
        submit_paper_orders=t.get("submit_paper_orders", False) is True,
        start_delay_minutes=int(t["start_delay_minutes"]),
        stop_before_close_minutes=int(t["stop_before_close_minutes"]),
        max_execution_delay_sessions=int(t["max_execution_delay_sessions"]),
        cash_buffer_pct=float(t["cash_buffer_pct"]),
        max_price_deviation=float(t["max_price_deviation"]),
        poll_interval_seconds=float(t["poll_interval_seconds"]),
        phase_timeout_minutes=float(t["phase_timeout_minutes"]),
        http_timeout_seconds=float(t["http_timeout_seconds"]),
        min_seconds_between_calls=float(t["min_seconds_between_calls"]),
        max_retries=int(t["max_retries"]),
        dev_start=_date(b["dev_start"], "dev_start"),
        dev_end=_date(b["dev_end"], "dev_end"),
        eval_start=_date(b["eval_start"], "eval_start"),
        eval_end=_date(b["eval_end"], "eval_end") if b.get("eval_end") else None,
        initial_capital=float(b["initial_capital"]),
        commission_per_order=float(b["commission_per_order"]),
        commission_bps=float(b["commission_bps"]),
        slippage_bps=float(b["slippage_bps"]),
        cash_rate_annual=float(b["cash_rate_annual"]),
        database=rel(p["database"]),
        reports_dir=rel(p["reports_dir"]),
        log_file=rel(p["log_file"]),
        max_drawdown=float(raw.get("risk", {}).get("max_drawdown", 0.08)),
        max_daily_loss=float(raw.get("risk", {}).get("max_daily_loss", 0.02)),
        raw=raw,
    )
    validate(cfg)
    return cfg


def validate(cfg: Config) -> None:
    n = len(cfg.symbols)
    if not 5 <= n <= 10:
        raise ConfigError(f"universe must contain 5-10 symbols, has {n}")
    if len(set(cfg.symbols)) != n:
        raise ConfigError("universe contains duplicate symbols")
    bad = sorted(set(cfg.symbols) & LEVERAGED_OR_INVERSE)
    if bad:
        raise ConfigError(f"leveraged/inverse products are not allowed: {bad}")
    if not 0 < cfg.max_weight_per_symbol <= 0.10:
        raise ConfigError("max_weight_per_symbol must be in (0, 0.10]")
    if not 0 < cfg.max_total_exposure <= 0.50:
        raise ConfigError("max_total_exposure must be in (0, 0.50]")
    if cfg.lookback_months < 1:
        raise ConfigError("lookback_months must be >= 1")
    for feed_name in ("bars_feed", "price_feed"):
        if getattr(cfg, feed_name) not in ("sip", "iex"):
            raise ConfigError(f"{feed_name} must be 'sip' or 'iex'")
    if not cfg.dev_start < cfg.dev_end < cfg.eval_start:
        raise ConfigError("backtest periods must satisfy dev_start < dev_end < eval_start")
    if cfg.eval_end and cfg.eval_end <= cfg.eval_start:
        raise ConfigError("eval_end must be after eval_start")
    if cfg.max_retries < 0 or cfg.max_retries > 10:
        raise ConfigError("max_retries must be between 0 and 10")
    if not 0 < cfg.max_drawdown < 1 or not 0 < cfg.max_daily_loss < 1:
        raise ConfigError("[risk] max_drawdown and max_daily_loss must be fractions in (0, 1)")
    if cfg.max_execution_delay_sessions < 0:
        raise ConfigError("max_execution_delay_sessions must be >= 0")


def paper_orders_allowed(cfg: Config) -> tuple[bool, str]:
    """Static (config-only) gate for paper order submission."""
    if not cfg.submit_paper_orders:
        return False, "trading.submit_paper_orders is false (dry-run mode)"
    if not cfg.universe_reviewed:
        return False, "universe.reviewed is false"
    if cfg.universe_review_hash != universe_hash(cfg.symbols):
        return False, "universe.review_hash does not match the current symbol list"
    return True, "ok"
