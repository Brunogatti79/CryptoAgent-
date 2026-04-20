# =============================================================
#  BACKTEST — SIMULATOR
#  Simula la estrategia del agente sobre datos históricos
#  Uso: python simulator.py
#       python simulator.py --symbol BTCUSDT --rsi 14 --ema-fast 20 --ema-slow 50
# =============================================================

import argparse
import os
import sys
import pandas as pd
import numpy as np
from dataclasses import dataclass, field


# ── Parámetros por defecto (los mismos del agente actual) ─────

@dataclass
class StrategyParams:
    rsi_period:      int   = 14
    ema_fast:        int   = 20
    ema_slow:        int   = 50
    min_conviction:  int   = 7      # umbral de convicción simulado
    stop_loss_pct:   float = 0.04   # 4%
    take_profit_pct: float = 0.08   # 8% (ratio 2:1)
    rsi_ob:          float = 70.0   # overbought — no entrar long
    rsi_os:          float = 30.0   # oversold — no entrar short


# ── Indicadores ───────────────────────────────────────────────

def calc_rsi(closes: pd.Series, period: int) -> pd.Series:
    delta = closes.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, 1e-10)
    return 100 - (100 / (1 + rs))


def calc_indicators(df: pd.DataFrame, p: StrategyParams) -> pd.DataFrame:
    df = df.copy()
    df["rsi"]      = calc_rsi(df["close"], p.rsi_period)
    df["ema_fast"] = df["close"].ewm(span=p.ema_fast).mean()
    df["ema_slow"] = df["close"].ewm(span=p.ema_slow).mean()
    df["trend"]    = df["ema_fast"] > df["ema_slow"]   # True = alcista
    return df


# ── Señal de entrada ──────────────────────────────────────────

def generate_signal(row) -> str:
    """
    Replica la lógica del agente sin llamar a Claude.
    LONG si: tendencia alcista + RSI no sobrecomprado
    SHORT si: tendencia bajista + RSI no sobrevendido
    """
    if pd.isna(row["rsi"]) or pd.isna(row["ema_fast"]):
        return "NEUTRAL"

    if row["trend"] and row["rsi"] < 70:
        return "LONG"
    elif not row["trend"] and row["rsi"] > 30:
        return "SHORT"
    return "NEUTRAL"


# ── Motor de simulación ───────────────────────────────────────

@dataclass
class Trade:
    symbol:      str
    direction:   str
    entry_price: float
    stop_loss:   float
    take_profit: float
    entry_time:  pd.Timestamp
    exit_price:  float  = 0.0
    exit_time:   pd.Timestamp = None
    result:      str    = "OPEN"   # WIN / LOSS / OPEN
    pnl_pct:     float  = 0.0


def simulate(df: pd.DataFrame, symbol: str, p: StrategyParams) -> list[Trade]:
    """Simula trades sobre el DataFrame con los parámetros dados."""
    df      = calc_indicators(df, p)
    trades  = []
    in_trade = False
    trade    = None

    for i in range(1, len(df)):
        row  = df.iloc[i]
        prev = df.iloc[i - 1]

        # Monitorear posición abierta
        if in_trade and trade:
            high  = row["high"]
            low   = row["low"]
            close = row["close"]

            if trade.direction == "LONG":
                if low <= trade.stop_loss:
                    trade.exit_price = trade.stop_loss
                    trade.exit_time  = row.name
                    trade.result     = "LOSS"
                    trade.pnl_pct    = (trade.stop_loss - trade.entry_price) / trade.entry_price * 100
                    trades.append(trade)
                    in_trade = False
                elif high >= trade.take_profit:
                    trade.exit_price = trade.take_profit
                    trade.exit_time  = row.name
                    trade.result     = "WIN"
                    trade.pnl_pct    = (trade.take_profit - trade.entry_price) / trade.entry_price * 100
                    trades.append(trade)
                    in_trade = False
            else:  # SHORT
                if high >= trade.stop_loss:
                    trade.exit_price = trade.stop_loss
                    trade.exit_time  = row.name
                    trade.result     = "LOSS"
                    trade.pnl_pct    = (trade.entry_price - trade.stop_loss) / trade.entry_price * 100
                    trades.append(trade)
                    in_trade = False
                elif low <= trade.take_profit:
                    trade.exit_price = trade.take_profit
                    trade.exit_time  = row.name
                    trade.result     = "WIN"
                    trade.pnl_pct    = (trade.entry_price - trade.take_profit) / trade.entry_price * 100
                    trades.append(trade)
                    in_trade = False

        # Buscar nueva entrada si no estamos en trade
        if not in_trade:
            signal = generate_signal(row)
            if signal == "NEUTRAL":
                continue

            price = row["close"]
            if signal == "LONG":
                stop   = price * (1 - p.stop_loss_pct)
                target = price * (1 + p.take_profit_pct)
            else:
                stop   = price * (1 + p.stop_loss_pct)
                target = price * (1 - p.take_profit_pct)

            trade    = Trade(symbol, signal, price, stop, target, row.name)
            in_trade = True

    return trades


# ── Métricas de performance ───────────────────────────────────

def calc_metrics(trades: list[Trade], symbol: str) -> dict:
    if not trades:
        return {"symbol": symbol, "trades": 0}

    closed  = [t for t in trades if t.result != "OPEN"]
    wins    = [t for t in closed if t.result == "WIN"]
    losses  = [t for t in closed if t.result == "LOSS"]

    if not closed:
        return {"symbol": symbol, "trades": 0}

    win_rate   = len(wins) / len(closed) * 100
    avg_win    = np.mean([t.pnl_pct for t in wins])   if wins   else 0
    avg_loss   = np.mean([t.pnl_pct for t in losses]) if losses else 0
    expectancy = (win_rate/100 * avg_win) + ((1 - win_rate/100) * avg_loss)

    # Curva de equity (capital inicial = 1000)
    capital   = 1000.0
    equity    = [capital]
    peak      = capital
    drawdowns = []

    for t in closed:
        pnl      = capital * (t.pnl_pct / 100) * 0.02  # 2% del capital por trade
        capital += pnl
        equity.append(capital)
        peak     = max(peak, capital)
        drawdowns.append((peak - capital) / peak * 100)

    max_dd = max(drawdowns) if drawdowns else 0

    # Sharpe ratio simplificado
    returns = pd.Series([t.pnl_pct for t in closed])
    sharpe  = (returns.mean() / returns.std() * np.sqrt(252)) if returns.std() > 0 else 0

    return {
        "symbol":      symbol,
        "trades":      len(closed),
        "win_rate":    round(win_rate, 1),
        "avg_win":     round(avg_win, 2),
        "avg_loss":    round(avg_loss, 2),
        "expectancy":  round(expectancy, 3),
        "max_drawdown":round(max_dd, 1),
        "sharpe":      round(sharpe, 2),
        "final_capital": round(capital, 2),
        "return_pct":  round((capital - 1000) / 1000 * 100, 1),
        "equity":      equity,
    }


def print_report(metrics: dict) -> None:
    if metrics.get("trades", 0) == 0:
        print(f"  {metrics['symbol']}: sin trades suficientes")
        return

    print(f"\n{'='*50}")
    print(f"  {metrics['symbol']}")
    print(f"{'='*50}")
    print(f"  Trades cerrados:   {metrics['trades']}")
    print(f"  Win rate:          {metrics['win_rate']}%")
    print(f"  Avg ganancia:      {metrics['avg_win']:+.2f}%")
    print(f"  Avg pérdida:       {metrics['avg_loss']:+.2f}%")
    print(f"  Expectancy:        {metrics['expectancy']:+.3f}% por trade")
    print(f"  Max drawdown:      {metrics['max_drawdown']}%")
    print(f"  Sharpe ratio:      {metrics['sharpe']}")
    print(f"  Retorno total:     {metrics['return_pct']:+.1f}%")
    print(f"  Capital final:     ${metrics['final_capital']:,.2f} (desde $1,000)")


# =============================================================
#  MODO RÉGIMEN — funciones adicionales
#  Activo con: python simulator.py --regime
# =============================================================

# Sesgo por régimen: qué señales están permitidas y con qué umbrales RSI
REGIME_BIAS = {
    # BULL_TREND: uptrend claro, baja vol (0.70%) → LONGs confiables
    "BULL_TREND": {"allow_long": True,  "allow_short": False, "rsi_ob": 70, "rsi_os": 40},
    # BEAR_TREND: vol MUY alta (1.50%) → stop 4% se toca por ruido → no operar
    "BEAR_TREND": {"allow_long": False, "allow_short": False, "rsi_ob": 60, "rsi_os": 30},
    # SIDEWAYS: vol más baja de todos (0.66%), slope negativo → slow downtrend confiable → SHORTs
    "SIDEWAYS":   {"allow_long": False, "allow_short": True,  "rsi_ob": 65, "rsi_os": 35},
    # REVERSAL: retorno +0.071%, vol media → LONGs conservadores
    "REVERSAL":   {"allow_long": True,  "allow_short": False, "rsi_ob": 60, "rsi_os": 40},
}


def generate_signal_with_regime(row, regime: str) -> str:
    """
    Versión del generador de señales que respeta el sesgo del régimen HMM.
    - BULL_TREND: solo LONGs cuando la tendencia confirma y RSI no está sobrecomprado
    - BEAR_TREND: solo SHORTs cuando la tendencia confirma y RSI no está sobrevendido
    - SIDEWAYS / REVERSAL: NEUTRAL siempre (evitar ruido)
    """
    if pd.isna(row["rsi"]) or pd.isna(row["ema_fast"]):
        return "NEUTRAL"

    bias = REGIME_BIAS.get(regime, REGIME_BIAS["SIDEWAYS"])

    if bias["allow_long"] and row["trend"] and row["rsi"] < bias["rsi_ob"]:
        return "LONG"
    if bias["allow_short"] and not row["trend"] and row["rsi"] > bias["rsi_os"]:
        return "SHORT"
    return "NEUTRAL"


def simulate_with_regime(df: pd.DataFrame, symbol: str, p: StrategyParams,
                         regimes: pd.Series) -> list[Trade]:
    """
    Igual que simulate() pero usa generate_signal_with_regime().
    regimes: pd.Series con índice de timestamps y valores como 'BULL_TREND', etc.
    """
    df       = calc_indicators(df, p)
    trades   = []
    in_trade = False
    trade    = None

    for i in range(1, len(df)):
        row  = df.iloc[i]
        prev = df.iloc[i - 1]

        if in_trade and trade:
            high  = row["high"]
            low   = row["low"]

            if trade.direction == "LONG":
                if low <= trade.stop_loss:
                    trade.exit_price = trade.stop_loss
                    trade.exit_time  = row.name
                    trade.result     = "LOSS"
                    trade.pnl_pct    = (trade.stop_loss - trade.entry_price) / trade.entry_price * 100
                    trades.append(trade)
                    in_trade = False
                elif high >= trade.take_profit:
                    trade.exit_price = trade.take_profit
                    trade.exit_time  = row.name
                    trade.result     = "WIN"
                    trade.pnl_pct    = (trade.take_profit - trade.entry_price) / trade.entry_price * 100
                    trades.append(trade)
                    in_trade = False
            else:
                if high >= trade.stop_loss:
                    trade.exit_price = trade.stop_loss
                    trade.exit_time  = row.name
                    trade.result     = "LOSS"
                    trade.pnl_pct    = (trade.entry_price - trade.stop_loss) / trade.entry_price * 100
                    trades.append(trade)
                    in_trade = False
                elif low <= trade.take_profit:
                    trade.exit_price = trade.take_profit
                    trade.exit_time  = row.name
                    trade.result     = "WIN"
                    trade.pnl_pct    = (trade.entry_price - trade.take_profit) / trade.entry_price * 100
                    trades.append(trade)
                    in_trade = False

        if not in_trade:
            regime = regimes.get(row.name, "SIDEWAYS")
            signal = generate_signal_with_regime(row, regime)
            if signal == "NEUTRAL":
                continue

            price = row["close"]
            if signal == "LONG":
                stop   = price * (1 - p.stop_loss_pct)
                target = price * (1 + p.take_profit_pct)
            else:
                stop   = price * (1 + p.stop_loss_pct)
                target = price * (1 - p.take_profit_pct)

            trade    = Trade(symbol, signal, price, stop, target, row.name)
            in_trade = True

    return trades


def run_regime_comparison(data_dir: str, symbol_filter: str | None,
                          p: StrategyParams) -> None:
    """
    Compara la estrategia naive vs regime-aware usando walk-forward:
    - Entrena HMM en el 80% más antiguo de datos 4h
    - Evalúa ambas estrategias en el 20% más reciente (mismo período)
    - Usa datos 1h para la simulación (misma granularidad que el baseline)
    """
    try:
        from regime_trainer import (
            compute_features, train_hmm, label_states, predict_regimes,
            StandardScaler,
        )
    except ImportError:
        print("ERROR: No se pudo importar regime_trainer.")
        print("  Asegurate de correr desde el directorio backtest/")
        return

    files_4h = sorted(f for f in os.listdir(data_dir) if f.endswith("_4h.csv"))
    files_1h = sorted(f for f in os.listdir(data_dir) if f.endswith("_1h.csv"))

    if not files_4h:
        print("No hay datos 4h. Corré: python downloader.py")
        return

    if symbol_filter:
        files_4h = [f for f in files_4h if symbol_filter.upper() in f]
        files_1h = [f for f in files_1h if symbol_filter.upper() in f]

    print(f"\n{'='*60}")
    print("  COMPARACIÓN: NAIVE vs REGIME-AWARE (walk-forward 80/20)")
    print(f"{'='*60}")

    summary_rows = []

    for fname_4h in files_4h:
        symbol   = fname_4h.replace("_4h.csv", "")
        fname_1h = f"{symbol}_1h.csv"

        if fname_1h not in files_1h:
            print(f"\n  {symbol}: sin datos 1h, saltando.")
            continue

        df_4h = pd.read_csv(os.path.join(data_dir, fname_4h), index_col=0, parse_dates=True)
        df_1h = pd.read_csv(os.path.join(data_dir, fname_1h), index_col=0, parse_dates=True)

        # ── Entrenar HMM en 80% más antiguo de datos 4h ───────
        split_4h   = int(len(df_4h) * 0.8)
        df_4h_train = df_4h.iloc[:split_4h]
        df_4h_val   = df_4h.iloc[split_4h:]

        try:
            X_train, _ = compute_features(df_4h_train)
        except Exception as e:
            print(f"\n  {symbol}: error en features HMM: {e}")
            continue

        scaler   = StandardScaler()
        X_scaled = scaler.fit_transform(X_train)
        model    = train_hmm(X_scaled, n_components=4)
        labels   = label_states(model, scaler)

        # Predecir régimen en el período de validación 4h
        regimes_4h_val = predict_regimes(df_4h_val, model, scaler, labels)

        # ── Alinear con datos 1h ───────────────────────────────
        # Usamos el período de validación del 4h también en 1h
        val_start = df_4h_val.index[0]
        val_end   = df_4h_val.index[-1]
        df_1h_val = df_1h[(df_1h.index >= val_start) & (df_1h.index <= val_end)]

        if len(df_1h_val) < 100:
            print(f"\n  {symbol}: período de validación 1h muy corto ({len(df_1h_val)} barras).")
            continue

        # Expandir regímenes 4h a timestamps 1h por forward-fill
        regimes_reindexed = regimes_4h_val.reindex(
            df_1h_val.index, method="ffill"
        ).fillna("SIDEWAYS")

        # ── Simular ambas estrategias en el mismo período ──────
        trades_naive  = simulate(df_1h_val, symbol, p)
        trades_regime = simulate_with_regime(df_1h_val, symbol, p, regimes_reindexed)

        m_naive  = calc_metrics(trades_naive,  symbol)
        m_regime = calc_metrics(trades_regime, symbol)

        # ── Imprimir comparación ───────────────────────────────
        print(f"\n  {symbol}  (validación: {val_start.date()} → {val_end.date()})")
        print(f"  {'':20}  {'NAIVE':>10}  {'REGIME':>10}  {'DELTA':>10}")
        print(f"  {'-'*54}")

        def row_cmp(label, key, fmt="{:.2f}", higher_is_better=True):
            vn = m_naive.get(key, 0)
            vr = m_regime.get(key, 0)
            delta = vr - vn
            sign  = "+" if delta >= 0 else ""
            better = (delta >= 0) == higher_is_better
            mark  = " ✓" if better else " ✗"
            print(f"  {label:20}  {fmt.format(vn):>10}  {fmt.format(vr):>10}  "
                  f"{sign}{fmt.format(delta):>9}{mark}")

        row_cmp("Trades",    "trades",      "{:.0f}")
        row_cmp("Win rate",  "win_rate",    "{:.1f}%")
        row_cmp("Sharpe",    "sharpe",      "{:.2f}")
        row_cmp("Expectancy","expectancy",  "{:+.3f}%")
        row_cmp("Max DD",    "max_drawdown","{:.1f}%", higher_is_better=False)
        row_cmp("Retorno",   "return_pct",  "{:+.1f}%")

        # Distribución de regímenes en validación
        counts = regimes_reindexed.value_counts()
        total  = len(regimes_reindexed)
        dist   = "  ".join(f"{k}: {v/total*100:.0f}%" for k, v in counts.items())
        print(f"\n  Regímenes en validación:  {dist}")

        # ── Qué regímenes generaron los trades regime-aware ──
        if trades_regime:
            regime_at_entry = {}
            for t in trades_regime:
                r = regimes_reindexed.get(t.entry_time, "?")
                regime_at_entry[r] = regime_at_entry.get(r, 0) + 1
            entry_dist = "  ".join(f"{k}: {v}" for k, v in sorted(regime_at_entry.items()))
            print(f"  Entradas por régimen:     {entry_dist}")

        summary_rows.append({
            "symbol":         symbol,
            "naive_sharpe":   m_naive.get("sharpe",   0),
            "regime_sharpe":  m_regime.get("sharpe",  0),
            "naive_return":   m_naive.get("return_pct", 0),
            "regime_return":  m_regime.get("return_pct", 0),
            "naive_trades":   m_naive.get("trades",   0),
            "regime_trades":  m_regime.get("trades",  0),
        })

    if not summary_rows:
        return

    print(f"\n{'='*60}")
    print("  RESUMEN GLOBAL")
    print(f"{'='*60}")
    naive_sharpe  = np.mean([r["naive_sharpe"]  for r in summary_rows])
    regime_sharpe = np.mean([r["regime_sharpe"] for r in summary_rows])
    naive_ret     = np.mean([r["naive_return"]  for r in summary_rows])
    regime_ret    = np.mean([r["regime_return"] for r in summary_rows])
    print(f"  Sharpe promedio:  naive={naive_sharpe:.2f}  regime={regime_sharpe:.2f}  "
          f"delta={regime_sharpe - naive_sharpe:+.2f}")
    print(f"  Retorno prom:     naive={naive_ret:+.1f}%  regime={regime_ret:+.1f}%  "
          f"delta={regime_ret - naive_ret:+.1f}%")

    pd.DataFrame(summary_rows).to_csv("regime_comparison.csv", index=False)
    print(f"\n  Resultados guardados en regime_comparison.csv")


# ── Main ──────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Backtest simulator")
    parser.add_argument("--symbol",    default=None)
    parser.add_argument("--rsi",       type=int,   default=14)
    parser.add_argument("--ema-fast",  type=int,   default=20)
    parser.add_argument("--ema-slow",  type=int,   default=50)
    parser.add_argument("--stop",      type=float, default=0.04)
    parser.add_argument("--target",    type=float, default=0.08)
    parser.add_argument("--regime",    action="store_true",
                        help="Comparar estrategia naive vs regime-aware (requiere regime_trainer.py)")
    args = parser.parse_args()

    p = StrategyParams(
        rsi_period      = args.rsi,
        ema_fast        = getattr(args, "ema_fast"),
        ema_slow        = getattr(args, "ema_slow"),
        stop_loss_pct   = args.stop,
        take_profit_pct = args.target,
    )

    data_dir = "data"

    if args.regime:
        run_regime_comparison(data_dir, args.symbol, p)
        return

    files = [f for f in os.listdir(data_dir) if f.endswith("_1h.csv")]

    if args.symbol:
        files = [f for f in files if args.symbol.upper() in f]

    if not files:
        print("No hay datos. Corré primero: python downloader.py")
        return

    print(f"\nSimulando estrategia: RSI={p.rsi_period} | EMA {p.ema_fast}/{p.ema_slow} | Stop={p.stop_loss_pct*100}% | Target={p.take_profit_pct*100}%\n")

    all_metrics = []
    for fname in sorted(files):
        symbol = fname.replace("_1h.csv", "")
        df     = pd.read_csv(os.path.join(data_dir, fname), index_col=0, parse_dates=True)
        trades = simulate(df, symbol, p)
        m      = calc_metrics(trades, symbol)
        print_report(m)
        all_metrics.append(m)

    # Resumen global
    valid = [m for m in all_metrics if m.get("trades", 0) > 0]
    if valid:
        print(f"\n{'='*50}")
        print("  RESUMEN GLOBAL")
        print(f"{'='*50}")
        print(f"  Sharpe promedio:   {np.mean([m['sharpe'] for m in valid]):.2f}")
        print(f"  Win rate promedio: {np.mean([m['win_rate'] for m in valid]):.1f}%")
        print(f"  Expectancy prom:   {np.mean([m['expectancy'] for m in valid]):+.3f}%")
        print(f"  Max DD promedio:   {np.mean([m['max_drawdown'] for m in valid]):.1f}%")
        print()

    # Guardar resultados
    results_df = pd.DataFrame([{k: v for k, v in m.items() if k != "equity"} for m in all_metrics])
    results_df.to_csv("backtest_results.csv", index=False)
    print("  Resultados guardados en backtest_results.csv")


if __name__ == "__main__":
    main()
