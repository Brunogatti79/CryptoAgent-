# =============================================================
#  CRYPTO AGENT — EXECUTOR
#  Ejecuta órdenes en Binance Testnet cuando hay señal accionable
#
#  v2: Position sizing por volatilidad (ATR-based risk)
#      + metadata por trade (signal_type, regime, atr)
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
 
# ── Sizing por riesgo ─────────────────────────────────────────
# RISK_PER_TRADE: fracción del capital que se arriesga por trade.
# Si el capital es $10,000 y RISK_PER_TRADE=0.01, se arriesgan $100.
# La posición se dimensiona para que la distancia al SL = ese riesgo.
# MAX_TRADE_USD sigue como cap absoluto de seguridad.
RISK_PER_TRADE = float(os.getenv('RISK_PER_TRADE', '0.01'))  # 1% default
 
# Capital inicial para el cálculo de balance desde DB
INITIAL_CAPITAL = float(os.getenv('INITIAL_CAPITAL', '10000'))
 
 
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
 
    # ── Migraciones v2: metadata para expectancy analysis ─────
    _migrations = [
        ("signal_type",     "TEXT"),
        ("regime_at_entry", "TEXT"),
        ("atr_at_entry",    "REAL"),
        ("mfe_price",       "REAL"),   # Max Favorable Excursion (mejor precio alcanzado)
        ("mae_price",       "REAL"),   # Max Adverse Excursion (peor precio alcanzado)
    ]
    for col, dtype in _migrations:
        try:
            conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {dtype}")
        except Exception:
            pass  # columna ya existe
 
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
         quantity, usd_value, order_id, status, opened_at, group_name,
         signal_type, regime_at_entry, atr_at_entry)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?)
    ''', (
        trade['symbol'], trade['direction'], trade['conviction'],
        trade['entry_price'], trade['stop_loss'], trade['take_profit'],
        trade['quantity'], trade['usd_value'], trade['order_id'],
        datetime.now().isoformat(), trade.get('group_name', 'A'),
        trade.get('signal_type'), trade.get('regime_at_entry'),
        trade.get('atr_at_entry'),
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
 
 
# ── Balance calculado desde DB ────────────────────────────────
 
def get_available_capital() -> float:
    """
    Calcula el capital disponible para sizing:
      capital_inicial + PnL realizado − exposición de posiciones abiertas.
 
    Esto es el capital "libre" que se puede usar para calcular riesgo.
    NO usa Binance API — es determinístico desde la DB.
    """
    conn = sqlite3.connect(DB_PATH)
 
    # PnL realizado total (trades cerrados)
    row = conn.execute(
        "SELECT COALESCE(SUM(pnl_usd), 0) FROM trades WHERE status IN ('WIN', 'LOSS')"
    ).fetchone()
    realized_pnl = float(row[0])
 
    # Exposición abierta (USD comprometidos en posiciones OPEN)
    row = conn.execute(
        "SELECT COALESCE(SUM(usd_value), 0) FROM trades WHERE status = 'OPEN'"
    ).fetchone()
    open_exposure = float(row[0])
 
    conn.close()
 
    available = INITIAL_CAPITAL + realized_pnl - open_exposure
    return max(available, 0)  # nunca negativo
 
 
# ── Cálculo de SL/TP ─────────────────────────────────────────
 
def _get_tp_multiplier(regime_info: dict | None,
                       signal_type: str | None = None,
                       change_24h: float = 0.0) -> tuple[float, str]:
    """
    Calcula el multiplicador de TP con 3 factores:
 
    1. BASE por signal_type (qué tan temprano estás en el movimiento):
         EMA_CROSS      → 2.5  (señal temprana, mayor recorrido esperado)
         RSI_RECOVERY/J → 2.0  (señal de reversión, recorrido medio)
         ALIGNMENT      → 1.75 (señal tardía, tendencia ya establecida)
 
    2. AJUSTE CONTINUO por calidad del régimen (sin saltos binarios):
         strength = f(persist_prob, bars_in_regime)
         - persist_prob aporta hasta +0.6  (interpolado desde 0.60 a 0.95)
         - bars_in_regime aporta hasta +0.3 (interpolado de 0 a 10 barras)
         → rango total del ajuste: 0 a +0.9
 
    3. DESCUENTO por sobre-extensión:
         Si |change_24h| > 10% → mult × 0.85
         (evita poner TP ambicioso en un techo/piso)
 
    Clamp final: [1.5, 3.0]
 
    Retorna: (multiplicador_tp, descripción_para_log)
    """
    # ── Paso 1: base por signal_type ──────────────────────────
    if signal_type == 'EMA_CROSS':
        base = 2.5
    elif signal_type in ('RSI_RECOVERY', 'RSI_REJECTION'):
        base = 2.0
    else:  # ALIGNMENT o desconocido
        base = 1.75
 
    # ── Paso 2: ajuste continuo por régimen ───────────────────
    strength = 0.0
    regime_desc = 'sin_régimen'
 
    if regime_info and regime_info.get('regime'):
        regime       = regime_info['regime']
        persist_prob = regime_info.get('persist_prob', 0.5)
        bars         = regime_info.get('bars_in_regime', 0)
 
        # Persistencia: interpolación lineal de 0.60→0 a 0.95→0.6
        # Por debajo de 0.60, no aporta nada (régimen muy inestable)
        persist_contrib = max(0, min((persist_prob - 0.60) / 0.35, 1.0)) * 0.6
 
        # Duración: interpolación lineal de 0→0 a 10 barras→0.3
        # Más de 10 barras (40h) ya no aporta extra
        duration_contrib = min(bars / 10.0, 1.0) * 0.3
 
        strength = persist_contrib + duration_contrib
 
        # En BEAR_TREND, reducir el ajuste (shorts tienen menos recorrido)
        if regime == 'BEAR_TREND':
            strength *= 0.7
 
        regime_desc = (
            f"{regime} persist={persist_prob:.0%} {bars}bar "
            f"str={strength:.2f}"
        )
 
    tp_mult = base + strength
 
    # ── Paso 3: anti sobre-extensión ──────────────────────────
    extension_discount = ''
    if abs(change_24h) > 10:
        tp_mult *= 0.85
        extension_discount = f' ext={change_24h:+.1f}%→×0.85'
 
    # ── Clamp final ───────────────────────────────────────────
    tp_mult = max(1.5, min(tp_mult, 3.0))
 
    reason = (
        f"{signal_type or 'ALIGN'}→base={base:.2f} "
        f"+ {regime_desc}"
        f"{extension_discount}"
        f" → {tp_mult:.2f}R"
    )
 
    return tp_mult, reason
 
 
def _calc_sl_tp(symbol: str, direction: str, entry: float,
                stop_pct: float, take_profit_signal: float,
                regime_info: dict | None = None,
                signal_type: str | None = None,
                change_24h: float = 0.0) -> tuple[float, float]:
    """
    Calcula SL/TP usando ATR(14) 4h — mismo timeframe que la señal de entrada.
 
    v4: TP dinámico con 3 factores (signal_type + regime quality + extensión).
      - SL siempre = ATR_4h × 1.5 (constante, define el riesgo)
      - TP varía continuamente:
          base por signal_type (1.75 a 2.5)
          + ajuste por calidad régimen (0 a +0.9)
          × descuento si sobre-extendido (×0.85)
          clamp [1.5R, 3.0R]
 
    Roles de timeframe en el sistema:
      - ATR 4h → SL/TP inicial (este cálculo) — coherente con la tesis de entrada
      - ATR 1h → trailing stop en runtime (main_async) — seguimiento fino del precio
 
    Fallback a porcentaje fijo si ATR no disponible.
    """
    from strategies.trailing_stop import calc_atr_multi, ATR_MULT
 
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
 
    # TP dinámico: signal_type + calidad régimen + extensión
    tp_mult, tp_reason = _get_tp_multiplier(regime_info, signal_type, change_24h)
 
    if atr_4h and atr_4h > 0:
        # ── SL/TP basado en ATR 4h ──────────────────────────────
        sl_distance = atr_4h * ATR_MULT
        tp_distance = atr_4h * ATR_MULT * tp_mult
 
        if direction == 'LONG':
            sl = entry - sl_distance
            tp = take_profit_signal if take_profit_signal > entry \
                 else entry + tp_distance
        else:
            sl = entry + sl_distance
            tp = take_profit_signal if 0 < take_profit_signal < entry \
                 else entry - tp_distance
 
        print(
            f"  [executor] ATR 4h={atr_4h:.4f} | ATR 1h={atr_1h:.4f if atr_1h else 'N/A'} | "
            f"ratio={ratio:.2f}x | TP {tp_reason} → SL={sl:.4f} TP={tp:.4f}"
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
        print(f"  [executor] ATR 4h no disponible — pct={pct:.1%} | TP {tp_reason} → SL={sl:.4f} TP={tp:.4f}")
 
    return round(sl, 8), round(tp, 8)
 
 
# ── Sizing por volatilidad ────────────────────────────────────
 
def _calc_position_size(symbol: str, current_price: float,
                        atr_4h: float | None, stop_pct: float | None) -> dict:
    """
    Calcula el tamaño de posición basado en riesgo por volatilidad.
 
    Lógica:
      1. risk_usd = capital_disponible × RISK_PER_TRADE (1% default)
      2. distance_to_sl = ATR_4h × 1.5  (o fallback a % fijo)
      3. quantity = risk_usd / distance_to_sl
      4. cap: nunca excede MAX_TRADE_USD
 
    Retorna:
      {quantity, usd_value, risk_usd, distance_sl, sizing_method, capped}
    """
    from strategies.trailing_stop import ATR_MULT
 
    capital   = get_available_capital()
    risk_usd  = capital * RISK_PER_TRADE
 
    # Distancia al SL (en precio, no en %)
    if atr_4h and atr_4h > 0:
        distance_sl   = atr_4h * ATR_MULT
        sizing_method = 'ATR'
    else:
        pct           = stop_pct or 0.04
        distance_sl   = current_price * pct
        sizing_method = 'PCT'
 
    # Quantity basada en riesgo
    if distance_sl <= 0:
        # Protección: si por alguna razón distance es 0, usar sizing fijo
        quantity_raw = MAX_TRADE_USD / current_price
        sizing_method = 'FIXED_FALLBACK'
    else:
        quantity_raw = risk_usd / distance_sl
 
    # USD value antes del cap
    usd_value_raw = quantity_raw * current_price
 
    # Cap: nunca exceder MAX_TRADE_USD (protección contra ATR muy bajo)
    capped = usd_value_raw > MAX_TRADE_USD
    if capped:
        quantity_raw  = MAX_TRADE_USD / current_price
        usd_value_raw = MAX_TRADE_USD
 
    return {
        'quantity':       quantity_raw,
        'usd_value':      round(usd_value_raw, 2),
        'risk_usd':       round(risk_usd, 2),
        'distance_sl':    round(distance_sl, 6),
        'capital':        round(capital, 2),
        'sizing_method':  sizing_method,
        'capped':         capped,
    }
 
 
# ── Ejecución principal ───────────────────────────────────────
 
def execute_signal(signal: dict, market_data: dict, stop_pct: float = None) -> dict | None:
    """
    Ejecuta una señal accionable en Binance.
 
    Sizing v2: posición dimensionada por riesgo (ATR-based).
      - risk_usd = 1% del capital disponible
      - quantity = risk_usd / distancia_al_SL
      - cap: MAX_TRADE_USD como límite de seguridad
 
    SL/TP calculado con ATR(14) 4h (coherente con la señal de entrada 4h).
    Trailing stop en runtime usa ATR 1h (ver main_async.py).
    Fallback a % fijo si ATR no disponible.
    Retorna dict con resultado o None si no se ejecutó.
    """
    init_db()
 
    symbol    = signal['symbol']
    direction = signal['direction']
 
    # Verificar que no haya posición abierta en este par específico
    if has_open_position(symbol):
        print(f"  [executor] Ya hay posición abierta en {symbol} — saltando")
        return None
 
    # Precio actual
    current_price = market_data.get(symbol, {}).get('price', 0)
    if not current_price:
        print(f"  [executor] Sin precio para {symbol} — abortando")
        return None
 
    # ── Pre-calcular ATR para sizing ──────────────────────────
    # Necesitamos el ATR ANTES de la orden para dimensionar la posición.
    # Después de la orden, _calc_sl_tp recalcula con entry_price real.
    from strategies.trailing_stop import calc_atr_multi
 
    atr_data = calc_atr_multi(symbol, period=14)
    atr_4h   = atr_data['atr_4h']
 
    # ── Position sizing por riesgo ────────────────────────────
    sizing = _calc_position_size(symbol, current_price, atr_4h, stop_pct)
    quantity_raw = sizing['quantity']
 
    print(
        f"  [executor] Sizing {symbol}: "
        f"capital=${sizing['capital']:.0f} | "
        f"riesgo=${sizing['risk_usd']:.2f} ({RISK_PER_TRADE:.0%}) | "
        f"distSL=${sizing['distance_sl']:.4f} | "
        f"size=${sizing['usd_value']:.2f} USD | "
        f"método={sizing['sizing_method']}"
        f"{' [CAPPED]' if sizing['capped'] else ''}"
    )
 
    # TP sugerido por Claude (referencia; puede ser reemplazado por ATR)
    take_profit_signal = parse_price(signal.get('take_profit', ''))
 
    try:
        exchange = get_exchange()
 
        # Redondear quantity según las reglas del par
        exchange.load_markets()
        market    = exchange.market(symbol)
        precision = market['precision']['amount']
        quantity  = exchange.amount_to_precision(symbol, quantity_raw)
 
        print(f"  [executor] Ejecutando {direction} {symbol} | qty: {quantity} | precio: ${current_price}")
 
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
 
        # SL/TP basado en ATR (se calcula con el entry_price real de la orden)
        # v4: TP dinámico con signal_type + calidad régimen + anti sobre-extensión
        regime_info = signal.get('regime_info')
        sig_type    = signal.get('signal_type')
        chg_24h     = market_data.get(symbol, {}).get('change_24h', 0)
        stop_loss, take_profit = _calc_sl_tp(
            symbol, direction, entry_price, stop_pct, take_profit_signal,
            regime_info=regime_info,
            signal_type=sig_type,
            change_24h=chg_24h,
        )
 
        # Guardar en DB (con metadata v2)
        trade_data = {
            'symbol':          symbol,
            'direction':       direction,
            'conviction':      signal['conviction'],
            'entry_price':     entry_price,
            'stop_loss':       stop_loss,
            'take_profit':     take_profit,
            'quantity':        float(quantity),
            'usd_value':       usd_value,
            'order_id':        order_id,
            'group_name':      signal.get('group_name', 'A'),
            'signal_type':     signal.get('signal_type'),
            'regime_at_entry': signal.get('regime_at_entry'),
            'atr_at_entry':    atr_4h,
        }
        trade_id = save_trade(trade_data)
 
        print(f"  [executor] Orden ejecutada — ID: {order_id} | Trade DB ID: {trade_id}")
 
        return {
            'trade_id':       trade_id,
            'order_id':       order_id,
            'symbol':         symbol,
            'direction':      direction,
            'entry_price':    entry_price,
            'stop_loss':      stop_loss,
            'take_profit':    take_profit,
            'quantity':       float(quantity),
            'usd_value':      usd_value,
            'sizing_method':  sizing['sizing_method'],
            'risk_usd':       sizing['risk_usd'],
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
 
 
def update_mfe_mae(trade_id: int, high: float, low: float) -> None:
    """
    Actualiza MFE/MAE de un trade abierto con el high/low del tick actual.
 
    Para LONG:
      MFE = max(mfe_actual, high)   → el precio más alto que alcanzó
      MAE = min(mae_actual, low)    → el precio más bajo que alcanzó
 
    Para SHORT:
      MFE = min(mfe_actual, low)    → el precio más bajo (favorable para short)
      MAE = max(mae_actual, high)   → el precio más alto (adverso para short)
 
    Se llama desde main_async._on_price() en cada tick del WebSocket.
    Es un UPDATE ligero (sin SELECT previo) usando MIN/MAX de SQL.
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        direction = conn.execute(
            "SELECT direction FROM trades WHERE id=? AND status='OPEN'",
            (trade_id,)
        ).fetchone()
 
        if not direction:
            conn.close()
            return
 
        direction = direction[0]
 
        if direction == 'LONG':
            # MFE = el high más alto visto; MAE = el low más bajo visto
            conn.execute("""
                UPDATE trades SET
                    mfe_price = CASE
                        WHEN mfe_price IS NULL THEN ?
                        WHEN ? > mfe_price THEN ?
                        ELSE mfe_price END,
                    mae_price = CASE
                        WHEN mae_price IS NULL THEN ?
                        WHEN ? < mae_price THEN ?
                        ELSE mae_price END
                WHERE id = ? AND status = 'OPEN'
            """, (high, high, high, low, low, low, trade_id))
        else:  # SHORT
            # MFE = el low más bajo visto; MAE = el high más alto visto
            conn.execute("""
                UPDATE trades SET
                    mfe_price = CASE
                        WHEN mfe_price IS NULL THEN ?
                        WHEN ? < mfe_price THEN ?
                        ELSE mfe_price END,
                    mae_price = CASE
                        WHEN mae_price IS NULL THEN ?
                        WHEN ? > mae_price THEN ?
                        ELSE mae_price END
                WHERE id = ? AND status = 'OPEN'
            """, (low, low, low, high, high, high, trade_id))
 
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"  [executor] update_mfe_mae ERROR #{trade_id}: {e}")
 
 
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
 
