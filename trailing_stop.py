# =============================================================
#  CRYPTO AGENT — TRAILING STOP
#  ATR-based trailing stop manager.
#
#  CAMBIO v2: _calc_atr_sync acepta timeframe ('1h', '4h', '1d')
#  ─────────────────────────────────────────────────────────────
#  Roles por timeframe:
#    4h → SL/TP inicial (coherente con la tesis de entrada)
#    1h → trailing stop en runtime (seguimiento fino del precio)
# =============================================================

import logging
import requests
import pandas as pd

log = logging.getLogger(__name__)

# Multiplicador ATR para trailing stop en runtime (1h)
ATR_MULT = 1.5

# Intervalo por defecto para el trailing en tiempo real
TRAILING_TIMEFRAME = '1h'

# Intervalo para el cálculo inicial de SL/TP
ENTRY_TIMEFRAME = '4h'

# Mínimo de velas necesarias para un ATR confiable
MIN_CANDLES = 20

# ── ATR sincrónico ────────────────────────────────────────────

def _calc_atr_sync(symbol: str, period: int = 14,
                   timeframe: str = '1h') -> float | None:
    """
    Calcula ATR(period) para el símbolo y timeframe indicados.

    Args:
        symbol:    Par en formato 'BTC/USDT' o 'BTCUSDT'
        period:    Período del ATR (default 14)
        timeframe: '1h', '4h', '1d' (default '1h')

    Returns:
        float con el ATR de la última vela, o None si hay error.

    Roles:
        - timeframe='4h' → usar para SL/TP inicial (coherente con señal)
        - timeframe='1h' → usar para trailing stop en runtime
    """
    VALID_TIMEFRAMES = {'1h', '4h', '1d', '15m', '1w'}
    if timeframe not in VALID_TIMEFRAMES:
        log.warning(f"[trailing] timeframe '{timeframe}' no válido — usando '1h'")
        timeframe = '1h'

    binance_symbol = symbol.replace('/', '')
    limit          = period * 3  # velas suficientes para ATR estable

    url = (
        f"https://api.binance.com/api/v3/klines"
        f"?symbol={binance_symbol}&interval={timeframe}&limit={limit}"
    )

    try:
        resp   = requests.get(url, timeout=10)
        resp.raise_for_status()
        klines = resp.json()

        if len(klines) < MIN_CANDLES:
            log.warning(f"[trailing] {symbol} {timeframe}: solo {len(klines)} velas — ATR no confiable")
            return None

        high  = pd.Series([float(k[2]) for k in klines])
        low   = pd.Series([float(k[3]) for k in klines])
        close = pd.Series([float(k[4]) for k in klines])

        # True Range
        prev_close = close.shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low  - prev_close).abs(),
        ], axis=1).max(axis=1)

        atr = tr.rolling(period).mean().iloc[-1]

        if pd.isna(atr) or atr <= 0:
            log.warning(f"[trailing] {symbol} {timeframe}: ATR inválido ({atr})")
            return None

        log.debug(f"[trailing] ATR {symbol} {timeframe}: {atr:.6f}")
        return float(atr)

    except requests.RequestException as e:
        log.error(f"[trailing] Error fetch {symbol} {timeframe}: {e}")
        return None
    except Exception as e:
        log.error(f"[trailing] Error calculando ATR {symbol} {timeframe}: {e}")
        return None


def calc_atr_multi(symbol: str, period: int = 14) -> dict:
    """
    Calcula ATR en 1h y 4h simultáneamente.
    Útil para comparar y detectar compresión anormal.

    Returns:
        {
            'atr_1h':  float | None,
            'atr_4h':  float | None,
            'ratio':   float | None,   # atr_4h / atr_1h (esperable ~2-4x)
            'compressed': bool         # True si ratio < 1.5 (señal de baja fiabilidad)
        }
    """
    atr_1h = _calc_atr_sync(symbol, period=period, timeframe='1h')
    atr_4h = _calc_atr_sync(symbol, period=period, timeframe='4h')

    ratio      = None
    compressed = False

    if atr_1h and atr_4h and atr_1h > 0:
        ratio      = round(atr_4h / atr_1h, 2)
        # Si ATR 4h < 1.5× ATR 1h, el mercado está comprimido
        # En condiciones normales ATR 4h debería ser ~2-4x el ATR 1h
        compressed = ratio < 1.5

    return {
        'atr_1h':     atr_1h,
        'atr_4h':     atr_4h,
        'ratio':      ratio,
        'compressed': compressed,
    }


# ── TrailingStopManager ───────────────────────────────────────

class TrailingStopManager:
    """
    Gestiona trailing stops en memoria para posiciones abiertas.
    Usa ATR 1h para el seguimiento intradiario del precio.
    """

    def __init__(self, db_path: str):
        self._db_path  = db_path
        self._stops:   dict[int, float] = {}   # trade_id → stop actual
        self._atrs:    dict[int, float] = {}   # trade_id → ATR 1h inicial

    def load_open_trades(self, trades: list[dict]) -> None:
        """Restaura stops desde DB al arrancar."""
        for t in trades:
            tid  = t['id']
            stop = t.get('trailing_stop_price') or t.get('stop_loss')
            atr  = t.get('atr_value')
            if stop:
                self._stops[tid] = float(stop)
            if atr:
                self._atrs[tid]  = float(atr)

    async def initialize_stop(self, trade: dict) -> float | None:
        """
        Inicializa el trailing stop para un trade recién detectado.
        Usa ATR 1h (rol: seguimiento fino del precio, no definición de riesgo).
        """
        import asyncio
        loop = asyncio.get_event_loop()
        atr  = await loop.run_in_executor(
            None, _calc_atr_sync, trade['symbol'], 14, TRAILING_TIMEFRAME
        )

        if not atr:
            return None

        direction = trade['direction']
        entry     = trade['entry_price']

        if direction == 'LONG':
            stop = entry - atr * ATR_MULT
        else:
            stop = entry + atr * ATR_MULT

        self._stops[trade['id']] = stop
        self._atrs[trade['id']]  = atr

        await self._persist_stop(trade['id'], stop, atr)
        return stop

    def update_on_price(self, trade_id: int, price: float) -> bool:
        """
        Actualiza el trailing stop con el precio actual.
        Retorna True si el precio tocó el stop (señal de cierre).
        """
        if trade_id not in self._stops or trade_id not in self._atrs:
            return False

        stop = self._stops[trade_id]
        atr  = self._atrs[trade_id]

        # Necesitamos la dirección — asumimos LONG si stop < precio inicial
        # En producción esto viene del trade dict en _on_price
        return price <= stop  # placeholder, el caller valida dirección

    def update_trailing(self, trade_id: int, price: float,
                        direction: str) -> tuple[float, bool]:
        """
        Mueve el stop si el precio avanzó a favor.
        Retorna (nuevo_stop, tocó_stop).
        """
        if trade_id not in self._stops or trade_id not in self._atrs:
            return 0.0, False

        stop = self._stops[trade_id]
        atr  = self._atrs[trade_id]
        hit  = False

        if direction == 'LONG':
            new_stop = price - atr * ATR_MULT
            if new_stop > stop:              # solo subir, nunca bajar
                self._stops[trade_id] = new_stop
                stop = new_stop
            hit = price <= stop

        else:  # SHORT
            new_stop = price + atr * ATR_MULT
            if new_stop < stop:              # solo bajar, nunca subir
                self._stops[trade_id] = new_stop
                stop = new_stop
            hit = price >= stop

        return stop, hit

    def get_stop(self, trade_id: int) -> float | None:
        return self._stops.get(trade_id)

    def remove(self, trade_id: int) -> None:
        self._stops.pop(trade_id, None)
        self._atrs.pop(trade_id, None)

    async def _persist_stop(self, trade_id: int, stop: float, atr: float) -> None:
        """Guarda trailing_stop_price y atr_value en la DB."""
        import asyncio
        import sqlite3

        def _write():
            try:
                conn = sqlite3.connect(self._db_path)
                conn.execute(
                    "UPDATE trades SET trailing_stop_price=?, atr_value=? WHERE id=?",
                    (round(stop, 8), round(atr, 8), trade_id)
                )
                conn.commit()
                conn.close()
            except Exception as e:
                log.error(f"[trailing] Error persistiendo stop #{trade_id}: {e}")

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _write)
