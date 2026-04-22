# =============================================================
#  CRYPTO AGENT — EXECUTOR
#  Ejecuta órdenes en Binance Testnet cuando hay señal accionable
# =============================================================

import json
import os
import sqlite3
import ccxt
from datetime import datetime
from config import (
    BINANCE_API_KEY, BINANCE_API_SECRET, BINANCE_TESTNET,
    MAX_TRADE_USD, MAX_OPEN_POSITIONS
)

# Railway Volume en /data, fallback a directorio local
DB_PATH = os.path.join(os.getenv('DATA_DIR', '.'), 'trades.db')


# ── Conexión al exchange ──────────────────────────────────────

def get_exchange():
    exchange = ccxt.binance({
        'apiKey': BINANCE_API_KEY,
        'secret': BINANCE_API_SECRET,
        'options': {
            'defaultType': 'spot',
            'adjustForTimeDifference': True,
        },
    })
    if BINANCE_TESTNET:
        exchange.set_sandbox_mode(True)
    return exchange


# ── Base de datos SQLite ──────────────────────────────────────

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS trades (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol      TEXT,
            direction   TEXT,
            conviction  INTEGER,
            entry_price REAL,
            stop_loss   REAL,
            take_profit REAL,
            quantity    REAL,
            usd_value   REAL,
            order_id    TEXT,
            status      TEXT DEFAULT 'OPEN',
            exit_price  REAL,
            pnl_usd     REAL,
            opened_at   TEXT,
            closed_at   TEXT,
            group_name  TEXT DEFAULT 'A'
        )
    ''')
    # Migración: agregar group_name si no existe (DB preexistente)
    try:
        conn.execute("ALTER TABLE trades ADD COLUMN group_name TEXT DEFAULT 'A'")
    except Exception:
        pass

    conn.execute('''
        CREATE TABLE IF NOT EXISTS events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp  TEXT    NOT NULL,
            type       TEXT    NOT NULL,
            symbol     TEXT,
            group_name TEXT,
            level      TEXT    DEFAULT 'INFO',
            title      TEXT    NOT NULL,
            details    TEXT
        )
    ''')
    conn.commit()
    conn.close()


def log_event(type: str, title: str, symbol: str = None, group: str = None,
              level: str = 'INFO', details: dict = None) -> None:
    """Registra un evento en la tabla events."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            """INSERT INTO events (timestamp, type, symbol, group_name, level, title, details)
               VALUES (?,?,?,?,?,?,?)""",
            (datetime.now().isoformat(), type, symbol, group, level, title,
             json.dumps(details, default=str) if details else None)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"  [executor] log_event ERROR: {e}")


def get_events(limit: int = 100, offset: int = 0,
               type_filter: str = None, symbol_filter: str = None) -> list[dict]:
    """Retorna eventos ordenados por timestamp descendente."""
    conn  = sqlite3.connect(DB_PATH)
    where = []
    args  = []
    if type_filter:
        where.append("type = ?");   args.append(type_filter)
    if symbol_filter:
        where.append("symbol = ?"); args.append(symbol_filter)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        f"SELECT id,timestamp,type,symbol,group_name,level,title,details "
        f"FROM events {clause} ORDER BY id DESC LIMIT ? OFFSET ?",
        args + [limit, offset]
    ).fetchall()
    total = conn.execute(f"SELECT COUNT(*) FROM events {clause}", args).fetchone()[0]
    conn.close()
    result = []
    for r in rows:
        d = {'id':r[0],'timestamp':r[1],'type':r[2],'symbol':r[3],
             'group':r[4],'level':r[5],'title':r[6]}
        try:
            d['details'] = json.loads(r[7]) if r[7] else None
        except Exception:
            d['details'] = r[7]
        result.append(d)
    return result, total


def save_trade(trade: dict) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute('''
        INSERT INTO trades
        (symbol, direction, conviction, entry_price, stop_loss, take_profit,
         quantity, usd_value, order_id, status, opened_at, group_name)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?)
    ''', (
        trade['symbol'], trade['direction'], trade['conviction'],
        trade['entry_price'], trade['stop_loss'], trade['take_profit'],
        trade['quantity'], trade['usd_value'], trade['order_id'],
        datetime.now().isoformat(), trade.get('group_name', 'A')
    ))
    trade_id = cur.lastrowid
    conn.commit()
    conn.close()
    return trade_id


def get_open_trades() -> list:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("SELECT * FROM trades WHERE status = 'OPEN'")
    trades = cur.fetchall()
    conn.close()
    return trades


def count_open_trades() -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("SELECT COUNT(*) FROM trades WHERE status = 'OPEN'")
    count = cur.fetchone()[0]
    conn.close()
    return count


def has_open_position(symbol: str) -> bool:
    """Retorna True si el par ya tiene una posición abierta."""
    conn  = sqlite3.connect(DB_PATH)
    count = conn.execute(
        "SELECT COUNT(*) FROM trades WHERE status='OPEN' AND symbol=?", (symbol,)
    ).fetchone()[0]
    conn.close()
    return count > 0


def get_open_position(symbol: str) -> dict | None:
    """Retorna la posición abierta de un par, o None si no hay."""
    conn  = sqlite3.connect(DB_PATH)
    row   = conn.execute(
        """SELECT id, symbol, direction, entry_price, stop_loss, take_profit,
                  quantity, opened_at
           FROM trades WHERE status='OPEN' AND symbol=? LIMIT 1""",
        (symbol,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {
        "id": row[0], "symbol": row[1], "direction": row[2],
        "entry_price": row[3], "stop_loss": row[4], "take_profit": row[5],
        "quantity": row[6], "opened_at": row[7],
    }


def get_trade_by_id(trade_id: int) -> dict | None:
    """Retorna un trade OPEN por ID, o None si no existe o ya está cerrado."""
    conn = sqlite3.connect(DB_PATH)
    row  = conn.execute(
        """SELECT id, symbol, direction, entry_price, stop_loss, take_profit, quantity, opened_at
           FROM trades WHERE id=? AND status='OPEN'""",
        (trade_id,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {
        "id": row[0], "symbol": row[1], "direction": row[2],
        "entry_price": row[3], "stop_loss": row[4], "take_profit": row[5],
        "quantity": row[6], "opened_at": row[7],
    }


def market_close_trade(trade: dict, current_price: float, reason: str) -> dict:
    """
    Cierra un trade al precio de mercado (no espera stop/target).
    Usado para salidas por cambio de régimen u otras condiciones externas.
    """
    try:
        exchange = get_exchange()
        exchange.load_markets()
        quantity = exchange.amount_to_precision(trade["symbol"], trade["quantity"])
        side  = 'sell' if trade["direction"] == 'LONG' else 'buy'
        order = exchange.create_order(
            symbol=trade["symbol"], type='market', side=side, amount=float(quantity)
        )
        exit_price = float(order.get('average') or order.get('price') or current_price)
    except Exception as e:
        print(f"  [executor] ERROR cerrando mercado {trade['symbol']}: {e}")
        exit_price = current_price

    if trade["direction"] == 'LONG':
        pnl = (exit_price - trade["entry_price"]) * trade["quantity"]
    else:
        pnl = (trade["entry_price"] - exit_price) * trade["quantity"]

    result = 'WIN' if pnl >= 0 else 'LOSS'
    close_trade(trade["id"], exit_price, result)

    print(f"  [executor] Trade #{trade['id']} cerrado por {reason} | {result} | PnL ${pnl:.2f}")
    return {
        "trade_id":    trade["id"],
        "symbol":      trade["symbol"],
        "direction":   trade["direction"],
        "result":      result,
        "entry_price": trade["entry_price"],
        "exit_price":  exit_price,
        "pnl_usd":     round(pnl, 4),
        "reason":      reason,
    }


# ── Parsing de precios desde señal ───────────────────────────

def parse_price(value: str) -> float:
    """Extrae el primer número de strings como '$66,400 (en retroceso)'"""
    import re
    if not value or value == 'N/A':
        return 0.0
    nums = re.findall(r'[\d,]+\.?\d*', value.replace(',', ''))
    return float(nums[0]) if nums else 0.0


# ── Ejecución principal ───────────────────────────────────────

def _calc_sl_tp(symbol: str, direction: str, entry: float,
                stop_pct: float, take_profit_signal: float) -> tuple[float, float]:
    """
    Calcula SL/TP usando ATR(14) 4h — mismo timeframe que la señal de entrada.

    Roles de timeframe en el sistema:
      - ATR 4h → SL/TP inicial (este cálculo) — coherente con la tesis de entrada
      - ATR 1h → trailing stop en runtime (main_async) — seguimiento fino del precio

    Fallback a porcentaje fijo si ATR no disponible.
    """
    from strategies.trailing_stop import calc_atr_multi, ATR_MULT

    atr_data = calc_atr_multi(symbol, period=14)
    atr_4h   = atr_data['atr_4h']
    atr_1h   = atr_data['atr_1h']
    ratio    = atr_data['ratio']

    if atr_data['compressed']:
        print(
            f"  [executor] ⚠️  {symbol}: ATR comprimido — "
            f"ratio 4h/1h={ratio:.2f}x (esperado >1.5x) — "
            f"stops pueden ser menos confiables"
        )

    if atr_4h and atr_4h > 0:
        if direction == 'LONG':
            sl = entry - atr_4h * ATR_MULT
            tp = take_profit_signal if take_profit_signal > entry \
                 else entry + atr_4h * ATR_MULT * 2
        else:
            sl = entry + atr_4h * ATR_MULT
            tp = take_profit_signal if 0 < take_profit_signal < entry \
                 else entry - atr_4h * ATR_MULT * 2

        print(
            f"  [executor] ATR 4h={atr_4h:.4f} | ATR 1h={atr_1h:.4f if atr_1h else 'N/A'} | "
            f"ratio={ratio:.2f}x → SL={sl:.4f} TP={tp:.4f}"
        )
    else:
        pct = stop_pct or 0.04
        if direction == 'LONG':
            sl = entry * (1 - pct)
            tp = take_profit_signal if take_profit_signal > entry \
                 else entry * (1 + pct * 2)
        else:
            sl = entry * (1 + pct)
            tp = take_profit_signal if 0 < take_profit_signal < entry \
                 else entry * (1 - pct * 2)
        print(f"  [executor] ATR 4h no disponible — usando pct={pct:.1%} → SL={sl:.4f} TP={tp:.4f}")

    return round(sl, 8), round(tp, 8)


def execute_signal(signal: dict, market_data: dict, stop_pct: float = None,
                   max_trade_usd: float = None) -> dict | None:
    """
    Ejecuta una señal accionable en Binance.
    SL/TP calculado con ATR(14) 4h (coherente con la señal de entrada 4h).
    Trailing stop en runtime usa ATR 1h (ver main_async.py).
    Fallback a % fijo si ATR no disponible.

    max_trade_usd: override del tamaño máximo por operación (usado por Grupo C).
                   Si es None usa MAX_TRADE_USD de config.
    """
    init_db()

    symbol    = signal['symbol']
    direction = signal['direction']

    if has_open_position(symbol):
        print(f"  [executor] Ya hay posición abierta en {symbol} — saltando")
        return None

    current_price = market_data.get(symbol, {}).get('price', 0)
    if not current_price:
        print(f"  [executor] Sin precio para {symbol} — abortando")
        return None

    # Tamaño de la operación — override por grupo si se especifica
    trade_usd    = max_trade_usd if max_trade_usd else MAX_TRADE_USD
    quantity_raw = trade_usd / current_price

    take_profit_signal = parse_price(signal.get('take_profit', ''))

    try:
        exchange = get_exchange()
        exchange.load_markets()
        market    = exchange.market(symbol)
        precision = market['precision']['amount']
        quantity  = exchange.amount_to_precision(symbol, quantity_raw)

        print(f"  [executor] Ejecutando {direction} {symbol} | qty: {quantity} | precio: ${current_price} | usd: ${trade_usd}")

        side  = 'buy' if direction == 'LONG' else 'sell'
        order = exchange.create_order(
            symbol=symbol,
            type='market',
            side=side,
            amount=float(quantity),
        )

        entry_price = float(order.get('average') or order.get('price') or current_price)
        order_id    = str(order['id'])
        usd_value   = float(quantity) * entry_price

        stop_loss, take_profit = _calc_sl_tp(
            symbol, direction, entry_price, stop_pct, take_profit_signal
        )

        trade_data = {
            'symbol':      symbol,
            'direction':   direction,
            'conviction':  signal['conviction'],
            'entry_price': entry_price,
            'stop_loss':   stop_loss,
            'take_profit': take_profit,
            'quantity':    float(quantity),
            'usd_value':   usd_value,
            'order_id':    order_id,
            'group_name':  signal.get('group_name', 'A'),
        }
        trade_id = save_trade(trade_data)

        print(f"  [executor] Orden ejecutada — ID: {order_id} | Trade DB ID: {trade_id}")

        return {
            'trade_id':    trade_id,
            'order_id':    order_id,
            'symbol':      symbol,
            'direction':   direction,
            'entry_price': entry_price,
            'stop_loss':   stop_loss,
            'take_profit': take_profit,
            'quantity':    float(quantity),
            'usd_value':   usd_value,
        }

    except Exception as e:
        print(f"  [executor] ERROR ejecutando {symbol}: {e}")
        return None


def get_balance_usdt() -> float:
    """
    Calcula el balance USDT disponible real desde la DB.

    Fórmula:
        balance = INITIAL_CAPITAL_USD + PnL_realizado - USD_en_posiciones_abiertas

    Por qué no usamos fetch_balance() de Binance:
      - En testnet siempre devuelve ~$10,000 (no refleja nuestras operaciones)
      - En producción el saldo incluye monedas compradas que no son USDT disponible.
      - Este cálculo refleja exactamente cuánto USDT tenemos disponible para
        nuevas operaciones, basado en el historial de trades de la DB.
    """
    from config import INITIAL_CAPITAL_USD
    try:
        conn = sqlite3.connect(DB_PATH)

        row = conn.execute(
            "SELECT COALESCE(SUM(pnl_usd), 0) FROM trades WHERE status IN ('WIN','LOSS')"
        ).fetchone()
        pnl_realizado = float(row[0]) if row else 0.0

        row2 = conn.execute(
            "SELECT COALESCE(SUM(usd_value), 0) FROM trades WHERE status = 'OPEN'"
        ).fetchone()
        usd_en_vuelo = float(row2[0]) if row2 else 0.0

        conn.close()

        balance = INITIAL_CAPITAL_USD + pnl_realizado - usd_en_vuelo
        return round(max(balance, 0.0), 2)

    except Exception as e:
        print(f"  [executor] ERROR calculando balance desde DB: {e}")
        return 0.0


def get_exchange_balance_usdt() -> float:
    """
    Consulta el balance USDT directamente desde Binance.
    Útil para verificar manualmente, pero NO se usa para el dashboard
    porque en testnet siempre devuelve ~$10,000.
    """
    try:
        exchange = get_exchange()
        balance  = exchange.fetch_balance()
        return float(balance['free'].get('USDT', 0))
    except Exception as e:
        print(f"  [executor] ERROR obteniendo balance de Binance: {e}")
        return 0.0


def close_trade(trade_id: int, exit_price: float, result: str) -> None:
    """Marca un trade como cerrado en la DB con PnL calculado."""
    conn = sqlite3.connect(DB_PATH)
    trade = conn.execute(
        "SELECT direction, entry_price, quantity FROM trades WHERE id = ?", (trade_id,)
    ).fetchone()
    if trade:
        direction, entry_price, quantity = trade
        if direction == 'LONG':
            pnl_usd = (exit_price - entry_price) * quantity
        else:
            pnl_usd = (entry_price - exit_price) * quantity
        conn.execute(
            """UPDATE trades SET status=?, exit_price=?, pnl_usd=?, closed_at=?
               WHERE id=?""",
            (result, exit_price, round(pnl_usd, 4), datetime.now().isoformat(), trade_id)
        )
        conn.commit()
    conn.close()


def check_open_positions(market_data: dict) -> list[dict]:
    """
    Revisa todas las posiciones OPEN contra el precio actual.
    Cierra las que tocaron stop-loss o take-profit.
    Retorna lista de trades cerrados en este ciclo.
    """
    conn   = sqlite3.connect(DB_PATH)
    trades = conn.execute(
        "SELECT id, symbol, direction, entry_price, stop_loss, take_profit, quantity FROM trades WHERE status='OPEN'"
    ).fetchall()
    conn.close()

    closed = []
    for trade in trades:
        trade_id, symbol, direction, entry, stop, target, qty = trade
        price = market_data.get(symbol, {}).get('price', 0)
        if not price:
            continue

        result     = None
        exit_price = None

        if direction == 'LONG':
            if price <= stop:
                result, exit_price = 'LOSS', stop
            elif price >= target:
                result, exit_price = 'WIN', target
        else:  # SHORT
            if price >= stop:
                result, exit_price = 'LOSS', stop
            elif price <= target:
                result, exit_price = 'WIN', target

        if result:
            close_trade(trade_id, exit_price, result)
            pnl = (exit_price - entry) * qty if direction == 'LONG' else (entry - exit_price) * qty
            closed.append({
                'trade_id':    trade_id,
                'symbol':      symbol,
                'direction':   direction,
                'result':      result,
                'entry_price': entry,
                'exit_price':  exit_price,
                'pnl_usd':     round(pnl, 4),
            })
            print(f"  [executor] Trade #{trade_id} cerrado: {result} | {symbol} | PnL ${pnl:.2f}")

    return closed


def get_all_trades_stats() -> dict:
    """Retorna estadísticas globales de todos los trades."""
    conn  = sqlite3.connect(DB_PATH)
    rows  = conn.execute("SELECT status, pnl_usd FROM trades").fetchall()
    open_ = conn.execute("SELECT id, symbol, direction, entry_price, stop_loss, take_profit, opened_at FROM trades WHERE status='OPEN'").fetchall()
    conn.close()

    wins   = [r for r in rows if r[0] == 'WIN']
    losses = [r for r in rows if r[0] == 'LOSS']
    total  = len(wins) + len(losses)

    return {
        "total_closed": total,
        "wins":         len(wins),
        "losses":       len(losses),
        "open_count":   len(open_),
        "win_rate":     round(len(wins) / total * 100, 1) if total else 0,
        "total_pnl":    round(sum(r[1] or 0 for r in rows if r[0] in ('WIN', 'LOSS')), 2),
        "open_trades":  [
            {
                "id":          t[0], "symbol": t[1], "direction": t[2],
                "entry_price": t[3], "stop_loss": t[4], "take_profit": t[5],
                "opened_at":   t[6],
            }
            for t in open_
        ],
    }
