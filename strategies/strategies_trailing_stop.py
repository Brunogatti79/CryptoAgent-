"""
strategies/trailing_stop.py
────────────────────────────
TrailingStopManager — gestiona trailing stops basados en ATR(14) de velas 1h.

Lógica:
  - Al abrir un trade: stop inicial = entrada − ATR * ATR_MULT (para LONG)
  - En cada tick de precio: si el precio sube, el stop sube con él (ratchet)
  - Si el precio toca o cruza el stop, retorna True → cerrar posición

El ATR se recalcula cada vez que se inicializa un stop (una sola vez por trade).
El trailing posterior es aritmético puro (no recalcula ATR en cada tick).
"""

import asyncio
import logging
import sqlite3
from typing import Optional

import requests
import pandas as pd

log = logging.getLogger(__name__)

# ── Parámetros ────────────────────────────────────────────────
ATR_PERIOD   = 14       # período ATR sobre velas 1h
ATR_MULT     = 1.5      # multiplicador: stop = entrada ± ATR * ATR_MULT
KLINES_LIMIT = 60       # velas 1h a descargar (> ATR_PERIOD + margen)
BINANCE_KLINES = "https://api.binance.com/api/v3/klines"


# ═══════════════════════════════════════════════════════════════
class TrailingStopManager:
    """
    Mantiene en memoria el trailing stop de cada posición abierta.

    state: { trade_id: {"stop": float, "peak": float, "atr": float, "direction": str} }
    """

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._state: dict[int, dict] = {}

    async def initialize_stop(self, trade: dict) -> Optional[float]:
        """
        Calcula el stop inicial para un trade recién abierto.
        Persiste atr_value y trailing_stop_price en la DB.
        Retorna el precio de stop, o None si falla.
        """
        trade_id    = trade["id"]
        symbol      = trade["symbol"]
        direction   = trade["direction"]
        entry_price = trade["entry_price"]

        atr = await _fetch_atr(symbol)
        if atr is None:
            log.warning(f"[TS] No se pudo calcular ATR para {symbol}")
            return None

        if direction == "LONG":
            stop = entry_price - atr * ATR_MULT
        else:
            stop = entry_price + atr * ATR_MULT

        self._state[trade_id] = {
            "stop":      stop,
            "peak":      entry_price,
            "atr":       atr,
            "direction": direction,
            "symbol":    symbol,
        }

        _persist_trailing(self._db_path, trade_id, stop, atr)
        log.info(
            f"[TS] #{trade_id} {symbol} {direction} | "
            f"entry={entry_price:.4f} ATR={atr:.4f} stop={stop:.4f}"
        )
        return stop

    def update_on_price(self, trade_id: int, current_price: float) -> bool:
        """
        Actualiza el trailing stop con el precio actual.
        Retorna True si el stop fue tocado (posición debe cerrarse).
        """
        st = self._state.get(trade_id)
        if st is None:
            return False

        direction = st["direction"]
        atr       = st["atr"]
        stop      = st["stop"]
        peak      = st["peak"]

        if direction == "LONG":
            if current_price > peak:
                new_peak = current_price
                new_stop = new_peak - atr * ATR_MULT
                if new_stop > stop:
                    st["peak"] = new_peak
                    st["stop"] = new_stop
                    _persist_trailing(self._db_path, trade_id, new_stop, atr)
                    log.debug(
                        f"[TS] #{trade_id} LONG peak={new_peak:.4f} → stop={new_stop:.4f}"
                    )
            if current_price <= st["stop"]:
                log.info(f"[TS] #{trade_id} LONG STOP HIT @ {current_price:.4f} (stop={st['stop']:.4f})")
                return True

        else:  # SHORT
            if current_price < peak:
                new_peak = current_price
                new_stop = new_peak + atr * ATR_MULT
                if new_stop < stop:
                    st["peak"] = new_peak
                    st["stop"] = new_stop
                    _persist_trailing(self._db_path, trade_id, new_stop, atr)
                    log.debug(
                        f"[TS] #{trade_id} SHORT peak={new_peak:.4f} → stop={new_stop:.4f}"
                    )
            if current_price >= st["stop"]:
                log.info(f"[TS] #{trade_id} SHORT STOP HIT @ {current_price:.4f} (stop={st['stop']:.4f})")
                return True

        return False

    def get_stop(self, trade_id: int) -> Optional[float]:
        st = self._state.get(trade_id)
        return st["stop"] if st else None

    def remove(self, trade_id: int) -> None:
        self._state.pop(trade_id, None)

    def load_open_trades(self, trades: list[dict]) -> None:
        """Restaura el estado en memoria leyendo trailing_stop_price desde la DB."""
        for trade in trades:
            tid = trade["id"]
            if tid in self._state:
                continue
            stop = trade.get("trailing_stop_price") or trade.get("stop_loss")
            atr  = trade.get("atr_value", 0.0)
            if stop:
                self._state[tid] = {
                    "stop":      stop,
                    "peak":      trade["entry_price"],
                    "atr":       atr,
                    "direction": trade["direction"],
                    "symbol":    trade["symbol"],
                }
                log.info(f"[TS] Restaurado #{tid} {trade['symbol']} stop={stop:.4f}")


# ── Helpers internos ──────────────────────────────────────────

async def _fetch_atr(symbol: str, period: int = ATR_PERIOD) -> Optional[float]:
    """Descarga velas 1h y calcula ATR(period). Async via executor."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _calc_atr_sync, symbol, period)


def _calc_atr_sync(symbol: str, period: int) -> Optional[float]:
    """Versión sincrónica del cálculo ATR 1h."""
    binance_sym = symbol.replace("/", "")
    try:
        resp = requests.get(
            BINANCE_KLINES,
            params={"symbol": binance_sym, "interval": "1h", "limit": KLINES_LIMIT},
            timeout=10,
        )
        klines = resp.json()
        if not klines or isinstance(klines, dict):
            log.warning(f"[TS] ATR klines vacías para {symbol}")
            return None

        high  = pd.Series([float(k[2]) for k in klines])
        low   = pd.Series([float(k[3]) for k in klines])
        close = pd.Series([float(k[4]) for k in klines])

        prev_close = close.shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low  - prev_close).abs(),
        ], axis=1).max(axis=1)

        atr = float(tr.rolling(period).mean().iloc[-1])
        return atr if pd.notna(atr) else None

    except Exception as e:
        log.warning(f"[TS] Error calculando ATR {symbol}: {e}")
        return None


def _persist_trailing(db_path: str, trade_id: int, stop: float, atr: float) -> None:
    """Actualiza trailing_stop_price y atr_value en la DB."""
    try:
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE trades SET trailing_stop_price=?, atr_value=? WHERE id=?",
            (round(stop, 8), round(atr, 8), trade_id),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning(f"[TS] Error persistiendo stop #{trade_id}: {e}")


# ── calc_atr_multi ────────────────────────────────────────────
# Requerida por executor.py para calcular SL/TP con ATR 4h.

def calc_atr_multi(symbol: str, period: int = 14) -> dict:
    """
    Calcula ATR en 1h y 4h simultaneamente.
    Usado por executor.py para definir SL/TP coherentes con la señal 4h.

    Returns:
        {
            'atr_1h':     float | None,
            'atr_4h':     float | None,
            'ratio':      float | None,   # atr_4h / atr_1h (esperable ~2-4x)
            'compressed': bool            # True si ratio < 1.5 (baja fiabilidad)
        }
    """
    atr_1h = _calc_atr_timeframe(symbol, period=period, timeframe='1h')
    atr_4h = _calc_atr_timeframe(symbol, period=period, timeframe='4h')

    ratio      = None
    compressed = False

    if atr_1h and atr_4h and atr_1h > 0:
        ratio      = round(atr_4h / atr_1h, 2)
        compressed = ratio < 1.5

    return {
        'atr_1h':     atr_1h,
        'atr_4h':     atr_4h,
        'ratio':      ratio,
        'compressed': compressed,
    }


def _calc_atr_timeframe(symbol: str, period: int = 14,
                         timeframe: str = '1h') -> Optional[float]:
    """Calcula ATR para un timeframe especifico (1h, 4h, 1d)."""
    binance_sym = symbol.replace('/', '')
    limit = period * 3
    try:
        resp = requests.get(
            BINANCE_KLINES,
            params={"symbol": binance_sym, "interval": timeframe, "limit": limit},
            timeout=10,
        )
        klines = resp.json()
        if not klines or isinstance(klines, dict):
            return None

        high  = pd.Series([float(k[2]) for k in klines])
        low   = pd.Series([float(k[3]) for k in klines])
        close = pd.Series([float(k[4]) for k in klines])

        prev_close = close.shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low  - prev_close).abs(),
        ], axis=1).max(axis=1)

        atr = float(tr.rolling(period).mean().iloc[-1])
        return atr if pd.notna(atr) and atr > 0 else None

    except Exception as e:
        log.warning(f"[TS] Error ATR {symbol} {timeframe}: {e}")
        return None
