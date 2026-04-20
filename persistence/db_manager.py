"""
persistence/db_manager.py
──────────────────────────
Extensión del esquema de executor.py para el motor async.

Añade las columnas:
  - trailing_stop_price REAL  → stop dinámico actual (actualizado en cada tick)
  - atr_value           REAL  → ATR(14) calculado al abrir la posición

También expone helpers async-safe (ejecutan sqlite en executor para no
bloquear el event loop).
"""

import asyncio
import logging
import os
import sqlite3
from functools import partial
from typing import Optional

log = logging.getLogger(__name__)

# Reutiliza el mismo path que executor.py
DB_PATH = os.path.join(os.getenv("DATA_DIR", "."), "trades.db")


# ── Migración ─────────────────────────────────────────────────

def migrate() -> None:
    """
    Agrega trailing_stop_price y atr_value a la tabla trades si no existen.
    Idempotente — se puede llamar en cada arranque.
    """
    conn = sqlite3.connect(DB_PATH)
    for col, typ in [("trailing_stop_price", "REAL"), ("atr_value", "REAL")]:
        try:
            conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {typ}")
            log.info(f"[DB] Columna {col} agregada a trades")
        except sqlite3.OperationalError:
            pass  # ya existe
    conn.commit()
    conn.close()


# ── Helpers síncronos (usados internamente) ───────────────────

def _get_open_trades_sync() -> list[dict]:
    """Retorna todas las posiciones OPEN con los campos necesarios para TrailingStop."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        """SELECT id, symbol, direction, entry_price, stop_loss, take_profit,
                  quantity, opened_at, trailing_stop_price, atr_value
           FROM trades WHERE status='OPEN'"""
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
            "opened_at":           r[7],
            "trailing_stop_price": r[8],
            "atr_value":           r[9],
        }
        for r in rows
    ]


def _close_trade_sync(trade_id: int, exit_price: float, result: str) -> None:
    """Marca un trade como cerrado. Espejo de executor.close_trade()."""
    from datetime import datetime
    conn  = sqlite3.connect(DB_PATH)
    trade = conn.execute(
        "SELECT direction, entry_price, quantity FROM trades WHERE id=?", (trade_id,)
    ).fetchone()
    if trade:
        direction, entry_price, quantity = trade
        if direction == "LONG":
            pnl = (exit_price - entry_price) * quantity
        else:
            pnl = (entry_price - exit_price) * quantity
        conn.execute(
            """UPDATE trades SET status=?, exit_price=?, pnl_usd=?, closed_at=?
               WHERE id=?""",
            (result, exit_price, round(pnl, 4), datetime.now().isoformat(), trade_id),
        )
        conn.commit()
    conn.close()


# ── Helpers async ─────────────────────────────────────────────

async def get_open_trades_async() -> list[dict]:
    """Versión async de _get_open_trades_sync."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _get_open_trades_sync)


async def close_trade_async(trade_id: int, exit_price: float, result: str) -> None:
    """Versión async de _close_trade_sync."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None, partial(_close_trade_sync, trade_id, exit_price, result)
    )
    log.info(f"[DB] Trade #{trade_id} cerrado: {result} @ {exit_price:.4f}")


async def migrate_async() -> None:
    """Versión async de migrate()."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, migrate)
