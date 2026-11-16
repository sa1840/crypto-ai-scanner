
import os
import json
import time
from datetime import datetime, timezone

import ccxt
import pandas as pd
import requests

# ---------------- SETTINGS ----------------
TOP_N = 30
TIMEFRAMES = ["15m", "1h", "4h"]
CANDLE_LIMIT = 100

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

DATA_DIR = "data"
LATEST_FILE = f"{DATA_DIR}/latest.json"
STATE_FILE = f"{DATA_DIR}/alert_state.json"

os.makedirs(DATA_DIR, exist_ok=True)

exchange = ccxt.binance({
    "enableRateLimit": True,
    "options": {"defaultType": "spot"}
})


def load_json(path, default):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def send_telegram(message):
    if not BOT_TOKEN or not CHAT_ID:
        print("Telegram secrets not configured.")
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    try:
        response = requests.post(
            url,
            data={"chat_id": CHAT_ID, "text": message},
            timeout=15
        )
        response.raise_for_status()
    except requests.RequestException as error:
        print("Telegram error:", error)


def get_indicators(symbol, timeframe):
    candles = exchange.fetch_ohlcv(
        symbol, timeframe=timeframe, limit=CANDLE_LIMIT
    )

    if not candles or len(candles) < 60:
        return None

    df = pd.DataFrame(
        candles,
        columns=["time", "open", "high", "low", "close", "volume"]
    )

    # Last candle may still be forming; ignore it.
    df = df.iloc[:-1].copy()

    df["ema9"] = df["close"].ewm(span=9, adjust=False).mean()
    df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()

    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1/14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/14, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, float("nan"))
    df["rsi"] = 100 - (100 / (1 + rs))
    df["rsi"] = df["rsi"].fillna(50)

    current = df.iloc[-1]
    previous = df.iloc[-2]

    bullish_cross = (
        previous["ema9"] <= previous["ema20"]
        and current["ema9"] > current["ema20"]
    )

    bearish_cross = (
        previous["ema9"] >= previous["ema20"]
        and current["ema9"] < current["ema20"]
    )

    bullish = current["ema9"] > current["ema20"]
    bearish = current["ema9"] < current["ema20"]

    score = 50

    if bullish:
        score += 15
    elif bearish:
        score -= 15

    if current["close"] > current["ema50"]:
        score += 10
    else:
        score -= 10

    rsi = float(current["rsi"])

    if 50 <= rsi <= 65:
        score += 10
    elif rsi > 75:
        score -= 10
    elif rsi < 30:
        score += 5
    elif rsi < 45:
        score -= 5

    if bullish_cross:
        score += 15
    elif bearish_cross:
        score -= 15

    score = max(0, min(100, score))

    if score >= 70:
        signal = "BUY"
    elif score <= 30:
        signal = "SELL"
    elif 45 <= score <= 55:
        signal = "HOLD"
    else:
        signal = "NO TRADE"

    return {
        "price": round(float(current["close"]), 8),
        "ema9": round(float(current["ema9"]), 8),
        "ema20": round(float(current["ema20"]), 8),
        "ema50": round(float(current["ema50"]), 8),
        "rsi": round(rsi, 2),
        "score": score,
        "signal": signal,
        "bullish_cross": bool(bullish_cross),
        "bearish_cross": bool(bearish_cross),
        "candle_time": int(current["time"])
    }


def main():
    print("Loading Binance Spot markets...")
    markets = exchange.load_markets()

    tickers = exchange.fetch_tickers()
    candidates = []

    for symbol, market in markets.items():
        if not market.get("spot") or not market.get("active"):
            continue
        if not symbol.endswith("/USDT"):
            continue

        ticker = tickers.get(symbol, {})
        volume = ticker.get("quoteVolume")

        if volume is None:
            continue

        candidates.append((symbol, float(volume)))

    candidates.sort(key=lambda x: x[1], reverse=True)
    selected = [item[0] for item in candidates[:TOP_N]]

    state = load_json(STATE_FILE, {})
    results = []
    alerts = []

    for index, symbol in enumerate(selected, start=1):
        print(f"[{index}/{len(selected)}] Scanning {symbol}")

        for timeframe in TIMEFRAMES:
            try:
                data = get_indicators(symbol, timeframe)

                if data is None:
                    continue

                row = {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    **data
                }
                results.append(row)

                key = f"{symbol}_{timeframe}"
                candle_id = str(data["candle_time"])

                if data["bullish_cross"]:
                    alert_key = f"{key}_bull_{candle_id}"
                    if not state.get(key) == alert_key:
                        alerts.append(
                            f"🟢 BULLISH EMA 9/20 CROSS\n"
                            f"{symbol} | {timeframe}\n"
                            f"Price: {data['price']}\n"
                            f"RSI: {data['rsi']}"
                        )
                        state[key] = alert_key

                elif data["bearish_cross"]:
                    alert_key = f"{key}_bear_{candle_id}"
                    if not state.get(key) == alert_key:
                        alerts.append(
                            f"🔴 BEARISH EMA 9/20 CROSS\n"
                            f"{symbol} | {timeframe}\n"
                            f"Price: {data['price']}\n"
                            f"RSI: {data['rsi']}"
                        )
                        state[key] = alert_key

            except Exception as error:
                print(f"Skipped {symbol} {timeframe}: {error}")

            time.sleep(0.15)

    results.sort(key=lambda x: x["score"], reverse=True)

    output = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "exchange": "Binance Spot",
        "scanned_coins": len(selected),
        "results": results
    }

    save_json(LATEST_FILE, output)
    save_json(STATE_FILE, state)

    for alert in alerts:
        send_telegram(alert)
        time.sleep(0.5)

    print(f"Done. Scanned {len(selected)} coins.")
    print(f"Results saved: {LATEST_FILE}")
    print(f"New crossover alerts: {len(alerts)}")


if __name__ == "__main__":
    main()
