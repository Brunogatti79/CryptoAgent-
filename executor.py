# =============================================================
#  CRYPTO AGENT — EXECUTOR
#  Ejecuta órdenes en Binance Testnet cuando hay señal accionable
#
#  v3: R:B adaptativo por régimen + re-entry cooldown
# =============================================================
 
import json
import os
import sqlite3
import ccxt
from datetime import datetime, timedelta
from config import (
    BINANCE_API_KEY, BINANCE_API_SECRET, BINANCE_TESTNET,
    MAX_TRADE_USD, MAX_OPEN_POSITIONS
)
 
# Railway Volume en /data, fallback a directorio local
DB_PATH = os.path.join(os.getenv('DATA_DIR', '.'), 'trades.db')
 
# ── Re-entry cooldown ────────────────────────────────────────
# Después de un stop-loss, esperar este tiempo antes de reabrir el mismo par.
# Sizing reducido al 50% en la re-entrada.
REENTRY_COOLDOWN_HOURS = 8
REENTRY_SIZE_MULT      = 0.5   # 50% del sizing normal
 
 
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
    # Migración v3: agregar columnas para trailing stop
    for col, default in [("trailing_stop_price", "NULL"), ("atr_value", "NULL")]:
        try:
            conn.execute(f"ALTER TABLE trades ADD COLUMN {col} REAL DEFAULT {default}")
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
 
 
# ── Re-entry cooldown (v3) ───────────────────────────────────
 
def _check_reentry_cooldown(symbol: str) -> dict:
    """
    Verifica si el par está en cooldown después de un stop-loss reciente.
 
    Retorna:
        {
            'allowed': bool,       — True si puede abrir
            'is_reentry': bool,    — True si es re-entrada (sizing reducido)
            'cooldown_until': str, — timestamp hasta cuándo hay cooldown (si aplica)
            'last_loss_ago_h': float — horas desde el último SL
        }
    """
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        """SELECT closed_at FROM trades
           WHERE symbol=? AND status='LOSS' AND closed_at IS NOT NULL
           ORDER BY closed_at DESC LIMIT 1""",
        (symbol,)
    ).fetchone()
    conn.close()
 
    if not row or not row[0]:
        return {'allowed': True, 'is_reentry': False,
                'cooldown_until': None, 'last_loss_ago_h': None}
 
    try:
        last_loss_time = datetime.fromisoformat(row[0])
    except (ValueError, TypeError):
        return {'allowed': True, 'is_reentry': False,
                'cooldown_until': None, 'last_loss_ago_h': None}
 
    now = datetime.now()
    hours_since = (now - last_loss_time).total_seconds() / 3600
    cooldown_end = last_loss_time + timedelta(hours=REENTRY_COOLDOWN_HOURS)
 
    if hours_since < REENTRY_COOLDOWN_HOURS:
        # Todavía en cooldown — NO permitir
        return {
            'allowed': False,
            'is_reentry': True,
            'cooldown_until': cooldown_end.isoformat(),
            'last_loss_ago_h': round(hours_since, 1),
        }
    elif hours_since < REENTRY_COOLDOWN_HOURS * 3:
        # Pasó el cooldown pero es reciente — permitir con sizing reducido
        return {
            'allowed': True,
            'is_reentry': True,
            'cooldown_until': None,
            'last_loss_ago_h': round(hours_since, 1),
        }
    else:
        # Más de 24h desde el último SL — operación normal
        return {
            'allowed': True,
            'is_reentry': False,
            'cooldown_until': None,
            'last_loss_ago_h': round(hours_since, 1),
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
        # Para cerrar un LONG vendemos, para cerrar un SHORT compramos
        side  = 'sell' if trade["direction"] == 'LONG' else 'buy'
        order = exchange.create_order(
            symbol=trade["symbol"], type='market', side=side, amount=float(quantity)
        )
        exit_price = float(order.get('average') or order.get('price') or current_price)
    except Exception as e:
        print(f"  [executor] ERROR cerrando mercado {trade['symbol']}: {e}")
        exit_price = current_price  # fallback: registrar al precio actual
 
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
                stop_pct: float, take_profit_signal: float,
                regime: str = None) -> tuple[float, float]:
    """
    Calcula SL/TP usando ATR(14) 4h — mismo timeframe que la señal de entrada.
 
    v3: R:B adaptativo por régimen.
      - BULL_TREND:  SL = ATR×1.5, TP = ATR×1.5×2.0 → ratio 2:1
      - BEAR_TREND:  SL = ATR×1.5, TP = ATR×1.5×3.0 → ratio 3:1
      - REVERSAL:    SL = ATR×1.5, TP = ATR×1.5×2.5 → ratio 2.5:1
 
    Roles de timeframe en el sistema:
      - ATR 4h → SL/TP inicial (este cálculo) — coherente con la tesis de entrada
      - ATR 1h → trailing stop en runtime (main_async) — seguimiento fino del precio
 
    Fallback a porcentaje fijo si ATR no disponible.
    """
    from strategies.trailing_stop import calc_atr_multi, ATR_MULT, REGIME_TP_MULT, DEFAULT_TP_MULT
 
    # Determinar multiplicador de TP según régimen
    tp_mult = REGIME_TP_MULT.get(regime, DEFAULT_TP_MULT) if regime else DEFAULT_TP_MULT
 
    # Calcular ATR en 4h y 1h simultáneamente
    atr_data = calc_atr_multi(symbol, period=14)
    atr_4h   = atr_data['atr_4h']
    atr_1h   = atr_data['atr_1h']
    ratio    = atr_data['ratio']
 
    # Warning si el mercado está comprimido (baja fiabilidad del ATR 4h)
    if atr_data['compressed']:
        print(
            f"  [executor] ⚠️  {symbol}: ATR comprimido — "
            f"ratio 4h/1h={ratio:.2f}x (esperado >1.5x) — "
            f"stops pueden ser menos confiables"
        )
 
    if atr_4h and atr_4h > 0:
        # ── SL/TP basado en ATR 4h con R:B adaptativo ────────
        if direction == 'LONG':
            sl = entry - atr_4h * ATR_MULT
            tp = take_profit_signal if take_profit_signal > entry \
                 else entry + atr_4h * ATR_MULT * tp_mult
        else:
            sl = entry + atr_4h * ATR_MULT
            tp = take_profit_signal if 0 < take_profit_signal < entry \
                 else entry - atr_4h * ATR_MULT * tp_mult
 
        regime_str = regime or 'UNKNOWN'
        print(
            f"  [executor] ATR 4h={atr_4h:.4f} | ATR 1h={atr_1h:.4f if atr_1h else 'N/A'} | "
            f"ratio={ratio:.2f}x | régimen={regime_str} → "
            f"R:B={tp_mult}:1 | SL={sl:.4f} TP={tp:.4f}"
        )
    else:
        # ── Fallback a porcentaje fijo ──────────────────────────
        pct = stop_pct or 0.04
        if direction == 'LONG':
            sl = entry * (1 - pct)
            tp = take_profit_signal if take_profit_signal > entry \
                 else entry * (1 + pct * tp_mult)
        else:
            sl = entry * (1 + pct)
            tp = take_profit_signal if 0 < take_profit_signal < entry \
                 else entry * (1 - pct * tp_mult)
        print(f"  [executor] ATR 4h no disponible — usando pct={pct:.1%} R:B={tp_mult}:1 → SL={sl:.4f} TP={tp:.4f}")
 
    return round(sl, 8), round(tp, 8)
 
 
def execute_signal(signal: dict, market_data: dict, stop_pct: float = None,
                   regime: str = None) -> dict | None:
    """
    Ejecuta una señal accionable en Binance.
 
    v3:
      - SL/TP con R:B adaptativo por régimen
      - Re-entry cooldown: 8h después de SL, sizing 50%
      - Conviction funcional desde Haiku confidence score
 
    Retorna dict con resultado o None si no se ejecutó.
    """
    init_db()
 
    symbol    = signal['symbol']
    direction = signal['direction']
 
    # Verificar que no haya posición abierta en este par específico
    if has_open_position(symbol):
        print(f"  [executor] Ya hay posición abierta en {symbol} — saltando")
        return None
 
    # ── Re-entry cooldown check (v3) ──────────────────────────
    reentry = _check_reentry_cooldown(symbol)
    if not reentry['allowed']:
        print(
            f"  [executor] {symbol} en cooldown post-SL — "
            f"faltan {REENTRY_COOLDOWN_HOURS - reentry['last_loss_ago_h']:.1f}h "
            f"(hasta {reentry['cooldown_until']})"
        )
        log_event("REENTRY_BLOCKED",
                  f"{symbol} bloqueado — cooldown post-SL ({reentry['last_loss_ago_h']:.1f}h de {REENTRY_COOLDOWN_HOURS}h)",
                  symbol=symbol, level="WARNING",
                  details=reentry)
        return None
 
    # Precio actual
    current_price = market_data.get(symbol, {}).get('price', 0)
    if not current_price:
        print(f"  [executor] Sin precio para {symbol} — abortando")
        return None
 
    # ── Sizing: normal o reducido por re-entry (v3) ──────────
    trade_usd = MAX_TRADE_USD
    if reentry['is_reentry']:
        trade_usd = MAX_TRADE_USD * REENTRY_SIZE_MULT
        print(
            f"  [executor] {symbol}: re-entrada post-SL — "
            f"sizing reducido ${trade_usd:.0f} (×{REENTRY_SIZE_MULT})"
        )
        log_event("REENTRY_REDUCED",
                  f"{symbol} re-entrada con sizing {REENTRY_SIZE_MULT*100:.0f}% (${trade_usd:.0f})",
                  symbol=symbol, level="INFO",
                  details=reentry)
 
    quantity_raw = trade_usd / current_price
 
    # TP sugerido por Claude (referencia; puede ser reemplazado por ATR)
    take_profit_signal = parse_price(signal.get('take_profit', ''))
 
    try:
        exchange = get_exchange()
 
        # Redondear quantity según las reglas del par
        exchange.load_markets()
        market    = exchange.market(symbol)
        precision = market['precision']['amount']
        quantity  = exchange.amount_to_precision(symbol, quantity_raw)
 
        print(f"  [executor] Ejecutando {direction} {symbol} | qty: {quantity} | precio: ${current_price} | usd: ${trade_usd:.0f}")
 
        # Orden de mercado
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
 
        # SL/TP basado en ATR con R:B adaptativo por régimen (v3)
        stop_loss, take_profit = _calc_sl_tp(
            symbol, direction, entry_price, stop_pct, take_profit_signal,
            regime=regime
        )
 
        # Guardar en DB
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
    """Retorna el balance de USDT disponible."""
    try:
        exchange = get_exchange()
        balance  = exchange.fetch_balance()
        return float(balance['free'].get('USDT', 0))
    except Exception as e:
        print(f"  [executor] ERROR obteniendo balance: {e}")
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
 
