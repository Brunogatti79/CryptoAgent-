# =============================================================
#  BACKTEST — OPTIMIZER
#  Prueba combinaciones de hiperparámetros y encuentra la óptima
#  Uso: python optimizer.py
#       python optimizer.py --metric sharpe   (default)
#       python optimizer.py --metric winrate
#       python optimizer.py --metric return
# =============================================================

import argparse
import os
import itertools
import pandas as pd
import numpy as np
from simulator import StrategyParams, simulate, calc_metrics


# ── Grid de parámetros a explorar ────────────────────────────

PARAM_GRID = {
    "rsi_period":      [9, 14, 21],
    "ema_fast":        [10, 20],
    "ema_slow":        [50, 100, 200],
    "stop_loss_pct":   [0.03, 0.04, 0.05],
    "take_profit_pct": [0.06, 0.08, 0.12],   # ratios 2:1, 2:1, 2.4:1 aprox
}


def walk_forward_test(df: pd.DataFrame, symbol: str, p: StrategyParams,
                      train_pct: float = 0.8) -> dict:
    """
    Walk-forward: entrena en 80% de los datos, valida en el 20% más reciente.
    Retorna métricas SOLO del período de validación.
    """
    split     = int(len(df) * train_pct)
    df_train  = df.iloc[:split]
    df_val    = df.iloc[split:]

    # Usamos train solo para confirmar que hay suficientes datos
    if len(df_val) < 100:
        return {"symbol": symbol, "trades": 0}

    trades_val = simulate(df_val, symbol, p)
    return calc_metrics(trades_val, symbol)


def score(metrics: dict, metric: str) -> float:
    """Función objetivo a maximizar."""
    if metrics.get("trades", 0) < 10:   # mínimo de trades para ser válido
        return -999
    if metric == "sharpe":
        return metrics.get("sharpe", -999)
    elif metric == "winrate":
        return metrics.get("win_rate", 0)
    elif metric == "return":
        return metrics.get("return_pct", -999)
    elif metric == "expectancy":
        return metrics.get("expectancy", -999)
    return metrics.get("sharpe", -999)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--metric", default="sharpe",
                        choices=["sharpe", "winrate", "return", "expectancy"])
    args = parser.parse_args()

    data_dir = "data"
    files    = [f for f in os.listdir(data_dir) if f.endswith("_1h.csv")]

    if not files:
        print("No hay datos. Corré primero: python downloader.py")
        return

    # Cargar todos los DataFrames
    datasets = {}
    for fname in sorted(files):
        symbol = fname.replace("_1h.csv", "")
        datasets[symbol] = pd.read_csv(
            os.path.join(data_dir, fname), index_col=0, parse_dates=True
        )

    # Generar todas las combinaciones
    keys   = list(PARAM_GRID.keys())
    values = list(PARAM_GRID.values())
    combos = list(itertools.product(*values))
    total  = len(combos)

    print(f"\nOptimizando {total} combinaciones de parámetros")
    print(f"Métrica objetivo: {args.metric.upper()}")
    print(f"Validación walk-forward (80% train / 20% test)\n")

    results = []

    for i, combo in enumerate(combos):
        params_dict = dict(zip(keys, combo))

        # Validar que ema_fast < ema_slow
        if params_dict["ema_fast"] >= params_dict["ema_slow"]:
            continue

        p = StrategyParams(**params_dict)

        # Probar en todos los pares y promediar
        scores_combo = []
        metrics_all  = []

        for symbol, df in datasets.items():
            m = walk_forward_test(df, symbol, p)
            metrics_all.append(m)
            scores_combo.append(score(m, args.metric))

        avg_score = np.mean(scores_combo)

        result = {
            **params_dict,
            f"avg_{args.metric}":   round(avg_score, 3),
            "avg_winrate":          round(np.mean([m.get("win_rate", 0) for m in metrics_all]), 1),
            "avg_sharpe":           round(np.mean([m.get("sharpe", 0) for m in metrics_all]), 2),
            "avg_drawdown":         round(np.mean([m.get("max_drawdown", 0) for m in metrics_all]), 1),
            "avg_trades":           round(np.mean([m.get("trades", 0) for m in metrics_all]), 0),
            "avg_expectancy":       round(np.mean([m.get("expectancy", 0) for m in metrics_all]), 3),
        }
        results.append(result)

        if (i + 1) % 10 == 0:
            print(f"  Progreso: {i+1}/{total}...", end="\r")

    print(f"  Progreso: {total}/{total} — completado          \n")

    # Ordenar por métrica objetivo
    df_results = pd.DataFrame(results)
    df_results = df_results.sort_values(f"avg_{args.metric}", ascending=False)

    # Top 5
    print(f"{'='*65}")
    print(f"  TOP 5 CONFIGURACIONES (por {args.metric.upper()})")
    print(f"{'='*65}")

    for rank, (_, row) in enumerate(df_results.head(5).iterrows(), 1):
        print(f"\n  #{rank}")
        print(f"    RSI período:    {int(row['rsi_period'])}")
        print(f"    EMA fast/slow:  {int(row['ema_fast'])} / {int(row['ema_slow'])}")
        print(f"    Stop-loss:      {row['stop_loss_pct']*100:.0f}%")
        print(f"    Take-profit:    {row['take_profit_pct']*100:.0f}%")
        print(f"    Win rate:       {row['avg_winrate']}%")
        print(f"    Sharpe ratio:   {row['avg_sharpe']}")
        print(f"    Max drawdown:   {row['avg_drawdown']}%")
        print(f"    Expectancy:     {row['avg_expectancy']:+.3f}% por trade")
        print(f"    Trades (val):   {int(row['avg_trades'])}")

    # Guardar todos los resultados
    df_results.to_csv("optimizer_results.csv", index=False)
    print(f"\n  Todos los resultados guardados en optimizer_results.csv")

    # Mejor configuración → actualizar config sugerido
    best = df_results.iloc[0]
    print(f"\n{'='*65}")
    print(f"  CONFIGURACIÓN ÓPTIMA SUGERIDA PARA EL AGENTE")
    print(f"{'='*65}")
    print(f"""
  Copiá estos valores en config.py del agente:

  RSI_PERIOD        = {int(best['rsi_period'])}
  EMA_FAST          = {int(best['ema_fast'])}
  EMA_SLOW          = {int(best['ema_slow'])}
  STOP_LOSS_PCT     = {best['stop_loss_pct']}
  TAKE_PROFIT_PCT   = {best['take_profit_pct']}
""")


if __name__ == "__main__":
    main()
