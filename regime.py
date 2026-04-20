# =============================================================
#  CRYPTO AGENT — REGIME CLASSIFIER (producción)
#  Clasifica el régimen de mercado actual usando el HMM entrenado.
#
#  Uso:  import regime
#        regimes = regime.classify_all(["BTC/USDT", "ETH/USDT"])
#        context = regime.format_regime_context(regimes)
# =============================================================

import os
import logging
import numpy as np
import pandas as pd
import requests

log = logging.getLogger(__name__)

MODELS_DIR = os.path.join(os.path.dirname(__file__), "models")

# Descripción human-readable de cada régimen para el prompt de Claude
REGIME_DESCRIPTIONS = {
    "BULL_TREND": {
        "desc":      "Tendencia alcista sostenida, volatilidad baja",
        "bias":      "Favorable para LONG",
        "risk_note": "Volatilidad baja → stops estándar son eficientes",
    },
    "BEAR_TREND": {
        "desc":      "Caída pronunciada con alta volatilidad (2x normal)",
        "bias":      "Muy volátil — stops fijos pueden triggear por ruido",
        "risk_note": "Considerar stops más amplios o reducir tamaño",
    },
    "SIDEWAYS": {
        "desc":      "Declive gradual con la menor volatilidad de todos los estados",
        "bias":      "Deriva bajista suave — SHORTs con tendencia 1h pueden funcionar",
        "risk_note": "Baja dirección en 4h, esperar confirmación",
    },
    "REVERSAL": {
        "desc":      "Recuperación post-bear con volatilidad media",
        "bias":      "Posibles LONGs tempranos, retorno medio positivo",
        "risk_note": "Estado de transición — vigilar cambio de régimen",
    },
}


# ── Cache de modelos en memoria ───────────────────────────────

_model_cache: dict = {}


def _load_model(binance_symbol: str) -> tuple:
    """Carga y cachea el modelo HMM. Retorna (model, scaler, labels) o (None, None, None)."""
    if binance_symbol in _model_cache:
        return _model_cache[binance_symbol]

    path = os.path.join(MODELS_DIR, f"hmm_{binance_symbol}.pkl")
    if not os.path.exists(path):
        log.warning(f"[regime] Modelo no encontrado: {path} — corré regime_trainer.py")
        return None, None, None

    try:
        import joblib
        bundle = joblib.load(path)
        _model_cache[binance_symbol] = (bundle["model"], bundle["scaler"], bundle["labels"])
        log.info(f"[regime] Modelo cargado: {binance_symbol}")
        return _model_cache[binance_symbol]
    except Exception as e:
        log.error(f"[regime] Error cargando modelo {binance_symbol}: {e}")
        return None, None, None


# ── Features (idéntico a backtest/regime_trainer.py) ─────────

def _compute_features(df: pd.DataFrame) -> tuple[np.ndarray, pd.Index]:
    """
    Computa las 5 features del HMM. DEBE ser idéntico a regime_trainer.py.
    Features: log_ret, vol_20, vol_ratio, rsi_centered, ema50_slope
    """
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


# ── Fetch de velas 4h ─────────────────────────────────────────

def _get_candles_4h(symbol: str, limit: int = 200) -> pd.DataFrame:
    binance_symbol = symbol.replace("/", "")
    url = (
        f"https://api.binance.com/api/v3/klines"
        f"?symbol={binance_symbol}&interval=4h&limit={limit}"
    )
    klines = requests.get(url, timeout=15).json()
    df = pd.DataFrame(klines, columns=[
        "timestamp", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore",
    ])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df.set_index("timestamp", inplace=True)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    return df[["open", "high", "low", "close", "volume"]]


# ── Clasificación ─────────────────────────────────────────────

def classify(symbol: str) -> dict:
    """
    Clasifica el régimen actual para un par.

    Retorna dict con:
      regime          — nombre del régimen ("BULL_TREND", etc.)
      bars_in_regime  — barras 4h consecutivas en este régimen
      hours_in_regime — equivalente en horas
      prev_regime     — régimen anterior (antes de la racha actual)
      persist_prob    — prob de mantenerse en este régimen (de la transmat)
      vol_state       — volatilidad media del estado actual (% por 4h)
      available       — False si no hay modelo o hubo error
    """
    binance_symbol = symbol.replace("/", "")
    model, scaler, labels = _load_model(binance_symbol)

    if model is None:
        return {"symbol": symbol, "regime": "UNKNOWN", "available": False}

    try:
        df = _get_candles_4h(symbol, limit=200)
        X, _index = _compute_features(df)

        if len(X) < 10:
            return {"symbol": symbol, "regime": "UNKNOWN", "available": False,
                    "error": "insuficientes barras"}

        X_scaled = scaler.transform(X)
        states   = model.predict(X_scaled)

        current_state  = int(states[-1])
        current_regime = labels.get(current_state, f"STATE_{current_state}")

        # Barras consecutivas en el régimen actual
        bars_in_regime = 0
        for s in reversed(states):
            if s == current_state:
                bars_in_regime += 1
            else:
                break

        # Régimen anterior
        prev_regime = None
        if bars_in_regime < len(states):
            prev_state  = int(states[-(bars_in_regime + 1)])
            prev_regime = labels.get(prev_state, f"STATE_{prev_state}")

        # Estadísticas del estado actual desde el modelo
        means         = scaler.inverse_transform(model.means_)
        vol_state     = float(means[current_state][1]) * 100   # vol_20 feature
        persist_prob  = float(model.transmat_[current_state][current_state])

        return {
            "symbol":         symbol,
            "regime":         current_regime,
            "bars_in_regime": int(bars_in_regime),
            "hours_in_regime":int(bars_in_regime * 4),
            "prev_regime":    prev_regime,
            "persist_prob":   round(persist_prob, 3),
            "vol_state":      round(vol_state, 3),
            "available":      True,
        }

    except Exception as e:
        log.error(f"[regime] Error clasificando {symbol}: {e}")
        return {"symbol": symbol, "regime": "UNKNOWN", "available": False, "error": str(e)}


def classify_all(symbols: list[str]) -> dict[str, dict]:
    """Clasifica todos los pares. Retorna {symbol: regime_info}."""
    return {sym: classify(sym) for sym in symbols}


# ── Formato para el prompt de Claude ─────────────────────────

def format_regime_context(regimes: dict[str, dict]) -> str:
    """
    Genera el bloque de texto de régimen para inyectar en el prompt de Claude.
    Solo incluye pares con modelo disponible.
    """
    lines = ["══ RÉGIMEN DE MERCADO (HMM 4h) ══"]

    for symbol, info in regimes.items():
        if not info.get("available"):
            lines.append(f"{symbol}: modelo no disponible")
            continue

        regime  = info["regime"]
        bars    = info["bars_in_regime"]
        hours   = info["hours_in_regime"]
        prev    = info.get("prev_regime")
        persist = info["persist_prob"] * 100
        vol     = info["vol_state"]
        meta    = REGIME_DESCRIPTIONS.get(regime, {})

        lines.append(f"\n{symbol} → {regime}")
        lines.append(f"  {meta.get('desc', '')}")
        lines.append(f"  Duración:      {bars} barras · {hours}h consecutivas")
        lines.append(f"  Persistencia:  {persist:.1f}% probabilidad de continuar")
        lines.append(f"  Vol del estado: {vol:.3f}%/4h")
        if prev and prev != regime:
            lines.append(f"  Transición:    {prev} → {regime}")
        lines.append(f"  Sesgo:         {meta.get('bias', '—')}")
        lines.append(f"  Riesgo:        {meta.get('risk_note', '—')}")

    return "\n".join(lines)
