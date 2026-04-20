"""
main_async.py
─────────────
Motor de trading async — Fase 1.

Corre en PARALELO con main.py (no lo reemplaza todavía).
Responsabilidades exclusivas de este módulo:
  1. Binance WebSocket → precios en tiempo real (core/binance_ws.py)
  2. TrailingStop ATR-based → cierre automático de posiciones (strategies/trailing_stop.py)

La lógica de análisis (Claude + régimen HMM) sigue en main.py hasta Fase 2.

Arranque:
    python main_async.py          # solo trailing stop
    ASYNC_ENABLED=true             # necesario; si no está, este módulo no hace nada

Integración con main.py:
    main.py llama a asyncio.run(run_trailing_engine()) al final de su propio loop,
    o bien se lanza como proceso separado con Procfile.
"""

import asyncio
import logging
import os
import signal
import sys

# ── Inicializar logging antes de importar módulos propios ────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("main_async")

# ── Imports propios ──────────────────────────────────────────
from config import SYMBOLS
from core.binance_ws import BinanceWebSocket
from executor import (
    get_open_trades,
    close_trade,
    log_event,
    init_db,
    market_close_trade,
    get_trade_by_id,
)
from persistence.db_manager import migrate, get_open_trades_async, close_trade_async
from strategies.trailing_stop import TrailingStopManager

# ── Configuración ────────────────────────────────────────────
DB_PATH         = os.path.join(os.getenv("DATA_DIR", "."), "trades.db")
ASYNC_ENABLED   = os.getenv("ASYNC_ENABLED", "false").lower() == "true"

# Incluir Group B symbols si hay posiciones abiertas de cualquier símbolo
# El WS suscribe a todos los symbols de la DB + SYMBOLS base
# Se refresca cada vez que hay un nuevo trade


# ═══════════════════════════════════════════════════════════════
class TrailingEngine:
    """
    Orquesta WebSocket + TrailingStopManager.

    Ciclo de vida:
      start() → conecta WS, carga trades abiertos, inicializa stops
      _on_price() → callback del WS, actualiza stops, cierra si toca
      stop()  → desconecta limpiamente
    """

    def __init__(self):
        self._ts_manager: TrailingStopManager = TrailingStopManager(DB_PATH)
        self._ws: BinanceWebSocket | None = None
        self._initialized_ids: set[int] = set()

    async def start(self) -> None:
        """Arranca el motor. Bloquea hasta que se llame stop()."""
        log.info("[Engine] Iniciando TrailingEngine...")

        # 1. Migración de esquema
        migrate()
        log.info("[Engine] Migración DB OK")

        # 2. Cargar posiciones abiertas existentes
        await self._refresh_open_trades()

        # 3. Construir lista de símbolos a suscribir
        symbols = await self._get_all_watched_symbols()
        if not symbols:
            log.warning("[Engine] Sin símbolos para suscribir — esperando 60s")
            await asyncio.sleep(60)
            await self.start()
            return

        log.info(f"[Engine] Suscribiendo WS a: {symbols}")
        self._ws = BinanceWebSocket(symbols=symbols, reconnect_delay=2.0)

        # 4. Arrancar WS (bloquea)
        await self._ws.start(on_price=self._on_price)

    def stop(self) -> None:
        if self._ws:
            self._ws.stop()

    # ── Callbacks ────────────────────────────────────────────

    async def _on_price(self, symbol: str, price: float, high: float, low: float) -> None:
        """
        Recibe cada tick del WebSocket.
        1. Inicializa stops de trades recién abiertos que aún no tienen stop ATR.
        2. Llama update_on_price → si toca stop, cierra la posición.
        """
        # Buscar trades abiertos en este símbolo
        trades = _get_open_trades_for_symbol(symbol)
        for trade in trades:
            tid = trade["id"]

            # Inicializar stop si es la primera vez que vemos este trade
            if tid not in self._initialized_ids:
                stop = await self._ts_manager.initialize_stop(trade)
                if stop is not None:
                    self._initialized_ids.add(tid)
                    log.info(f"[Engine] Stop inicializado #{tid} {symbol} @ {stop:.4f}")
                continue  # el primer tick solo inicializa

            # Actualizar trailing y verificar si tocó
            hit = self._ts_manager.update_on_price(tid, price)
            if hit:
                await self._close_by_trailing(trade, price)

    # ── Cierre por trailing stop ──────────────────────────────

    async def _close_by_trailing(self, trade: dict, current_price: float) -> None:
        """Cierra un trade que tocó su trailing stop."""
        tid     = trade["id"]
        symbol  = trade["symbol"]
        stop    = self._ts_manager.get_stop(tid)

        log.info(f"[Engine] TRAILING STOP HIT #{tid} {symbol} @ {current_price:.4f} (stop={stop})")

        # Cerrar en la DB con precio actual (el stop fue tocado exactamente)
        exit_price = stop if stop else current_price
        direction  = trade["direction"]

        if direction == "LONG":
            pnl = (exit_price - trade["entry_price"]) * trade["quantity"]
        else:
            pnl = (trade["entry_price"] - exit_price) * trade["quantity"]

        result = "WIN" if pnl >= 0 else "LOSS"

        # Ejecutar en executor para no bloquear el event loop
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, close_trade, tid, exit_price, result)

        # Log en tabla events
        log_event(
            type="TRADE_CLOSE",
            title=f"Trailing Stop: {symbol} {direction} → {result}",
            symbol=symbol,
            level="SUCCESS" if result == "WIN" else "WARNING",
            details={
                "trade_id":    tid,
                "entry_price": trade["entry_price"],
                "exit_price":  round(exit_price, 6),
                "pnl_usd":     round(pnl, 4),
                "reason":      "TRAILING_STOP",
            },
        )

        # Limpiar estado
        self._ts_manager.remove(tid)
        self._initialized_ids.discard(tid)

        log.info(f"[Engine] Trade #{tid} cerrado | {result} | PnL ${pnl:.2f}")

    # ── Helpers ───────────────────────────────────────────────

    async def _refresh_open_trades(self) -> None:
        """Carga trades abiertos y restaura stops en memoria."""
        trades = await get_open_trades_async()
        self._ts_manager.load_open_trades(trades)
        for t in trades:
            self._initialized_ids.add(t["id"])
        log.info(f"[Engine] {len(trades)} posiciones abiertas cargadas")

    async def _get_all_watched_symbols(self) -> list[str]:
        """
        Combina SYMBOLS base + símbolos de posiciones abiertas en DB.
        Así el WS sigue recibiendo precios aunque el symbol no esté en SYMBOLS.
        """
        base = list(SYMBOLS)
        trades = await get_open_trades_async()
        for t in trades:
            if t["symbol"] not in base:
                base.append(t["symbol"])
        return base


# ── Helpers de acceso a DB (síncronos, llamados desde executor) ──

def _get_open_trades_for_symbol(symbol: str) -> list[dict]:
    """Retorna trades OPEN para un símbolo específico."""
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        """SELECT id, symbol, direction, entry_price, stop_loss, take_profit,
                  quantity, trailing_stop_price, atr_value
           FROM trades WHERE status='OPEN' AND symbol=?""",
        (symbol,),
    ).fetchall()
    conn.close()
    return [
        {
            "id":                  r[0],
            "symbol":              r[1],
            "direction":           r[2],
            "entry_price":         r[3],
            "stop_loss":           r[4],
            "take_profit":         r[5],
            "quantity":            r[6],
            "trailing_stop_price": r[7],
            "atr_value":           r[8],
        }
        for r in rows
    ]


# ═══════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════

async def run_trailing_engine() -> None:
    """Coroutine principal. Puede ser awaited desde main.py o correr standalone."""
    if not ASYNC_ENABLED:
        log.info("[Engine] ASYNC_ENABLED=false — motor async desactivado")
        return

    engine = TrailingEngine()

    # Manejo de señales para shutdown limpio
    loop = asyncio.get_event_loop()

    def _shutdown():
        log.info("[Engine] Señal de apagado recibida")
        engine.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except NotImplementedError:
            pass  # Windows no soporta add_signal_handler en todos los contextos

    await engine.start()


if __name__ == "__main__":
    init_db()  # asegura que la DB existe antes de migrar
    asyncio.run(run_trailing_engine())
