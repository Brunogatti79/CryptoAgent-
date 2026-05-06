# =============================================================
#  BACKTEST — CALIBRACIÓN DE UMBRALES POR RÉGIMEN
#  
#  Objetivo: determinar empíricamente los mejores umbrales de
#  RSI y volumen para cada combinación de régimen + signal_type,
#  en lugar de usar valores arbitrarios hardcodeados.
#
#  Uso:
#    python backtest_regimen.py
#
#  Requiere: modelos HMM en ./models/hmm_BTCUSDT.pkl etc.
#  Descarga datos históricos directamente de Binance (sin API key).
# =============================================================

import os
import sys
import json
import itertools
import requests
import numpy as np
import pandas as pd
from datetime import datetime, timedelta

# ── Configuración ─────────────────────────────────────────────

SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]

# Período de backtest — cuántas velas 4h hacia atrás
# 1000 velas = ~166 días (~5.5 meses). Máx Binance sin paginación = 1000.
# Con paginación llegamos a 18 meses (~2000 velas).
LOOKBACK_BARS = 2000

# ATR multiplier para SL/TP (igual que en producción)
ATR_MULT   = 1.5
ATR_PERIOD = 14

# Cuántas barras adelante mirar para evaluar resultado del trade
FORWARD_BARS = 6   # 6 barras × 4h = 24h para que la tesis se desarrolle

# Grilla de umbrales a testear
RSI_MIN_LONG_GRID  = [32, 35, 38, 42, 45]
RSI_MAX_LONG_GRID  = [60, 65, 68, 72, 75]
RSI_MIN_SHORT_GRID = [25, 28, 32, 35]
RSI_MAX_SHORT_GRID = [55, 58, 62, 65]
VOL_MIN_GRID       = [1.0, 1.1, 1.2, 1.3, 1.5]

MODELS_DIR = os.path.join(os.path.dirname(__file__), "models")


# ── Fetch histórico con paginación ───────────────────────────

def fetch_candles_4h(symbol: str, total_bars: int = 2000) -> pd.DataFrame:
    """
    Descarga velas 4h históricas de Binance con paginación.
    Binance devuelve máx 1000 por request → paginamos si hace falta.
    """
    binance_symbol = symbol.replace("/", "")
    all_klines = []
    end_time   = None  # empieza desde ahora y va hacia atrás

    print(f"  Descargando {total_bars} velas 4h para {symbol}...")

    while len(all_klines) < total_bars:
        limit  = min(1000, total_bars - len(all_klines))
        url    = (
            f"https://api.binance.com/api/v3/klines"
            f"?symbol={binance_symbol}&interval=4h&limit={limit}"
        )
        if end_time:
            url += f"&endTime={end_time}"

        try:
            resp   = requests.get(url, timeout=15)
            resp.raise_for_status()
            klines = resp.json()
        except Exception as e:
            print(f"  ERROR fetch {symbol}: {e}")
            break

        if not klines:
            break

        all_klines = klines + all_klines
        end_time   = int(klines[0][0]) - 1  # una ms antes del primer timestamp

        if len(klines) < limit:
            break  # no hay más datos

    if not all_klines:
        return pd.DataFrame()

    df = pd.DataFrame(all_klines, columns=[
        "timestamp", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore",
    ])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df.set_index("timestamp", inplace=True)
    df = df[~df.index.duplicated(keep="last")].sort_index()

    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)

    print(f"  → {len(df)} velas descargadas ({df.index[0].date()} → {df.index[-1].date()})")
    return df[["open", "high", "low", "close", "volume"]]


# ── HMM: features e inferencia ───────────────────────────────

def compute_features(df: pd.DataFrame) -> tuple[np.ndarray, pd.Index]:
    """Idéntico a regime.py — DEBE mantenerse sincronizado."""
    d = df.copy()
    d["log_ret"]     = np.log(d["close"] / d["close"].shift(1))
    d["vol_20"]      = d["log_ret"].rolling(20).std()
    vol_ma           = d["volume"].rolling(20).mean().replace(0, 1e-10)
    d["vol_ratio"]   = d["volume"] / vol_ma

    delta            = d["close"].diff()
    gain             = delta.clip(lower=0).rolling(14).mean()
    loss             = (-delta.clip(upper=0)).rolling(14).mean().replace(0, 1e-10)
    d["rsi_c"]       = (100 - 100 / (1 + gain / loss) - 50) / 50

    d["ema50"]       = d["close"].ewm(span=50).mean()
    d["ema50_slope"] = d["ema50"].diff(3) / d["ema50"]

    d.dropna(inplace=True)
    cols = ["log_ret", "vol_20", "vol_ratio", "rsi_c", "ema50_slope"]
    return d[cols].values, d.index


def load_hmm_model(symbol: str):
    """Carga modelo HMM desde disco. Retorna (model, scaler, labels) o None."""
    binance_symbol = symbol.replace("/", "")
    path = os.path.join(MODELS_DIR, f"hmm_{binance_symbol}.pkl")
    if not os.path.exists(path):
        print(f"  WARN: modelo no encontrado en {path}")
        return None, None, None
    try:
        import joblib
        bundle = joblib.load(path)
        return bundle["model"], bundle["scaler"], bundle["labels"]
    except Exception as e:
        print(f"  ERROR cargando modelo {symbol}: {e}")
        return None, None, None


def classify_regimes(df: pd.DataFrame, model, scaler, labels: dict) -> pd.Series:
    """
    Aplica el HMM sobre el DataFrame completo y retorna
    una Serie con el nombre del régimen para cada barra.
    """
    X, idx = compute_features(df)
    X_scaled = scaler.transform(X)
    states   = model.predict(X_scaled)
    regimes  = pd.Series(
        [labels.get(int(s), f"STATE_{s}") for s in states],
        index=idx,
        name="regime"
    )
    return regimes


# ── Indicadores técnicos sobre histórico completo ────────────

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Calcula todos los indicadores que usa check_entry_conditions
    sobre el DataFrame completo (vectorizado, sin loops).
    """
    d = df.copy()

    # RSI 14
    delta = d["close"].diff()
    gain  = delta.clip(lower=0).rolling(14).mean()
    loss  = (-delta.clip(upper=0)).rolling(14).mean().replace(0, 1e-10)
    d["rsi"] = 100 - (100 / (1 + gain / loss))

    # EMAs
    d["ema20"] = d["close"].ewm(span=20).mean()
    d["ema50"] = d["close"].ewm(span=50).mean()
    d["trend"] = np.where(d["ema20"] > d["ema50"], "ALCISTA", "BAJISTA")

    # Volumen relativo
    d["vol_ratio"] = d["volume"] / d["volume"].rolling(20).mean()

    # EMA Cross — cruce en las últimas 4 velas
    ema_cross_up   = []
    ema_cross_down = []
    ema20 = d["ema20"].values
    ema50 = d["ema50"].values
    for i in range(len(d)):
        cu = cd = False
        for j in range(1, 5):
            if i - j - 1 >= 0 and i - j >= 0:
                if ema20[i-j-1] < ema50[i-j-1] and ema20[i-j] >= ema50[i-j]:
                    cu = True
                if ema20[i-j-1] > ema50[i-j-1] and ema20[i-j] <= ema50[i-j]:
                    cd = True
        ema_cross_up.append(cu)
        ema_cross_down.append(cd)
    d["ema_cross_up"]   = ema_cross_up
    d["ema_cross_down"] = ema_cross_down

    # RSI Recovery / Rejection
    rsi_vals = d["rsi"].values
    rsi_recovery  = []
    rsi_rejection = []
    for i in range(len(d)):
        start = max(0, i - 5)
        window = rsi_vals[start:i+1]
        rec  = any(v < 35 for v in window[:-1]) and rsi_vals[i] > 40 if len(window) > 1 else False
        rej  = any(v > 65 for v in window[:-1]) and rsi_vals[i] < 60 if len(window) > 1 else False
        rsi_recovery.append(rec)
        rsi_rejection.append(rej)
    d["rsi_recovery"]  = rsi_recovery
    d["rsi_rejection"] = rsi_rejection

    # ATR 14 (para calcular SL/TP en cada trade simulado)
    prev_close = d["close"].shift(1)
    tr = pd.concat([
        d["high"] - d["low"],
        (d["high"] - prev_close).abs(),
        (d["low"]  - prev_close).abs(),
    ], axis=1).max(axis=1)
    d["atr"] = tr.rolling(ATR_PERIOD).mean()

    return d


# ── Simulación de señal con umbrales variables ────────────────

def check_entry(row: pd.Series, regime: str,
                rsi_min_long: float, rsi_max_long: float,
                rsi_min_short: float, rsi_max_short: float,
                vol_min: float) -> dict | None:
    """
    Versión parametrizada de check_entry_conditions.
    Retorna dict con direction y signal_type, o None si no califica.
    """
    rsi        = row["rsi"]
    trend      = row["trend"]
    vol_ratio  = row["vol_ratio"]
    cross_up   = row["ema_cross_up"]
    cross_down = row["ema_cross_down"]
    rsi_rec    = row["rsi_recovery"]
    rsi_rej    = row["rsi_rejection"]

    if pd.isna(rsi) or pd.isna(vol_ratio):
        return None

    # Gate 1: régimen
    if regime == "BULL_TREND":
        direction = "LONG"
    elif regime == "BEAR_TREND":
        direction = "SHORT"
    else:
        return None  # SIDEWAYS / REVERSAL / UNKNOWN no califican en este gate

    # Tipo de señal
    signal_type = None
    if direction == "LONG"  and cross_up:
        signal_type = "EMA_CROSS"
    elif direction == "SHORT" and cross_down:
        signal_type = "EMA_CROSS"
    elif direction == "LONG"  and rsi_rec:
        signal_type = "RSI_RECOVERY"
    elif direction == "SHORT" and rsi_rej:
        signal_type = "RSI_REJECTION"

    # Gate 2: EMA alineada (excepto EMA_CROSS)
    if signal_type != "EMA_CROSS":
        if direction == "LONG"  and trend != "ALCISTA":
            return None
        if direction == "SHORT" and trend != "BAJISTA":
            return None

    # Gate 3: RSI (umbrales parametrizados)
    if direction == "LONG":
        _rsi_min = rsi_min_long if signal_type == "RSI_RECOVERY" else rsi_min_long
        _rsi_max = rsi_max_long
        if not (_rsi_min <= rsi <= _rsi_max):
            return None
    else:
        _rsi_min = rsi_min_short
        _rsi_max = rsi_max_short if signal_type == "RSI_REJECTION" else rsi_max_short
        if not (_rsi_min <= rsi <= _rsi_max):
            return None

    # Gate 4: volumen
    _vol_min = vol_min * 0.9 if signal_type else vol_min  # leve relajación con señal fuerte
    if vol_ratio < _vol_min:
        return None

    return {"direction": direction, "signal_type": signal_type or "ALIGNMENT"}


def simulate_trade(df: pd.DataFrame, entry_idx: int,
                   direction: str, atr: float) -> dict:
    """
    Simula el resultado del trade desde entry_idx.
    SL = entry ± ATR * ATR_MULT
    TP = entry ± ATR * ATR_MULT * 2
    Evalúa las siguientes FORWARD_BARS velas.
    """
    if atr <= 0 or pd.isna(atr):
        return {"result": "SKIP", "pnl_pct": 0}

    entry = df["close"].iloc[entry_idx]
    if direction == "LONG":
        sl = entry - atr * ATR_MULT
        tp = entry + atr * ATR_MULT * 2
    else:
        sl = entry + atr * ATR_MULT
        tp = entry - atr * ATR_MULT * 2

    end_idx = min(entry_idx + FORWARD_BARS, len(df) - 1)

    for i in range(entry_idx + 1, end_idx + 1):
        high  = df["high"].iloc[i]
        low   = df["low"].iloc[i]
        close = df["close"].iloc[i]

        if direction == "LONG":
            if low <= sl:
                pnl_pct = (sl - entry) / entry * 100
                return {"result": "LOSS", "pnl_pct": round(pnl_pct, 4),
                        "exit_bar": i - entry_idx, "exit_price": sl}
            if high >= tp:
                pnl_pct = (tp - entry) / entry * 100
                return {"result": "WIN", "pnl_pct": round(pnl_pct, 4),
                        "exit_bar": i - entry_idx, "exit_price": tp}
        else:
            if high >= sl:
                pnl_pct = (entry - sl) / entry * 100
                return {"result": "LOSS", "pnl_pct": round(pnl_pct, 4),
                        "exit_bar": i - entry_idx, "exit_price": sl}
            if low <= tp:
                pnl_pct = (entry - tp) / entry * 100
                return {"result": "WIN", "pnl_pct": round(pnl_pct, 4),
                        "exit_bar": i - entry_idx, "exit_price": tp}

    # No tocó ni SL ni TP en FORWARD_BARS — cerrar al precio final
    final  = df["close"].iloc[end_idx]
    if direction == "LONG":
        pnl_pct = (final - entry) / entry * 100
    else:
        pnl_pct = (entry - final) / entry * 100
    result = "WIN" if pnl_pct > 0 else "LOSS"
    return {"result": result, "pnl_pct": round(pnl_pct, 4),
            "exit_bar": FORWARD_BARS, "exit_price": final}


# ── Backtest completo para un símbolo ────────────────────────

def run_backtest_symbol(symbol: str) -> pd.DataFrame:
    """
    Corre el backtest completo para un símbolo.
    Retorna DataFrame con todos los trades simulados.
    """
    print(f"\n{'='*60}")
    print(f"  BACKTEST: {symbol}")
    print(f"{'='*60}")

    # 1. Cargar modelo
    model, scaler, labels = load_hmm_model(symbol)
    if model is None:
        print(f"  Sin modelo HMM — salteando {symbol}")
        return pd.DataFrame()

    # 2. Descargar datos
    df = fetch_candles_4h(symbol, total_bars=LOOKBACK_BARS)
    if df.empty or len(df) < 100:
        print(f"  Insuficientes datos para {symbol}")
        return pd.DataFrame()

    # 3. Calcular indicadores
    df = compute_indicators(df)

    # 4. Clasificar régimen para cada barra
    regimes = classify_regimes(df, model, scaler, labels)
    df = df.join(regimes, how="left")
    df["regime"] = df["regime"].fillna("UNKNOWN")

    print(f"  Distribución de regímenes:")
    for r, cnt in df["regime"].value_counts().items():
        pct = cnt / len(df) * 100
        print(f"    {r:15s}: {cnt:4d} barras ({pct:.1f}%)")

    # 5. Grilla de umbrales — generar todas las combinaciones
    param_grid = list(itertools.product(
        RSI_MIN_LONG_GRID,
        RSI_MAX_LONG_GRID,
        RSI_MIN_SHORT_GRID,
        RSI_MAX_SHORT_GRID,
        VOL_MIN_GRID,
    ))
    print(f"\n  Grilla: {len(param_grid)} combinaciones de parámetros")

    # 6. Para cada barra, detectar señales con TODOS los parámetros a la vez
    #    Primero generamos el "mapa de señales" por barra para no recalcular indicadores
    print(f"  Simulando trades...")

    # Warmup: primeras 60 barras no las usamos (EMAs/RSI necesitan historia)
    WARMUP = 60
    results = []

    for params in param_grid:
        rsi_min_l, rsi_max_l, rsi_min_s, rsi_max_s, vol_min = params

        # Validar que los rangos tengan sentido
        if rsi_min_l >= rsi_max_l or rsi_min_s >= rsi_max_s:
            continue

        trades_this_param = []
        in_trade = False
        trade_end_idx = 0

        for i in range(WARMUP, len(df) - FORWARD_BARS):
            # No entrar si estamos en un trade activo (1 posición a la vez)
            if in_trade and i < trade_end_idx:
                continue
            in_trade = False

            row    = df.iloc[i]
            regime = df["regime"].iloc[i]
            atr    = row["atr"]

            signal = check_entry(
                row, regime,
                rsi_min_l, rsi_max_l,
                rsi_min_s, rsi_max_s,
                vol_min
            )
            if signal is None:
                continue

            # Simular trade
            trade_result = simulate_trade(df, i, signal["direction"], atr)
            if trade_result["result"] == "SKIP":
                continue

            trades_this_param.append({
                "timestamp":   df.index[i],
                "regime":      regime,
                "signal_type": signal["signal_type"],
                "direction":   signal["direction"],
                "rsi":         row["rsi"],
                "vol_ratio":   row["vol_ratio"],
                "result":      trade_result["result"],
                "pnl_pct":     trade_result["pnl_pct"],
                "exit_bar":    trade_result["exit_bar"],
                # Parámetros usados
                "p_rsi_min_l": rsi_min_l,
                "p_rsi_max_l": rsi_max_l,
                "p_rsi_min_s": rsi_min_s,
                "p_rsi_max_s": rsi_max_s,
                "p_vol_min":   vol_min,
            })

            in_trade      = True
            trade_end_idx = i + trade_result.get("exit_bar", FORWARD_BARS)

        results.extend(trades_this_param)

    if not results:
        print(f"  Sin trades simulados para {symbol}")
        return pd.DataFrame()

    df_results = pd.DataFrame(results)
    df_results["symbol"] = symbol
    print(f"  Total trades simulados: {len(df_results):,}")
    return df_results


# ── Análisis de resultados ────────────────────────────────────

def analyze_results(df: pd.DataFrame) -> None:
    """
    Genera el reporte de calibración:
    1. Mejores parámetros globales por régimen + signal_type
    2. Win rate y expectancy por combinación
    3. Comparación con parámetros actuales de producción
    """
    if df.empty:
        print("\nSin resultados para analizar.")
        return

    print(f"\n{'='*70}")
    print(f"  ANÁLISIS DE RESULTADOS")
    print(f"  Total trades analizados: {len(df):,}")
    print(f"{'='*70}")

    # ── Parámetros actuales de producción (baseline) ──────────
    PROD_PARAMS = {
        "rsi_min_l": 42, "rsi_max_l": 65,
        "rsi_min_s": 35, "rsi_max_s": 58,
        "vol_min":   1.3,
    }
    # Para señal fuerte (EMA_CROSS, RSI_RECOVERY)
    PROD_PARAMS_SIGNAL = {
        "rsi_min_l": 35, "rsi_max_l": 72,
        "rsi_min_s": 28, "rsi_max_s": 65,
        "vol_min":   1.2,
    }

    # ── 1. Análisis por régimen ───────────────────────────────
    print(f"\n── Win Rate por Régimen (todos los parámetros) ──")
    regime_summary = df.groupby("regime").agg(
        trades=("result", "count"),
        wins=("result", lambda x: (x == "WIN").sum()),
        avg_pnl=("pnl_pct", "mean"),
        avg_exit=("exit_bar", "mean"),
    ).assign(win_rate=lambda x: x["wins"] / x["trades"] * 100)
    print(regime_summary.to_string())

    # ── 2. Mejor combinación de parámetros por régimen ───────
    print(f"\n── Mejores Parámetros por Régimen + Signal Type ──")

    combos = df.groupby([
        "regime", "signal_type",
        "p_rsi_min_l", "p_rsi_max_l",
        "p_rsi_min_s", "p_rsi_max_s",
        "p_vol_min"
    ]).agg(
        n_trades=("result", "count"),
        win_rate=("result", lambda x: (x == "WIN").mean() * 100),
        avg_pnl=("pnl_pct", "mean"),
        expectancy=("pnl_pct", lambda x: x.mean()),
    ).reset_index()

    # Filtrar combinaciones con al menos 5 trades (estadísticamente relevantes)
    combos = combos[combos["n_trades"] >= 5].copy()
    combos["score"] = combos["win_rate"] * 0.6 + combos["avg_pnl"] * 10 * 0.4

    best_by_regime = {}
    for (regime, signal_type), group in combos.groupby(["regime", "signal_type"]):
        if group.empty:
            continue
        best = group.nlargest(1, "score").iloc[0]
        best_by_regime[(regime, signal_type)] = best

        print(f"\n  {regime} | {signal_type}")
        print(f"    Trades:      {int(best['n_trades'])}")
        print(f"    Win Rate:    {best['win_rate']:.1f}%")
        print(f"    Avg PnL:     {best['avg_pnl']:+.3f}%")
        print(f"    Score:       {best['score']:.2f}")
        print(f"    RSI LONG:    [{best['p_rsi_min_l']:.0f} – {best['p_rsi_max_l']:.0f}]")
        print(f"    RSI SHORT:   [{best['p_rsi_min_s']:.0f} – {best['p_rsi_max_s']:.0f}]")
        print(f"    Vol mín:     {best['p_vol_min']:.1f}×")

    # ── 3. Análisis específico REVERSAL ───────────────────────
    print(f"\n── Análisis REVERSAL (régimen clave para tunear) ──")
    rev = df[df["regime"] == "REVERSAL"] if "REVERSAL" in df["regime"].values else pd.DataFrame()
    if rev.empty:
        print("  Sin trades en REVERSAL en el período analizado.")
        print("  (REVERSAL actualmente bloqueado en producción — esto es esperado)")
    else:
        rev_combos = combos[combos["regime"] == "REVERSAL"].nlargest(5, "score")
        if not rev_combos.empty:
            print(rev_combos[[
                "signal_type", "n_trades", "win_rate", "avg_pnl",
                "p_rsi_min_l", "p_rsi_max_l", "p_vol_min"
            ]].to_string(index=False))

    # ── 4. Tabla comparativa: producción vs óptimo ───────────
    print(f"\n── Comparación: Producción vs Óptimo ──")
    print(f"{'Combinación':<30} {'Prod WR':>8} {'Prod PnL':>9} {'Opt WR':>8} {'Opt PnL':>9} {'Mejora':>8}")
    print("-" * 75)

    for (regime, signal_type), best in best_by_regime.items():
        # Win rate con parámetros de producción
        is_strong = signal_type in ("EMA_CROSS", "RSI_RECOVERY", "RSI_REJECTION")
        p = PROD_PARAMS_SIGNAL if is_strong else PROD_PARAMS

        prod_trades = df[
            (df["regime"] == regime) &
            (df["signal_type"] == signal_type) &
            (df["p_rsi_min_l"] == p["rsi_min_l"]) &
            (df["p_rsi_max_l"] == p["rsi_max_l"]) &
            (df["p_vol_min"] == p["vol_min"])
        ]

        if prod_trades.empty:
            prod_wr  = 0.0
            prod_pnl = 0.0
        else:
            prod_wr  = (prod_trades["result"] == "WIN").mean() * 100
            prod_pnl = prod_trades["pnl_pct"].mean()

        combo_name = f"{regime[:10]} | {signal_type[:12]}"
        mejora = best["win_rate"] - prod_wr
        print(
            f"{combo_name:<30} {prod_wr:>7.1f}% {prod_pnl:>+8.3f}%"
            f" {best['win_rate']:>7.1f}% {best['avg_pnl']:>+8.3f}%"
            f" {mejora:>+7.1f}%"
        )

    # ── 5. Recomendaciones finales ────────────────────────────
    print(f"\n{'='*70}")
    print(f"  RECOMENDACIONES PARA data.py")
    print(f"{'='*70}")
    print("""
  Reemplazar en check_entry_conditions() los umbrales hardcodeados
  por los valores óptimos encontrados arriba.

  Estructura actual (data.py líneas 250-253):

    rsi_max_long  = 72 if signal_type in ('EMA_CROSS',)     else 65
    rsi_min_long  = 35 if signal_type in ('RSI_RECOVERY',)  else 42
    rsi_min_short = 28 if signal_type in ('EMA_CROSS',)     else 35
    rsi_max_short = 65 if signal_type in ('RSI_REJECTION',) else 58

  Estructura propuesta (con umbrales por régimen):

    # Umbrales calibrados por backtest — ver backtest_regimen.py
    RSI_THRESHOLDS = {
        'BULL_TREND': {
            'EMA_CROSS':    {'min_l': X, 'max_l': X, 'vol': X},
            'RSI_RECOVERY': {'min_l': X, 'max_l': X, 'vol': X},
            'ALIGNMENT':    {'min_l': X, 'max_l': X, 'vol': X},
        },
        'BEAR_TREND': { ... },
    }
    thresholds = RSI_THRESHOLDS.get(regime, RSI_THRESHOLDS['BULL_TREND'])
                               .get(signal_type or 'ALIGNMENT', ...)
  """)

    # ── 6. Guardar resultados completos ──────────────────────
    output_path = "/mnt/user-data/outputs/backtest_resultados.csv"
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    df.to_csv(output_path, index=False)
    print(f"  Resultados completos guardados en: {output_path}")

    # Guardar mejores parámetros como JSON
    best_params = {}
    for (regime, signal_type), row in best_by_regime.items():
        key = f"{regime}|{signal_type}"
        best_params[key] = {
            "regime":       regime,
            "signal_type":  signal_type,
            "n_trades":     int(row["n_trades"]),
            "win_rate":     round(float(row["win_rate"]), 1),
            "avg_pnl":      round(float(row["avg_pnl"]), 4),
            "rsi_min_long": int(row["p_rsi_min_l"]),
            "rsi_max_long": int(row["p_rsi_max_l"]),
            "rsi_min_short":int(row["p_rsi_min_s"]),
            "rsi_max_short":int(row["p_rsi_max_s"]),
            "vol_min":      float(row["p_vol_min"]),
        }

    json_path = "/mnt/user-data/outputs/best_params.json"
    with open(json_path, "w") as f:
        json.dump(best_params, f, indent=2)
    print(f"  Mejores parámetros guardados en: {json_path}")


# ── Entry point ───────────────────────────────────────────────

def main():
    print(f"\n{'#'*70}")
    print(f"  BACKTEST — CALIBRACIÓN DE UMBRALES POR RÉGIMEN")
    print(f"  Período: últimas {LOOKBACK_BARS} velas 4h (~{LOOKBACK_BARS*4//24//30} meses)")
    print(f"  Símbolos: {SYMBOLS}")
    print(f"  Forward bars: {FORWARD_BARS} (={FORWARD_BARS*4}h para evaluar resultado)")
    print(f"  Combinaciones a testear: {len(list(itertools.product(RSI_MIN_LONG_GRID, RSI_MAX_LONG_GRID, RSI_MIN_SHORT_GRID, RSI_MAX_SHORT_GRID, VOL_MIN_GRID))):,}")
    print(f"{'#'*70}\n")

    all_results = []

    for symbol in SYMBOLS:
        df_sym = run_backtest_symbol(symbol)
        if not df_sym.empty:
            all_results.append(df_sym)

    if not all_results:
        print("\nSin resultados. Verificá que los modelos HMM estén en ./models/")
        return

    df_all = pd.concat(all_results, ignore_index=True)
    analyze_results(df_all)

    print(f"\n✅ Backtest completado.")
    print(f"   Revisá backtest_resultados.csv y best_params.json para los detalles.")


if __name__ == "__main__":
    main()
