# =============================================================
#  BACKTEST — DOWNLOADER
#  Descarga 1 año de velas 1h desde Binance public API
#  Corre una sola vez: python downloader.py
# =============================================================

import requests
import pandas as pd
import time
import os
from datetime import datetime, timedelta

SYMBOLS   = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]
INTERVALS = ["1h", "4h"]
DAYS      = 730
OUT_DIR   = "data"


def download_klines(symbol: str, interval: str, days: int) -> pd.DataFrame:
    """Descarga velas históricas desde Binance en bloques de 1000."""
    url      = "https://api.binance.com/api/v3/klines"
    end_ms   = int(datetime.now().timestamp() * 1000)
    start_ms = int((datetime.now() - timedelta(days=days)).timestamp() * 1000)

    all_klines = []
    current    = start_ms

    while current < end_ms:
        params = {
            "symbol":    symbol,
            "interval":  interval,
            "startTime": current,
            "endTime":   end_ms,
            "limit":     1000,
        }
        r    = requests.get(url, params=params, timeout=15)
        data = r.json()

        if not data:
            break

        all_klines.extend(data)
        current = data[-1][0] + 1  # siguiente vela
        time.sleep(0.3)            # respetar rate limit

        pct = min(100, (current - start_ms) / (end_ms - start_ms) * 100)
        print(f"  {symbol}: {pct:.0f}% — {len(all_klines)} velas", end="\r")

    print(f"  {symbol}: 100% — {len(all_klines)} velas totales          ")

    df = pd.DataFrame(all_klines, columns=[
        "timestamp", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore"
    ])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df.set_index("timestamp", inplace=True)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    return df[["open", "high", "low", "close", "volume"]]


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    total = len(SYMBOLS) * len(INTERVALS)
    print(f"Descargando {DAYS} días ({', '.join(INTERVALS)}) para {len(SYMBOLS)} pares — {total} archivos...\n")

    for symbol in SYMBOLS:
        for interval in INTERVALS:
            path = os.path.join(OUT_DIR, f"{symbol}_{interval}.csv")
            print(f"→ {symbol} {interval}")
            try:
                df = download_klines(symbol, interval, DAYS)
                df.to_csv(path)
                print(f"  Guardado: {path}  ({len(df)} filas, {df.index[0].date()} → {df.index[-1].date()})\n")
            except Exception as e:
                print(f"  ERROR en {symbol} {interval}: {e}\n")

    print("Descarga completa.")
    print("  → Corré simulator.py para el backtest baseline")
    print("  → Corré regime_trainer.py para entrenar el clasificador HMM")


if __name__ == "__main__":
    main()
