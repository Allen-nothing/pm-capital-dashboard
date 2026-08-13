#!/usr/bin/env python3
"""
PM Capital Allocation Dashboard - daily generator.

What this does:
  1. Pulls current quotes from Finnhub for SPY/QQQ (market trend), a VIX proxy
     ETF, 11 sector ETFs (breadth), and your watchlist.
  2. Appends today's closing price to a local rolling history file
     (data/history.json) that lives in the repo. Finnhub's free tier does
     NOT reliably serve historical daily candles for US equities, so instead
     of depending on that endpoint, we build our own price history one data
     point per day. SMA/RSI/trend scoring only becomes fully reliable after
     ~20 trading days of accumulated history (MIN_HISTORY_FOR_TREND) - before
     that we fall back to a same-day % change proxy and label the page
     accordingly.
  3. Computes a 0-100 PM Confidence Index from trend + VIX + sector breadth.
  4. Scores each watchlist symbol for a conviction number and a *heuristic*
     options-strategy tag. IMPORTANT: Finnhub's free tier has no options
     chain / IV data, so this is a technical-momentum heuristic, not real
     options analytics (IV rank, skew, etc). Treat "Conviction" and the
     strategy tag as a starting point for your own review, not a signal.
  5. Renders a single-page HTML dashboard styled after the reference brief.

This tool does not place trades and is not investment advice - it is a
personal screening aid. All thresholds live in config/settings.json.
"""

import json
import os
import sys
import time
import datetime
import statistics
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
OUTPUT_PATH = ROOT / "index.html"

FINNHUB_TOKEN = os.environ.get("FINNHUB_API_KEY")
BASE_URL = "https://finnhub.io/api/v1"

if not FINNHUB_TOKEN:
    print("ERROR: FINNHUB_API_KEY environment variable not set.", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Config / data I/O
# ---------------------------------------------------------------------------

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

TODAY = datetime.date.today().isoformat()
LOOKBACK = settings.get("history_lookback_days", 60)
MIN_HISTORY_FOR_TREND = settings.get("min_history_for_trend", 20)


# ---------------------------------------------------------------------------
# Finnhub client (with light retry + rate-limit friendliness)
# ---------------------------------------------------------------------------

def get_quote(symbol, retries=3):
    """Returns Finnhub /quote payload: c (current), pc (prev close), dp (% change)."""
    for attempt in range(retries):
        try:
            r = requests.get(
                f"{BASE_URL}/quote",
                params={"symbol": symbol, "token": FINNHUB_TOKEN},
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()
            if data.get("c") in (None, 0):
                raise ValueError(f"empty quote for {symbol}: {data}")
            return data
        except Exception as e:
            print(f"  [warn] quote fetch failed for {symbol} (attempt {attempt+1}): {e}")
            time.sleep(1.5)
    return None


def fetch_all_quotes(symbols):
    quotes = {}
    for sym in symbols:
        q = get_quote(sym)
        quotes[sym] = q
        time.sleep(0.3)  # be gentle with free-tier rate limits (60/min)
    return quotes


# ---------------------------------------------------------------------------
# Rolling history maintenance (our own SMA/RSI data source)
# ---------------------------------------------------------------------------

def update_history(symbol, close_price):
    series = history.setdefault(symbol, [])
    if series and series[-1]["date"] == TODAY:
        series[-1]["close"] = close_price  # re-running same day overwrites, no dupes
    else:
        series.append({"date": TODAY, "close": close_price})
    # trim to lookback window
    if len(series) > LOOKBACK:
        history[symbol] = series[-LOOKBACK:]
    return history[symbol]


def closes(symbol):
    return [p["close"] for p in history.get(symbol, [])]


def sma(values, period):
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def rsi(values, period=14):
    if len(values) < period + 1:
        return None
    gains, losses = [], []
    for i in range(-period, 0):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def trend_score(symbol, day_change_pct):
    """0-100 score. Uses SMA20 vs SMA50 once enough history exists,
    otherwise falls back to today's % change as a rough proxy."""
    vals = closes(symbol)
    if len(vals) >= MIN_HISTORY_FOR_TREND:
        s20 = sma(vals, min(20, len(vals)))
        s50 = sma(vals, min(50, len(vals))) or s20
        last = vals[-1]
        # price above both SMAs and SMA20 above SMA50 = strong uptrend
        score = 50
        score += 15 if last > s20 else -15
        score += 15 if s20 >= s50 else -15
        score += max(-20, min(20, day_change_pct * 4))
        return max(0, min(100, score)), True
    else:
        # proxy mode: scale day change into a 0-100 band around 50
        score = 50 + max(-40, min(40, day_change_pct * 8))
        return max(0, min(100, score)), False


# ---------------------------------------------------------------------------
# Confidence Index
# ---------------------------------------------------------------------------

def compute_confidence_index(quotes):
    weights = settings["confidence_index"]["weights"]
    index_syms = settings["index_symbols"]
    sector_etfs = settings["sector_etfs"]
    vix_symbol = settings["vix_proxy_symbol"]

    trend_scores = []
    proxy_mode_flags = []
    for sym in index_syms:
        q = quotes.get(sym)
        if not q:
            continue
        update_history(sym, q["c"])
        ts, is_real = trend_score(sym, q.get("dp", 0.0) or 0.0)
        trend_scores.append(ts)
        proxy_mode_flags.append(is_real)
    trend_component = statistics.mean(trend_scores) if trend_scores else 50

    vix_q = quotes.get(vix_symbol)
    if vix_q:
        update_history(vix_symbol, vix_q["c"])
        # VIXY-style proxy: bigger daily jump = more fear = lower confidence
        dp = vix_q.get("dp", 0.0) or 0.0
        vix_component = max(0, min(100, 60 - dp * 6))
    else:
        vix_component = 50

    breadth_hits = 0
    breadth_total = 0
    for sym in sector_etfs:
        q = quotes.get(sym)
        if not q:
            continue
        update_history(sym, q["c"])
        breadth_total += 1
        if (q.get("dp", 0.0) or 0.0) > 0:
            breadth_hits += 1
    breadth_component = (breadth_hits / breadth_total * 100) if breadth_total else 50

    confidence = (
        trend_component * weights["trend"]
        + vix_component * weights["vix"]
        + breadth_component * weights["breadth"]
    )
    confidence = round(max(0, min(100, confidence)), 1)

    is_proxy_mode = not all(proxy_mode_flags) if proxy_mode_flags else True

    return {
        "score": confidence,
        "trend_component": round(trend_component, 1),
        "vix_component": round(vix_component, 1),
        "breadth_component": round(breadth_component, 1),
        "breadth_hits": breadth_hits,
        "breadth_total": breadth_total,
        "proxy_mode": is_proxy_mode,
    }


def classify_confidence(score):
    cfg = settings["confidence_index"]
    if score >= cfg["full_deploy_threshold"]:
        return {
            "label_zh": "全倉部署",
            "label_en": "AGGRESSIVE",
            "sub_zh": "Deploy 75-100%",
            "color": "green",
        }
    elif score >= cfg["selective_threshold"]:
        return {
            "label_zh": "選擇性部署",
            "label_en": "SELECTIVE",
            "sub_zh": "Deploy 25-50%",
            "color": "gold",
        }
    else:
        return {
            "label_zh": "空倉觀望",
            "label_en": "DEFENSIVE",
            "sub_zh": "Deploy 0%, Cash",
            "color": "red",
        }


# ---------------------------------------------------------------------------
# Watchlist scoring -> tonight's trade / long-term pick / avoid
# ---------------------------------------------------------------------------

def strategy_for(score, rsi_val):
    """Heuristic tag only - NOT real options/IV analytics (see module docstring)."""
    if rsi_val is None:
        if score >= 60:
            return "Momentum Long (building history)"
        elif score <= 40:
            return "Avoid / Wait for data"
        else:
            return "Watch — Insufficient History"
    if score >= 60 and 40 <= rsi_val <= 68:
        return "Put Credit Spread"
    if score >= 65 and rsi_val > 68:
        return "Long Calls / Trend Continuation"
    if score <= 40 and rsi_val < 35:
        return "Call Credit Spread (fade bounce)"
    if score <= 35:
        return "No Trade"
    return "Watch / No Edge"


def score_watchlist(quotes):
    results = []
    for item in watchlist_cfg:
        sym = item["symbol"]
        q = quotes.get(sym)
        if not q:
            continue
        update_history(sym, q["c"])
        day_change = q.get("dp", 0.0) or 0.0
        ts, is_real = trend_score(sym, day_change)
        vals = closes(sym)
        rsi_val = rsi(vals) if is_real else None

        # relative strength vs SPY today
        spy_q = quotes.get("SPY")
        rel_strength = day_change - (spy_q.get("dp", 0.0) if spy_q else 0.0)

        conviction = (ts / 10) * 0.7 + max(-3, min(3, rel_strength)) * 0.5
        conviction = round(max(0, min(10, conviction)), 1)

        blackout = item.get("earnings_blackout")
        is_blackout = blackout == TODAY

        results.append({
            "symbol": sym,
            "theme": item.get("theme", "Trend Leader"),
            "trend_score": round(ts, 1),
            "conviction": conviction,
            "rsi": round(rsi_val, 1) if rsi_val is not None else None,
            "day_change_pct": round(day_change, 2),
            "strategy": "NO TRADE (earnings blackout)" if is_blackout else strategy_for(ts, rsi_val),
            "is_blackout": is_blackout,
            "has_full_history": is_real,
        })

    results.sort(key=lambda r: r["conviction"], reverse=True)
    return results


def pick_slots(scored):
    if not scored:
        return None, None, None
    eligible = [r for r in scored if not r["is_blackout"]]
    tonight = eligible[0] if eligible else scored[0]
    remaining = [r for r in eligible if r["symbol"] != tonight["symbol"]]
    long_term = max(remaining, key=lambda r: r["trend_score"]) if remaining else tonight
    avoid_candidates = [r for r in scored if r["is_blackout"]] or scored
    avoid = min(avoid_candidates, key=lambda r: r["conviction"])
    return tonight, long_term, avoid


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

COLOR_MAP = {
    "green": "#1a7a3c",
    "gold": "#b8860b",
    "red": "#c0392b",
}

def render_html(confidence, classification, tonight, long_term, avoid, quotes):
    color = COLOR_MAP[classification["color"]]
    dot = {"green": "🟢", "gold": "🟡", "red": "🔴"}[classification["color"]]
    now_hkt = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    date_str = now_hkt.strftime("%Y-%m-%d")

    proxy_note = ""
    if confidence["proxy_mode"]:
        proxy_note = (
            '<p class="proxy-note">⚠ Trend history still building (need '
            f'{MIN_HISTORY_FOR_TREND} trading days) — scores currently use a '
            "same-day % change proxy and will sharpen over the next few weeks.</p>"
        )

    def stock_card_tonight():
        return f"""
        <div class="card card-trade">
          <div class="card-label">今晚交易 · Tonight's Trade</div>
          <div class="card-ticker">{tonight['symbol']}</div>
          <div class="card-detail">{tonight['strategy']}</div>
          <div class="card-sub">Conv {tonight['conviction']}</div>
        </div>"""

    def stock_card_longterm():
        return f"""
        <div class="card card-longterm">
          <div class="card-label">長線首選 · Long-Term Pick</div>
          <div class="card-ticker"><span class="dot">🟢</span>{long_term['symbol']}</div>
          <div class="card-detail">{long_term['theme']}</div>
          <div class="card-sub">Score {long_term['trend_score']}</div>
        </div>"""

    def stock_card_avoid():
        reason = "NO TRADE (earnings blackout)" if avoid["is_blackout"] else "NO TRADE"
        return f"""
        <div class="card card-avoid">
          <div class="card-label">避免交易 · Avoid</div>
          <div class="card-ticker"><span class="dot">🔴</span>{avoid['symbol']}</div>
          <div class="card-detail">{reason}</div>
        </div>"""

    html = f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PM Capital Allocation Dashboard · {date_str}</title>
<style>
  :root {{
    --navy: #12294a;
    --brown: #6b3e10;
    --gold-bg: #fdf3e0;
    --gold-border: #c8912a;
    --gray-bg: #f2f3f4;
    --pink-bg: #fdecec;
    --text-dark: #1c1c1c;
    --text-muted: #6b7280;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    font-family: -apple-system, "Segoe UI", "PingFang HK", "Noto Sans HK", Arial, sans-serif;
    background: #ffffff;
    margin: 0;
    padding: 24px;
    color: var(--text-dark);
  }}
  .wrap {{ max-width: 1100px; margin: 0 auto; }}
  .header {{
    background: var(--navy);
    color: #fff;
    padding: 22px 28px;
    border-radius: 10px 10px 0 0;
  }}
  .header .eyebrow {{
    letter-spacing: 3px;
    font-size: 13px;
    opacity: 0.85;
    text-transform: uppercase;
    margin: 0 0 6px 0;
  }}
  .header h1 {{
    font-size: 22px;
    margin: 0;
    font-weight: 700;
  }}
  .cards {{
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 16px;
    padding: 22px 4px;
  }}
  .card {{
    border-radius: 8px;
    padding: 18px;
    min-height: 140px;
  }}
  .card-label {{
    font-size: 13px;
    color: var(--text-muted);
    margin-bottom: 10px;
    font-weight: 600;
  }}
  .card-confidence {{
    border: 2px solid {color};
    background: #fff;
  }}
  .confidence-score {{
    font-size: 34px;
    font-weight: 800;
    color: {color};
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 6px;
  }}
  .confidence-status {{
    font-weight: 700;
    font-size: 15px;
  }}
  .confidence-sub {{
    color: var(--text-muted);
    font-size: 13px;
    margin-top: 2px;
  }}
  .card-trade {{ background: var(--gold-bg); border: 1px solid var(--gold-border); }}
  .card-longterm {{ background: var(--gray-bg); }}
  .card-avoid {{ background: var(--pink-bg); }}
  .card-ticker {{
    font-size: 26px;
    font-weight: 800;
    margin-bottom: 6px;
  }}
  .card-trade .card-ticker {{ color: #a06a10; }}
  .card-longterm .card-ticker {{ color: #1a7a3c; }}
  .card-avoid .card-ticker {{ color: #c0392b; }}
  .card-detail {{ font-size: 14px; font-weight: 600; }}
  .card-sub {{ font-size: 13px; color: var(--text-muted); margin-top: 4px; }}
  .action-bar {{
    background: var(--brown);
    color: #fff;
    padding: 16px 22px;
    font-size: 16px;
    font-weight: 700;
    border-radius: 6px 6px 0 0;
    margin-top: 8px;
  }}
  .action-detail {{
    background: #f5f6f8;
    display: grid;
    grid-template-columns: 1.4fr 1fr 1fr;
    padding: 18px 22px;
    border-radius: 0 0 6px 6px;
    font-size: 15px;
  }}
  .action-detail .avoid-text {{ color: #c0392b; font-weight: 700; }}
  .action-detail .label {{ font-weight: 700; }}
  .proxy-note {{
    font-size: 12.5px;
    color: #a06a10;
    background: #fdf3e0;
    border: 1px solid var(--gold-border);
    padding: 8px 12px;
    border-radius: 6px;
    margin: 4px 4px 0 4px;
  }}
  .disclaimer {{
    margin-top: 18px;
    padding: 12px 16px;
    font-size: 12px;
    color: var(--text-muted);
    border-top: 1px solid #e5e7eb;
  }}
  .footer-meta {{
    font-size: 11px;
    color: #9ca3af;
    text-align: right;
    margin-top: 8px;
  }}
  @media (max-width: 800px) {{
    .cards {{ grid-template-columns: repeat(2, 1fr); }}
    .action-detail {{ grid-template-columns: 1fr; gap: 10px; }}
  }}
</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <p class="eyebrow">PM CAPITAL ALLOCATION DASHBOARD</p>
    <h1>{date_str} · EXECUTIVE BRIEF</h1>
  </div>

  {proxy_note}

  <div class="cards">
    <div class="card card-confidence">
      <div class="card-label">PM 信心指數 · Confidence Index</div>
      <div class="confidence-score">{dot} {confidence['score']}</div>
      <div class="confidence-status">{classification['label_en']} · {classification['label_zh']}</div>
      <div class="confidence-sub">{classification['sub_zh']}</div>
    </div>
    {stock_card_tonight()}
    {stock_card_longterm()}
    {stock_card_avoid()}
  </div>

  <div class="action-bar">行動結論：{dot} {classification['label_zh']} {classification['label_en']}</div>
  <div class="action-detail">
    <div><span class="label">首選：</span>{tonight['symbol']} {tonight['strategy']}</div>
    <div><span class="label">建議部署：</span>{classification['sub_zh']}</div>
    <div class="avoid-text"><span class="label">避免：</span>{avoid['symbol']}</div>
  </div>

  <div class="disclaimer">
    本頁面由程式自動生成，僅供個人參考，並非投資建議。Conviction 分數及策略標籤基於技術動能的簡化演算法，
    並非真實期權 IV / Greeks 分析（Finnhub 免費方案不提供期權鏈數據）。交易決策及風險自負。
    Index components — Trend: {confidence['trend_component']} · VIX: {confidence['vix_component']} ·
    Breadth: {confidence['breadth_component']} ({confidence['breadth_hits']}/{confidence['breadth_total']} sectors up).
  </div>
  <div class="footer-meta">Generated {now_hkt.strftime('%Y-%m-%d %H:%M')} HKT · Data: Finnhub</div>
</div>
</body>
</html>
"""
    return html


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    settings_syms = (
        settings["index_symbols"]
        + [settings["vix_proxy_symbol"]]
        + settings["sector_etfs"]
    )
    watchlist_syms = [item["symbol"] for item in watchlist_cfg]
    all_syms = list(dict.fromkeys(settings_syms + watchlist_syms))

    print(f"Fetching quotes for {len(all_syms)} symbols...")
    quotes = fetch_all_quotes(all_syms)

    missing = [s for s, q in quotes.items() if q is None]
    if missing:
        print(f"  [warn] no data for: {missing}")

    confidence = compute_confidence_index(quotes)
    classification = classify_confidence(confidence["score"])
    scored_watchlist = score_watchlist(quotes)
    tonight, long_term, avoid = pick_slots(scored_watchlist)

    if tonight is None:
        print("ERROR: no watchlist data available, aborting render.", file=sys.stderr)
        sys.exit(1)

    html = render_html(confidence, classification, tonight, long_term, avoid, quotes)
    OUTPUT_PATH.write_text(html, encoding="utf-8")
    save_json(DATA_DIR / "history.json", history)

    print(f"Confidence Index: {confidence['score']} ({classification['label_en']})")
    print(f"Tonight: {tonight['symbol']} — {tonight['strategy']} (Conv {tonight['conviction']})")
    print(f"Long-term: {long_term['symbol']}")
    print(f"Avoid: {avoid['symbol']}")
    print(f"Wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
