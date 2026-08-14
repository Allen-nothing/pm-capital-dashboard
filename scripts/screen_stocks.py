#!/usr/bin/env python3
"""
Standalone auto-screener: pulls the CURRENT S&P 500 constituent list
(auto-updated, no manual maintenance needed) plus any extra tickers you
list in config/universe.json, scans them against 4 filters, and writes the
symbols that pass ALL of them into config/watchlist.json - which
generate_dashboard.py then uses as usual.

Filters (all must pass):
  1. Price > SMA200            (uptrend filter)
  2. Market Cap > 2B USD       (size/liquidity filter)
  3. Beta (1yr) > 1             (volatility filter)
  4. $Volume (1mo avg) > 900M   (liquidity filter: avg daily price*volume over ~21 trading days)

S&P 500 constituent source (free, no API key, auto-updated from Wikipedia):
  https://raw.githubusercontent.com/datasets/s-and-p-500-companies/master/data/constituents.csv

Data sources:
  - S&P 500 constituent list -> GitHub dataset above (free, auto-updated)
  - Market Cap + Beta        -> Finnhub (/stock/profile2, /stock/metric) - free tier
  - Price history + volume   -> Twelve Data (/time_series) - free tier, needed
    because Finnhub's free tier doesn't serve historical daily volume for
    US equities.

Performance: most of the ~500 S&P names fail the Market Cap / Beta filter
using only fast Finnhub calls, so Twelve Data (the slow, rate-limited part -
8s between calls on the free tier) is only queried for names that already
passed those two checks. A full run typically finishes in ~25-35 minutes
rather than the ~67 minutes a naive "query everything" approach would take.
Set screener.max_candidates in config/settings.json if you want to cap the
universe size for a faster/cheaper run.

This is a pure quantitative screen - it does NOT look at news, valuation
quality, or fundamentals beyond market cap. Treat the output as a candidate
list to review, not a buy list. Existing manual 'theme' tags for symbols
already in your watchlist are preserved; newly-added symbols get their
Finnhub sector/industry as the theme.

Usage:
    export FINNHUB_API_KEY=your_key
    export TWELVEDATA_API_KEY=your_key
    python scripts/screen_stocks.py
"""

import csv
import io
import json
import os
import random
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"

FINNHUB_TOKEN = os.environ.get("FINNHUB_API_KEY")
TD_TOKEN = os.environ.get("TWELVEDATA_API_KEY")
FINNHUB_BASE = "https://finnhub.io/api/v1"
TD_BASE = "https://api.twelvedata.com/time_series"
SP500_CSV_URL = (
    "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/"
    "master/data/constituents.csv"
)

if not FINNHUB_TOKEN:
    print("ERROR: FINNHUB_API_KEY not set.", file=sys.stderr)
    sys.exit(1)
if not TD_TOKEN:
    print("ERROR: TWELVEDATA_API_KEY not set (needed for price history + volume).", file=sys.stderr)
    sys.exit(1)

# --- filter thresholds (edit here if you want to tune them) ---
MIN_MARKET_CAP_USD = 2_000_000_000
MIN_BETA = 1.0
MIN_DOLLAR_VOLUME_1MO = 900_000_000
SMA_PERIOD = 200

# --- rate-limit throttling (time-based, so only calls that actually happen incur delay) ---
FINNHUB_MIN_INTERVAL = 1.1   # ~55/min, safely under free tier's 60/min with 2 calls/symbol
TD_MIN_INTERVAL = 8.0        # free tier is tightly rate-limited
_last_call = {"finnhub": 0.0, "td": 0.0}


def _throttle(api):
    elapsed = time.time() - _last_call[api]
    wait = (FINNHUB_MIN_INTERVAL if api == "finnhub" else TD_MIN_INTERVAL) - elapsed
    if wait > 0:
        time.sleep(wait)
    _last_call[api] = time.time()


def load_json(path, default=None):
    if not path.exists():
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def fetch_sp500_symbols():
    """Free, no-API-key S&P 500 constituent list, auto-updated from Wikipedia."""
    r = requests.get(SP500_CSV_URL, timeout=20)
    r.raise_for_status()
    reader = csv.DictReader(io.StringIO(r.text))
    return [row["Symbol"].strip() for row in reader if row.get("Symbol", "").strip()]


settings = load_json(CONFIG_DIR / "settings.json", default={})


def get_market_cap_and_sector(symbol, retries=2):
    for attempt in range(retries):
        try:
            _throttle("finnhub")
            r = requests.get(
                f"{FINNHUB_BASE}/stock/profile2",
                params={"symbol": symbol, "token": FINNHUB_TOKEN},
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()
            cap_million = data.get("marketCapitalization")
            sector = data.get("finnhubIndustry")
            if cap_million is None:
                return None, None
            return cap_million * 1_000_000, sector
        except Exception as e:
            print(f"  [warn] profile2 failed for {symbol} (attempt {attempt+1}): {e}")
            time.sleep(1)
    return None, None


def get_beta(symbol, retries=2):
    for attempt in range(retries):
        try:
            _throttle("finnhub")
            r = requests.get(
                f"{FINNHUB_BASE}/stock/metric",
                params={"symbol": symbol, "metric": "all", "token": FINNHUB_TOKEN},
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()
            beta = (data.get("metric") or {}).get("beta")
            return beta
        except Exception as e:
            print(f"  [warn] metric failed for {symbol} (attempt {attempt+1}): {e}")
            time.sleep(1)
    return None


def get_price_history_and_volume(symbol, outputsize=210, retries=2):
    for attempt in range(retries):
        try:
            _throttle("td")
            r = requests.get(
                TD_BASE,
                params={
                    "symbol": symbol,
                    "interval": "1day",
                    "outputsize": outputsize,
                    "apikey": TD_TOKEN,
                },
                timeout=15,
            )
            r.raise_for_status()
            data = r.json()
            if data.get("status") == "error":
                print(f"  [warn] time_series error for {symbol}: {data.get('message')}")
                return None
            values = data.get("values")
            if not values:
                return None
            # Twelve Data returns newest-first; flip to oldest-first
            values = list(reversed(values))
            closes = [float(v["close"]) for v in values]
            volumes = [float(v.get("volume") or 0) for v in values]
            return closes, volumes
        except Exception as e:
            print(f"  [warn] time_series failed for {symbol} (attempt {attempt+1}): {e}")
            time.sleep(2)
    return None


def sma(values, period):
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def avg_dollar_volume(closes, volumes, days=21):
    if len(closes) < days or len(volumes) < days:
        return None
    dollar_vols = [c * v for c, v in zip(closes[-days:], volumes[-days:])]
    return sum(dollar_vols) / len(dollar_vols)


def screen_symbol(symbol):
    """Returns a result dict if the symbol passes all 4 filters, else None.
    Cheap Finnhub checks (market cap, beta) run first; the slow, rate-limited
    Twelve Data history call only happens for symbols that already passed both."""
    market_cap, sector = get_market_cap_and_sector(symbol)
    if market_cap is None or market_cap <= MIN_MARKET_CAP_USD:
        return None

    beta = get_beta(symbol)
    if beta is None or beta <= MIN_BETA:
        return None

    hist = get_price_history_and_volume(symbol)
    if hist is None:
        return None
    closes, volumes = hist

    s200 = sma(closes, SMA_PERIOD)
    if s200 is None:
        print(f"  [skip] {symbol}: not enough history for SMA200 ({len(closes)} bars)")
        return None
    price = closes[-1]
    if price <= s200:
        return None

    dvol = avg_dollar_volume(closes, volumes)
    if dvol is None or dvol <= MIN_DOLLAR_VOLUME_1MO:
        return None

    return {
        "symbol": symbol,
        "market_cap": market_cap,
        "beta": round(beta, 2),
        "price": round(price, 2),
        "sma200": round(s200, 2),
        "pct_above_sma200": round((price / s200 - 1) * 100, 2),
        "dollar_volume_1mo": round(dvol, 0),
        "sector": sector,
    }


def build_universe():
    screener_cfg = settings.get("screener", {})
    source = screener_cfg.get("source", "sp500")
    max_candidates = screener_cfg.get("max_candidates")  # None = no cap, scan everything

    extras = []
    extras_path = CONFIG_DIR / "universe.json"
    extras_data = load_json(extras_path, default={"universe": []})
    extras = extras_data.get("universe", [])

    if source == "sp500":
        try:
            sp500 = fetch_sp500_symbols()
            print(f"Fetched {len(sp500)} current S&P 500 constituents.")
        except Exception as e:
            print(f"[warn] Failed to fetch S&P 500 list ({e}); using extras list only.")
            sp500 = []
        universe = list(dict.fromkeys(sp500 + extras))  # de-dup, preserve order
        if extras:
            print(f"Added {len(extras)} extra ticker(s) from config/universe.json (non-S&P500 names).")
    else:
        universe = list(dict.fromkeys(extras))
        print(f"Using manual candidate list from config/universe.json ({len(universe)} symbols).")

    if max_candidates and len(universe) > max_candidates:
        universe = random.sample(universe, max_candidates)
        print(f"Capped to {max_candidates} randomly-sampled symbols "
              f"(screener.max_candidates in settings.json). Run again later for a different slice.")

    return universe


def main():
    universe = build_universe()

    existing_watchlist = load_json(CONFIG_DIR / "watchlist.json", default={"watchlist": []})
    existing_themes = {item["symbol"]: item.get("theme") for item in existing_watchlist.get("watchlist", [])}

    print(f"\nScreening {len(universe)} candidates against 4 filters:")
    print(f"  Price > SMA200, MarketCap > ${MIN_MARKET_CAP_USD/1e9:.1f}B, "
          f"Beta > {MIN_BETA}, $Volume(1mo) > ${MIN_DOLLAR_VOLUME_1MO/1e6:.0f}M\n")

    passed = []
    for sym in universe:
        result = screen_symbol(sym)
        if result:
            passed.append(result)
            print(f"  [PASS] {sym}: cap=${result['market_cap']/1e9:.1f}B beta={result['beta']} "
                  f"price={result['price']} (+{result['pct_above_sma200']}% vs SMA200) "
                  f"$vol={result['dollar_volume_1mo']/1e6:.0f}M")
        else:
            print(f"  [fail] {sym}")

    passed.sort(key=lambda r: r["dollar_volume_1mo"], reverse=True)

    new_watchlist = []
    for r in passed:
        theme = existing_themes.get(r["symbol"]) or r["sector"] or "自動篩選"
        new_watchlist.append({"symbol": r["symbol"], "theme": theme})

    save_json(CONFIG_DIR / "watchlist.json", {
        "_comment": (
            "Auto-generated by scripts/screen_stocks.py based on: Price>SMA200, "
            "MarketCap>2B, Beta>1, $Volume(1mo)>900M, scanned against the current "
            "S&P 500 (+ config/universe.json extras). Re-run the screener to refresh, "
            "or edit by hand - manual edits survive until the next screener run for "
            "symbols that keep passing (their 'theme' is preserved)."
        ),
        "watchlist": new_watchlist,
    })

    print(f"\nDone. {len(passed)}/{len(universe)} symbols passed all 4 filters.")
    print(f"Wrote config/watchlist.json with {len(new_watchlist)} symbols.")
    print("Now run scripts/generate_dashboard.py (or trigger the daily workflow) to render the dashboard.")


if __name__ == "__main__":
    main()
