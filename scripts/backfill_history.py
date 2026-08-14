#!/usr/bin/env python3
"""
One-time (or occasional) backfill: pulls REAL historical daily closes from
Twelve Data's free tier and writes them into data/history.json in the same
format the main generator (generate_dashboard.py) expects.

Why this exists: Finnhub's free tier does not serve historical daily candles
for US equities, so the main script builds its own history one day at a time
(~20 trading days to become fully accurate). Running this script once seeds
real history immediately, so the dashboard leaves "proxy mode" right away.

Get a free API key (no credit card): https://twelvedata.com/pricing
Free tier: 800 requests/day, which comfortably covers a ~20-symbol watchlist.

Usage:
    export TWELVEDATA_API_KEY=your_key_here
    python scripts/backfill_history.py

Safe to re-run: it OVERWRITES history for whatever symbols it successfully
fetches, and leaves any symbol it couldn't fetch untouched.
"""

import json
import os
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"

TD_TOKEN = os.environ.get("TWELVEDATA_API_KEY")
BASE_URL = "https://api.twelvedata.com/time_series"

if not TD_TOKEN:
    print("ERROR: TWELVEDATA_API_KEY environment variable not set.", file=sys.stderr)
    print("Get a free key at https://twelvedata.com/pricing (no card needed).", file=sys.stderr)
    sys.exit(1)


def load_json(path, default=None):
    if not path.exists():
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


settings = load_json(CONFIG_DIR / "settings.json")
watchlist_cfg = load_json(CONFIG_DIR / "watchlist.json")["watchlist"]
history = load_json(DATA_DIR / "history.json", default={})

LOOKBACK = settings.get("history_lookback_days", 60)


def fetch_history(symbol, outputsize=60, retries=3):
    for attempt in range(retries):
        try:
            r = requests.get(
                BASE_URL,
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
                print(f"  [warn] {symbol}: {data.get('message')}")
                return None
            values = data.get("values")
            if not values:
                print(f"  [warn] {symbol}: no values returned")
                return None
            # Twelve Data returns newest-first; we want oldest-first to match history.json
            values = list(reversed(values))
            return [{"date": v["datetime"], "close": round(float(v["close"]), 2)} for v in values]
        except Exception as e:
            print(f"  [warn] fetch failed for {symbol} (attempt {attempt+1}): {e}")
            time.sleep(2)
    return None


def main():
    settings_syms = (
        settings["index_symbols"]
        + [settings["vix_proxy_symbol"]]
        + settings["sector_etfs"]
    )
    watchlist_syms = [item["symbol"] for item in watchlist_cfg]
    all_syms = list(dict.fromkeys(settings_syms + watchlist_syms))

    print(f"Backfilling real historical data for {len(all_syms)} symbols via Twelve Data...")
    ok, failed = [], []
    for i, sym in enumerate(all_syms):
        series = fetch_history(sym, outputsize=LOOKBACK)
        if series:
            history[sym] = series
            ok.append(sym)
        else:
            failed.append(sym)
        # Free tier is rate-limited (~8 req/min) - stay well under it.
        if i < len(all_syms) - 1:
            time.sleep(8)

    save_json(DATA_DIR / "history.json", history)
    print(f"\nDone. Backfilled: {len(ok)}/{len(all_syms)}")
    if failed:
        print(f"Failed (left untouched, will fall back to proxy mode for these): {failed}")
    print("Now run scripts/generate_dashboard.py (or trigger the daily workflow) "
          "to render the dashboard using this real history.")


if __name__ == "__main__":
    main()
