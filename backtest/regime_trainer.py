# =============================================================
#  BACKTEST — REGIME TRAINER
#  Entrena un HMM Gaussiano por par sobre datos 4h históricos
#  y guarda el modelo en models/ para uso en producción y backtest.
#
#  Uso: python regime_trainer.py
#       python regime_trainer.py --symbol BTCUSDT
#       python regime_trainer.py --states 4  (default)
# =============================================================

import argparse
import os
import sys

import joblib
import numpy as np
import pandas as pd
from sklearn.preprocessing import StandardScaler

try:
    from hmmlearn.hmm import GaussianHMM
except ImportError:
    print("ERROR: hmmlearn no instalado.")
    print("  Corré: pip install hmmlearn")
    sys.exit(1)


DATA_DIR   = os.path.join(os.path.dirname(__file__), "data")
MODELS_DIR = os.path.join(os.path.dirname(__file__), "..", "models")
INTERVAL   = "4h"
N_RESTARTS = 10   # cantidad de inicializaciones aleatorias (elige la mejor por log-likelihood)


# ── Features ──────────────────────────────────────────────────

def compute_features(df: pd.DataFrame) -> tuple[np.ndarray, pd.Index]:
    """
    Construye la matriz de features para el HMM a partir de velas OHLCV.

    Features (5 columnas):
      0: log_return        — dirección de precio barra a barra
      1: rolling_vol_20    — std de log_returns en 20 barras (volatilidad realizada)
      2: volume_ratio      — volumen / MA20 de volumen (actividad relativa)
      3: rsi_centered      — (RSI14 - 50) / 50  →  [-1, 1] momentum normalizado
      4: ema50_slope       — (EMA50[t] - EMA50[t-3]) / EMA50[t]  (tendencia de fondo)

    Retorna:
      X     — np.ndarray shape (n_valid_bars, 5)
      index — DatetimeIndex de las barras válidas (sin NaN)
    """
    d = df.copy()
    d["log_ret"]    = np.log(d["close"] / d["close"].shift(1))
    d["vol_20"]     = d["log_ret"].rolling(20).std()
    vol_ma          = d["volume"].rolling(20).mean().replace(0, 1e-10)
    d["vol_ratio"]  = d["volume"] / vol_ma

    delta           = d["close"].diff()
    gain            = delta.clip(lower=0).rolling(14).mean()
    loss            = (-delta.clip(upper=0)).rolling(14).mean().replace(0, 1e-10)
    d["rsi_c"]      = (100 - 100 / (1 + gain / loss) - 50) / 50

    d["ema50"]      = d["close"].ewm(span=50).mean()
    d["ema50_slope"]= d["ema50"].diff(3) / d["ema50"]

    d.dropna(inplace=True)
    cols = ["log_ret", "vol_20", "vol_ratio", "rsi_c", "ema50_slope"]
    return d[cols].values, d.index


# ── Entrenamiento HMM ─────────────────────────────────────────

def train_hmm(X_scaled: np.ndarray, n_components: int = 4) -> GaussianHMM:
    """
    Entrena GaussianHMM con N_RESTARTS inicializaciones distintas.
    Retorna el modelo con mayor log-likelihood.
    """
    best_model, best_score = None, -np.inf

    for seed in range(N_RESTARTS):
        model = GaussianHMM(
            n_components    = n_components,
            covariance_type = "full",
            n_iter          = 300,
            random_state    = seed,
            tol             = 1e-5,
        )
        try:
            model.fit(X_scaled)
            s = model.score(X_scaled)
            if s > best_score:
                best_score = s
                best_model = model
        except Exception:
            continue

    if best_model is None:
        raise RuntimeError("HMM no convergió en ningún intento.")

    return best_model


# ── Etiquetado post-hoc ───────────────────────────────────────

def label_states(model: GaussianHMM, scaler: StandardScaler) -> dict[int, str]:
    """
    Asigna nombres de régimen a los estados del HMM mirando las medias
    de cada estado en escala original.

    Lógica:
      - Ordenar estados por mean log_return
      - Mayor return  → BULL_TREND
      - Menor return  → BEAR_TREND
      - De los 2 restantes: menor vol → SIDEWAYS, mayor vol → REVERSAL
    """
    means = scaler.inverse_transform(model.means_)
    # columnas: [log_ret, vol_20, vol_ratio, rsi_c, ema50_slope]

    states         = list(range(model.n_components))
    by_return      = sorted(states, key=lambda i: means[i][0])

    bear_state     = by_return[0]
    bull_state     = by_return[-1]
    middle         = by_return[1:-1]

    labels: dict[int, str] = {bull_state: "BULL_TREND", bear_state: "BEAR_TREND"}

    if len(middle) >= 2:
        sideways, reversal = sorted(middle, key=lambda i: means[i][1])
        labels[sideways]   = "SIDEWAYS"
        labels[reversal]   = "REVERSAL"
    elif len(middle) == 1:
        labels[middle[0]]  = "SIDEWAYS"

    return labels


# ── Predicción ────────────────────────────────────────────────

def predict_regimes(df: pd.DataFrame, model: GaussianHMM,
                    scaler: StandardScaler, labels: dict) -> pd.Series:
    """Retorna una Series con el nombre de régimen por barra."""
    X, index = compute_features(df)
    X_scaled = scaler.transform(X)
    states   = model.predict(X_scaled)
    return pd.Series([labels.get(s, f"STATE_{s}") for s in states], index=index, name="regime")


# ── Persistencia ──────────────────────────────────────────────

def save_model(symbol: str, model: GaussianHMM, scaler: StandardScaler,
               labels: dict, models_dir: str = MODELS_DIR) -> str:
    os.makedirs(models_dir, exist_ok=True)
    path = os.path.join(models_dir, f"hmm_{symbol}.pkl")
    joblib.dump({"model": model, "scaler": scaler, "labels": labels}, path)
    return path


def load_model(symbol: str, models_dir: str = MODELS_DIR) -> tuple:
    """Retorna (model, scaler, labels). Lanza FileNotFoundError si no existe."""
    path = os.path.join(models_dir, f"hmm_{symbol}.pkl")
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Modelo no encontrado: {path}\n"
            f"Corré primero: python backtest/regime_trainer.py"
        )
    bundle = joblib.load(path)
    return bundle["model"], bundle["scaler"], bundle["labels"]


# ── Reporte de estadísticas ───────────────────────────────────

def print_regime_stats(symbol: str, model: GaussianHMM, scaler: StandardScaler,
                       labels: dict, X_scaled: np.ndarray) -> None:
    states = model.predict(X_scaled)
    means  = scaler.inverse_transform(model.means_)
    n      = len(states)

    print(f"\n{'='*58}")
    print(f"  {symbol}  —  {n} barras 4h  ({n * 4 / 24:.0f} días)")
    print(f"{'='*58}")

    for state_id, name in sorted(labels.items(), key=lambda x: x[1]):
        mask  = states == state_id
        count = int(mask.sum())
        pct   = count / n * 100
        m     = means[state_id]
        print(f"\n  [{name}]  estado {state_id}")
        print(f"    Barras:         {count:>5}  ({pct:5.1f}%)")
        print(f"    Ret medio 4h:   {m[0]*100:+.3f}%")
        print(f"    Volatilidad:    {m[1]*100:.3f}%")
        print(f"    Vol ratio:      {m[2]:.2f}x")
        print(f"    RSI medio:      {m[3]*50 + 50:.1f}")
        print(f"    EMA50 slope:    {m[4]*100:+.4f}%/barra")

    # Matriz de transición
    names_ordered = [labels.get(i, f"S{i}") for i in range(model.n_components)]
    col_w = 11
    print(f"\n  Matriz de transición (prob de pasar de fila → columna):")
    header = "  " + " " * 13 + "".join(f"{n[:col_w]:>{col_w}}" for n in names_ordered)
    print(header)
    for i, row in enumerate(model.transmat_):
        name = labels.get(i, f"S{i}")
        vals = "".join(f"{v:{col_w}.3f}" for v in row)
        print(f"  {name[:12]:>12}: {vals}")


# ── Main ──────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Entrenador HMM de régimen de mercado")
    parser.add_argument("--symbol", default=None, help="Par específico (ej: BTCUSDT)")
    parser.add_argument("--states", type=int, default=4, help="Número de estados HMM (default: 4)")
    args = parser.parse_args()

    files = sorted(f for f in os.listdir(DATA_DIR) if f.endswith(f"_{INTERVAL}.csv"))
    if not files:
        print(f"No hay archivos {INTERVAL} en {DATA_DIR}.")
        print("Corré primero: python downloader.py")
        sys.exit(1)

    if args.symbol:
        files = [f for f in files if args.symbol.upper() in f]
        if not files:
            print(f"No se encontró archivo para {args.symbol} en {INTERVAL}.")
            sys.exit(1)

    print(f"\nEntrenando HMM  |  estados={args.states}  |  reinicios={N_RESTARTS}")
    print(f"Datos: {DATA_DIR}  →  Modelos: {MODELS_DIR}\n")

    for fname in files:
        symbol = fname.replace(f"_{INTERVAL}.csv", "")
        fpath  = os.path.join(DATA_DIR, fname)

        print(f"── {symbol} ──")
        df = pd.read_csv(fpath, index_col=0, parse_dates=True)

        try:
            X, index = compute_features(df)
        except Exception as e:
            print(f"  ERROR calculando features: {e}\n")
            continue

        print(f"  Features OK  |  {len(X)} barras válidas")

        scaler   = StandardScaler()
        X_scaled = scaler.fit_transform(X)

        print(f"  Entrenando HMM ({N_RESTARTS} reinicios)...", end=" ", flush=True)
        model = train_hmm(X_scaled, n_components=args.states)
        ll    = model.score(X_scaled)
        print(f"log-likelihood: {ll:.2f}")

        labels = label_states(model, scaler)
        path   = save_model(symbol, model, scaler, labels)
        print(f"  Modelo guardado: {path}")

        print_regime_stats(symbol, model, scaler, labels, X_scaled)

    print(f"\n{'='*58}")
    print("  Entrenamiento completo.")
    print("  Siguiente paso: python simulator.py --regime")
    print(f"{'='*58}\n")


if __name__ == "__main__":
    main()
