# =============================================================
#  CRYPTO AGENT — DATA
#  Obtiene precios e indicadores desde Binance public API
#  (sin key, sin rate limit estricto)
#
#  CAMBIO v3: conviction scoring mecánico en check_entry_conditions
#  Cada gate aporta puntos según margen de cumplimiento (no hardcoded 9).
# =============================================================
 
import requests
import pandas as pd
from datetime import datetime
 
 
def get_prices_and_indicators(symbols: list[str]) -> dict:
    results = {}
 
    for symbol in symbols:
        binance_symbol = symbol.replace("/", "")  # BTC/USDT → BTCUSDT
        try:
            # Precio actual + cambio 24h
            ticker_url = f"https://api.binance.com/api/v3/ticker/24hr?symbol={binance_symbol}"
            tr = requests.get(ticker_url, timeout=10).json()
            price      = float(tr["lastPrice"])
            change_24h = float(tr["priceChangePercent"])
 
            # Velas 4h — últimas 100 para EMA50, RSI14 y volumen
            klines_url = (
                f"https://api.binance.com/api/v3/klines"
                f"?symbol={binance_symbol}&interval=4h&limit=100"
            )
            klines = requests.get(klines_url, timeout=10).json()
            closes  = pd.Series([float(k[4]) for k in klines])
            volumes = pd.Series([float(k[5]) for k in klines])
 
            rsi_series = _calc_rsi_series(closes, period=14)
            rsi        = round(float(rsi_series.iloc[-1]), 1)
            ema20_s    = closes.ewm(span=20).mean()
            ema50_s    = closes.ewm(span=50).mean()
            ema20      = ema20_s.iloc[-1]
            ema50      = ema50_s.iloc[-1]
            trend      = "ALCISTA" if ema20 > ema50 else "BAJISTA"
            vol_ratio  = round(float(volumes.iloc[-1] / volumes.rolling(20).mean().iloc[-1]), 2)
            change_4h  = round((closes.iloc[-1] - closes.iloc[-2]) / closes.iloc[-2] * 100, 2)
 
            # ── Señales adicionales ──────────────────────────────
            # EMA Cross reciente: EMA20 cruzó EMA50 en las últimas 4 velas (no solo alineada)
            ema_cross_up   = any(
                ema20_s.iloc[-(i+2)] < ema50_s.iloc[-(i+2)] and
                ema20_s.iloc[-(i+1)] >= ema50_s.iloc[-(i+1)]
                for i in range(4)
            )
            ema_cross_down = any(
                ema20_s.iloc[-(i+2)] > ema50_s.iloc[-(i+2)] and
                ema20_s.iloc[-(i+1)] <= ema50_s.iloc[-(i+1)]
                for i in range(4)
            )
 
            # RSI Recovery: estuvo en oversold (<35) en las últimas 6 velas y ahora salió (>40)
            rsi_recovery = (
                any(rsi_series.iloc[-i] < 35 for i in range(1, 7)) and rsi > 40
            )
            # RSI Rejection: estuvo en overbought (>65) en las últimas 6 velas y ahora cayó (<60)
            rsi_rejection = (
                any(rsi_series.iloc[-i] > 65 for i in range(1, 7)) and rsi < 60
            )
 
            # ── Confirmación timeframe 1h ────────────────────────
            klines_1h_url = (
                f"https://api.binance.com/api/v3/klines"
                f"?symbol={binance_symbol}&interval=1h&limit=30"
            )
            klines_1h        = requests.get(klines_1h_url, timeout=10).json()
            closes_1h        = pd.Series([float(k[4]) for k in klines_1h])
            ema20_1h         = closes_1h.ewm(span=20).mean().iloc[-1]
            current_close    = closes_1h.iloc[-1]
            price_above_1h   = bool(current_close > ema20_1h)
            price_below_1h   = bool(current_close < ema20_1h)
 
            results[symbol] = {
                "price":               round(price, 2),
                "change_24h":          round(change_24h, 2),
                "change_4h":           change_4h,
                "rsi":                 rsi,
                "ema20":               round(ema20, 2),
                "ema50":               round(ema50, 2),
                "trend":               trend,
                "vol_ratio":           vol_ratio,
                "ema_cross_up":        ema_cross_up,
                "ema_cross_down":      ema_cross_down,
                "rsi_recovery":        rsi_recovery,
                "rsi_rejection":       rsi_rejection,
                # ── Confirmación 1h ──────────────────────────────
                "ema20_1h":            round(ema20_1h, 2),
                "price_above_ema20_1h": price_above_1h,
                "price_below_ema20_1h": price_below_1h,
            }
            cross = "🔼EMA" if ema_cross_up else ("🔽EMA" if ema_cross_down else "")
            recov = "↩RSI" if rsi_recovery else ""
            print(f"  [data] {symbol}: ${price:,.2f} | RSI {rsi} | {trend} | vol {vol_ratio}x {cross}{recov}")
 
        except Exception as e:
            print(f"  [data] ERROR {symbol}: {e}")
            results[symbol] = {"error": str(e)}
 
    return results
 
 
def get_fear_and_greed() -> dict:
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
        d = r.json()["data"][0]
        return {"value": int(d["value"]), "label": d["value_classification"]}
    except Exception as e:
        print(f"  [data] Fear&Greed ERROR: {e}")
        return {"value": 50, "label": "Neutral"}
 
 
def _calc_rsi_series(closes: pd.Series, period: int = 14) -> pd.Series:
    delta = closes.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, 1e-10)
    return 100 - (100 / (1 + rs))
 
 
def _calc_rsi(closes: pd.Series, period: int = 14) -> float:
    return float(_calc_rsi_series(closes, period).iloc[-1])
 
 
def get_prices_and_indicators_for(symbols: list[str]) -> dict:
    """Igual que get_prices_and_indicators pero solo para los símbolos indicados."""
    return get_prices_and_indicators(symbols)
 
 
def get_top_movers(symbols_a: list[str], n: int = 2,
                   min_change_pct: float = 8.0,
                   min_volume_usd: float = 50_000_000) -> list[dict]:
    """
    Escanea todos los pares USDT de Binance y devuelve los N con mayor
    movimiento absoluto en 24h, filtrando por volumen mínimo.
    Excluye stablecoins, tokens wrapped y los símbolos del Grupo A.
    """
    EXCLUDE = {'USDC','BUSD','DAI','TUSD','FDUSD','USDT','WBTC','WETH','WBNB'}
    group_a = {s.replace('/','') for s in symbols_a}
 
    try:
        tickers = requests.get(
            "https://api.binance.com/api/v3/ticker/24hr", timeout=15
        ).json()
    except Exception as e:
        print(f"  [data] get_top_movers ERROR: {e}")
        return []
 
    movers = []
    for t in tickers:
        sym = t.get('symbol','')
        if not sym.endswith('USDT'):
            continue
        base = sym[:-4]
        if base in EXCLUDE or sym in group_a:
            continue
        try:
            change = float(t['priceChangePercent'])
            volume = float(t['quoteVolume'])
            price  = float(t['lastPrice'])
        except Exception:
            continue
        if abs(change) >= min_change_pct and volume >= min_volume_usd and price > 0:
            movers.append({
                'symbol':     base + '/USDT',
                'change_24h': round(change, 2),
                'volume_usd': round(volume, 0),
                'price':      round(price, 6),
            })
 
    movers.sort(key=lambda x: abs(x['change_24h']), reverse=True)
    return movers[:n]
 
 
# ═══════════════════════════════════════════════════════════════
#  CONVICTION SCORING — Punto 2 fix
# ═══════════════════════════════════════════════════════════════
#
#  Cada gate aporta puntos según qué tan cómodamente se cumplió.
#  Escala 1-10 donde:
#    6 = pasó todos los gates con margen mínimo (señal válida pero débil)
#    7-8 = pasó con margen sólido (señal operativa estándar)
#    9-10 = señal fuerte con múltiples confirmaciones
#
#  Componentes del score:
#    Base por régimen:               +5 (BULL/BEAR habilitado)
#    Señal fuerte (EMA_CROSS/RSI_*): +1
#    RSI en zona óptima:             +1 (centro del rango, no bordes)
#    Volumen alto (>1.8x):           +1
#    Confirmación 1h holgada:        +1
#    Régimen maduro (>6 barras):     +1
#
#  Mínimo posible si califica: 5 (solo régimen)
#  Máximo: 10 (todas las condiciones con margen)
#  MIN_SIGNAL_CONVICTION=8 en config → filtra señales débiles
# ═══════════════════════════════════════════════════════════════
 
def _calc_conviction(direction: str, signal_type: str | None,
                     rsi: float, vol_ratio: float,
                     regime_info: dict, market_data_sym: dict,
                     rsi_min: float, rsi_max: float) -> tuple[int, list[str]]:
    """
    Calcula conviction score mecánico basado en calidad de la señal.
 
    Retorna: (score 1-10, lista de razones del scoring)
    """
    score = 5   # base: régimen operable
    details = ["base régimen operable: +5"]
 
    # +1 señal fuerte (EMA_CROSS, RSI_RECOVERY, RSI_REJECTION)
    if signal_type in ('EMA_CROSS', 'RSI_RECOVERY', 'RSI_REJECTION'):
        score += 1
        details.append(f"señal fuerte ({signal_type}): +1")
 
    # +1 RSI en zona óptima (centro del rango, no en los bordes)
    # "Centro" = dentro del 60% medio del rango permitido
    rsi_range = rsi_max - rsi_min
    rsi_center = rsi_min + rsi_range * 0.5
    rsi_margin = rsi_range * 0.3  # 30% a cada lado del centro
    if (rsi_center - rsi_margin) <= rsi <= (rsi_center + rsi_margin):
        score += 1
        details.append(f"RSI {rsi:.1f} en zona óptima [{rsi_center-rsi_margin:.0f}-{rsi_center+rsi_margin:.0f}]: +1")
 
    # +1 volumen alto (>1.8x promedio — claramente por encima del mínimo)
    if vol_ratio >= 1.8:
        score += 1
        details.append(f"volumen {vol_ratio:.1f}x alto (>1.8): +1")
 
    # +1 confirmación 1h holgada (precio bien separado de EMA20 1h)
    ema20_1h = market_data_sym.get('ema20_1h', 0)
    price    = market_data_sym.get('price', 0)
    if ema20_1h and price:
        pct_from_ema = abs(price - ema20_1h) / ema20_1h * 100
        correct_side = (
            (direction == 'LONG'  and price > ema20_1h) or
            (direction == 'SHORT' and price < ema20_1h)
        )
        if correct_side and pct_from_ema >= 0.3:
            score += 1
            details.append(f"1h holgada ({pct_from_ema:.2f}% desde EMA20 1h): +1")
 
    # +1 régimen maduro (>6 barras = >24h en el régimen actual)
    bars = regime_info.get('bars_in_regime', 0)
    if bars and bars > 6:
        score += 1
        details.append(f"régimen maduro ({bars} barras, >{bars*4}h): +1")
 
    # Clampar a 10
    score = min(score, 10)
 
    return score, details
 
 
def check_entry_conditions(symbol: str, market_data: dict, regime_info: dict) -> dict:
    """
    Filtro mecánico de entrada para Grupo A. Todas las condiciones deben cumplirse.
 
    CAMBIO v3: retorna conviction score real (no hardcoded 9).
 
    Condiciones:
      1. Régimen HMM en BULL_TREND (→ LONG) o BEAR_TREND (→ SHORT)
      2. EMA20/EMA50 alineada con el régimen
      3. RSI en zona neutral — no sobrecomprado ni sobrevendido en la entrada
      4. Volumen ≥ 1.2/1.3× promedio 20 períodos
      5. Confirmación timeframe 1h (excepto EMA_CROSS)
 
    Retorna: {qualified, direction, conviction, conviction_details, reasons, blockers}
    """
    blockers: list[str] = []
    reasons:  list[str] = []
 
    d = market_data.get(symbol, {})
    if d.get('error'):
        return {'qualified': False, 'direction': None, 'conviction': 0,
                'reasons': [], 'blockers': [f'error de datos: {d["error"]}']}
 
    regime        = regime_info.get('regime')  if regime_info and regime_info.get('available') else None
    rsi           = float(d.get('rsi',            50.0))
    trend         = d.get('trend',         '')
    vol_ratio     = float(d.get('vol_ratio',       1.0))
    ema_cross_up  = d.get('ema_cross_up',   False)
    ema_cross_down= d.get('ema_cross_down', False)
    rsi_recovery  = d.get('rsi_recovery',   False)
    rsi_rejection = d.get('rsi_rejection',  False)
 
    # 1. Régimen operable
    if regime == 'BULL_TREND':
        direction = 'LONG'
        reasons.append('régimen BULL_TREND ✓')
    elif regime == 'BEAR_TREND':
        direction = 'SHORT'
        reasons.append('régimen BEAR_TREND ✓')
    else:
        blockers.append(f'régimen {regime or "DESCONOCIDO"} — sin tendencia clara')
        return {'qualified': False, 'direction': None, 'conviction': 0,
                'reasons': reasons, 'blockers': blockers, 'signal_type': None}
 
    # 2. Detectar tipo de señal
    signal_type = None
    if direction == 'LONG'  and ema_cross_up:
        signal_type = 'EMA_CROSS'
        reasons.append('🔼 EMA20 cruzó EMA50 recientemente ✓✓')
    elif direction == 'SHORT' and ema_cross_down:
        signal_type = 'EMA_CROSS'
        reasons.append('🔽 EMA20 cruzó EMA50 recientemente ✓✓')
    elif direction == 'LONG'  and rsi_recovery:
        signal_type = 'RSI_RECOVERY'
        reasons.append('↩ RSI salió de oversold ✓✓')
    elif direction == 'SHORT' and rsi_rejection:
        signal_type = 'RSI_REJECTION'
        reasons.append('↩ RSI salió de overbought ✓✓')
 
    # 3. EMA alineada (siempre requerida)
    if direction == 'LONG' and trend == 'ALCISTA':
        reasons.append('EMA20 > EMA50 ✓')
    elif direction == 'SHORT' and trend == 'BAJISTA':
        reasons.append('EMA20 < EMA50 ✓')
    else:
        if signal_type != 'EMA_CROSS':
            blockers.append(f'EMA {trend} no alinea con {direction}')
 
    # 4. RSI — rango más amplio si hay señal fuerte
    rsi_max_long  = 72 if signal_type in ('EMA_CROSS',)        else 65
    rsi_min_long  = 35 if signal_type in ('RSI_RECOVERY',)     else 42
    rsi_min_short = 28 if signal_type in ('EMA_CROSS',)        else 35
    rsi_max_short = 65 if signal_type in ('RSI_REJECTION',)    else 58
 
    if direction == 'LONG':
        rsi_min, rsi_max = rsi_min_long, rsi_max_long
        if rsi_min <= rsi <= rsi_max:
            reasons.append(f'RSI {rsi:.1f} ✓')
        elif rsi > rsi_max:
            blockers.append(f'RSI {rsi:.1f} sobrecomprado')
        else:
            blockers.append(f'RSI {rsi:.1f} débil para LONG')
    else:
        rsi_min, rsi_max = rsi_min_short, rsi_max_short
        if rsi_min <= rsi <= rsi_max:
            reasons.append(f'RSI {rsi:.1f} ✓')
        elif rsi < rsi_min:
            blockers.append(f'RSI {rsi:.1f} sobrevendido')
        else:
            blockers.append(f'RSI {rsi:.1f} alto para SHORT')
 
    # 5. Volumen
    vol_min = 1.2 if signal_type else 1.3
    if vol_ratio >= vol_min:
        reasons.append(f'volumen {vol_ratio:.1f}x ✓')
    else:
        blockers.append(f'volumen {vol_ratio:.1f}x bajo (mín {vol_min}×)')
 
    # 6. Confirmación timeframe 1h
    if signal_type != 'EMA_CROSS':
        price_above_1h = d.get('price_above_ema20_1h')
        price_below_1h = d.get('price_below_ema20_1h')
 
        if price_above_1h is None:
            reasons.append('confirmación 1h sin datos (omitida)')
        elif direction == 'LONG':
            if price_above_1h:
                reasons.append('precio > EMA20 1h ✓')
            else:
                blockers.append('precio bajo EMA20 1h — sin confirmación intradiaria')
        else:
            if price_below_1h:
                reasons.append('precio < EMA20 1h ✓')
            else:
                blockers.append('precio sobre EMA20 1h — sin confirmación intradiaria')
    else:
        reasons.append('confirmación 1h omitida (EMA_CROSS es suficiente)')
 
    qualified = len(blockers) == 0
 
    # ── Conviction scoring (v3) ──────────────────────────────
    # Solo se calcula si calificó — no tiene sentido scorear una señal bloqueada
    conviction = 0
    conviction_details = []
    if qualified:
        conviction, conviction_details = _calc_conviction(
            direction=direction,
            signal_type=signal_type,
            rsi=rsi,
            vol_ratio=vol_ratio,
            regime_info=regime_info,
            market_data_sym=d,
            rsi_min=rsi_min,
            rsi_max=rsi_max,
        )
 
    return {
        'qualified':          qualified,
        'direction':          direction if qualified else None,
        'signal_type':        signal_type or 'ALIGNMENT',
        'conviction':         conviction,
        'conviction_details': conviction_details,
        'reasons':            reasons,
        'blockers':           blockers,
    }
 
 
def format_market_context(market_data: dict, fng: dict) -> str:
    lines = [
        f"=== CONTEXTO DE MERCADO — {datetime.now().strftime('%Y-%m-%d %H:%M')} ===",
        f"Fear & Greed Index: {fng['value']}/100 ({fng['label']})",
        "",
    ]
    for symbol, d in market_data.items():
        if "error" in d:
            lines.append(f"{symbol}: ERROR — {d['error']}")
            continue
        lines += [
            f"--- {symbol} ---",
            f"  Precio:       ${d['price']:,.2f}",
            f"  Cambio 4h:    {d['change_4h']:+.2f}%",
            f"  Cambio 24h:   {d['change_24h']:+.2f}%",
            f"  RSI (14):     {d['rsi']}",
            f"  EMA20/50:     {d['ema20']} / {d['ema50']}  →  Tendencia {d['trend']}",
            f"  Volumen 4h:   {d['vol_ratio']}x promedio",
            "",
        ]
    return "\n".join(lines)
 
