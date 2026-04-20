# =============================================================
#  CRYPTO AGENT — BRAIN
#  Llama a Claude con el contexto de mercado y parsea la señal
# =============================================================

import json
import re
import anthropic
from config import ANTHROPIC_API_KEY, MIN_SIGNAL_CONVICTION

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """Sos un analista cuantitativo senior con 20 años de experiencia en crypto y mercados globales.
Tu función es evaluar el contexto de mercado provisto y emitir señales de trading con rigor institucional.

PROCESO OBLIGATORIO:
1. Leé el RÉGIMEN DE MERCADO (HMM 4h) — es la capa más importante del análisis
2. Evaluá el Fear & Greed Index como termómetro de sentiment general
3. Para cada activo analizá: tendencia (EMA 4h), momentum (RSI 4h), volumen relativo
4. Cruzá los activos para detectar correlaciones o divergencias

CÓMO USAR EL RÉGIMEN HMM:
- BULL_TREND: sesgo LONG. Volatilidad baja → stops estándar son eficientes.
- BEAR_TREND: volatilidad 2x normal → stops fijos se triggean por ruido. Reducir tamaño o NEUTRAL.
- SIDEWAYS: deriva bajista gradual. Sin trades de trend-following, esperar ruptura.
- REVERSAL: recuperación post-bear. LONGs tempranos posibles con RSI no sobrecomprado (< 60).
- Si el régimen lleva pocas barras (< 6), puede ser una transición — ser más conservador.

FORMATO DE RESPUESTA — siempre exactamente este bloque para cada activo:
ACTIVO: [símbolo]
DIRECCIÓN: LONG | SHORT | NEUTRAL
CONVICCIÓN: [1-10]
ENTRADA: [precio sugerido o MERCADO]
STOP-LOSS: [precio]
TAKE-PROFIT: [precio]
RATIO R/B: [x:1]
TESIS: [máximo 2 líneas: señal + qué la invalidaría + cómo el régimen la refuerza o debilita]
---

REGLAS ABSOLUTAS:
- Solo emitir LONG o SHORT si convicción >= 7
- RSI > 75 → no entrar long. RSI < 25 → no entrar short
- En BEAR_TREND: convicción mínima 8 para operar (entorno desfavorable)
- Si Fear & Greed < 20 (Extreme Fear) → solo considerar LONG de largo plazo
- Si Fear & Greed > 80 (Extreme Greed) → sesgo bajista, ser cauteloso con longs
- Ratio riesgo/beneficio mínimo 2:1 para cualquier señal no-NEUTRAL
- Respondé siempre en español, sé directo y cuantitativo"""


SYSTEM_PROMPT_B = """Sos un analista de momentum especializado en activos cripto de alta volatilidad.
Se te presentan activos que acaban de moverse significativamente (>8% en 24h) — Grupo B.
Tu función es evaluar si el movimiento tiene continuación o es un pico de agotamiento.

CONTEXTO IMPORTANTE:
- Estos activos NO tienen modelo HMM entrenado. Solo indicadores técnicos disponibles.
- El riesgo es materialmente mayor que en activos del Grupo A (BTC, ETH, SOL, AVAX).
- Parámetros fijos: stop 3% desde entrada, target mínimo 15% (ratio mínimo 5:1).
- Solo entrar si las señales técnicas CONFIRMAN el momentum — no perseguir pumps agotados.

CRITERIOS DE ENTRADA:
- LONG: EMA20 > EMA50, RSI entre 45–68, volumen > 1.5x promedio, momentum positivo sostenido.
- SHORT: EMA20 < EMA50, RSI entre 32–55, volumen alto, dump sostenido con velas rojas.
- NEUTRAL si: RSI > 72 (pump agotado), RSI < 28 (dump agotado), o movimiento fue 1 sola vela sin continuación.

FORMATO DE RESPUESTA — exactamente este bloque por activo:
ACTIVO: [símbolo]
DIRECCIÓN: LONG | SHORT | NEUTRAL
CONVICCIÓN: [1-10]
ENTRADA: [precio o MERCADO]
STOP-LOSS: [precio — máx 3% desde entrada]
TAKE-PROFIT: [precio — mín 15% desde entrada]
RATIO R/B: [x:1]
TESIS: [máximo 2 líneas: qué confirma el momentum + qué lo invalidaría]
---

REGLAS ABSOLUTAS:
- RSI > 72 → NEUTRAL obligatorio (trampa alcista)
- RSI < 28 → NEUTRAL obligatorio
- Ratio mínimo 4:1 — si no se puede construir, NEUTRAL
- Respondé siempre en español, sé directo"""


SYSTEM_PROMPT_VETO = """Sos el risk manager de un sistema de trading algorítmico.
El sistema ya validó una entrada técnica mecánicamente. Tu único trabajo es detectar razones OBJETIVAS para no ejecutar.

VETÁ solo si detectás algo concreto:
- Evento macroeconómico inminente conocido (FOMC, CPI, halving, hard fork en las próximas horas)
- Divergencia RSI obvia (precio en nuevo máximo, RSI más bajo que el anterior)
- BTC en caída libre simultánea mientras el activo es altcoin
- Sobreextensión técnica extrema (>3 velas consecutivas de >5% sin retroceso)

NO vetés por: incertidumbre general, "podría bajar", falta de datos, volatilidad normal.
En caso de duda, NO vetés — la señal técnica ya fue validada.

Respondé ÚNICAMENTE con JSON válido, sin texto adicional:
{"veto": false, "reason": ""}
{"veto": true, "reason": "razón concreta en 1 línea"}"""


def analyze_veto(symbol: str, direction: str, conditions: dict,
                 market_data: dict, regime_context: str = "") -> dict:
    """
    Claude como veto de última instancia sobre una entrada ya calificada mecánicamente.
    Usa claude-haiku (más barato y rápido — la tarea es simple).
    Retorna: {veto: bool, reason: str, tokens: int}
    """
    d = market_data.get(symbol, {})
    context = (
        f"Setup validado mecánicamente:\n"
        f"  Activo:     {symbol}\n"
        f"  Dirección:  {direction}\n"
        f"  Condiciones:{', '.join(conditions.get('reasons', []))}\n"
        f"  Precio:     ${d.get('price', 0):,.4f}\n"
        f"  RSI:        {d.get('rsi', 0):.1f}\n"
        f"  Cambio 24h: {d.get('change_24h', 0):+.2f}%\n"
        f"  Volumen:    {d.get('vol_ratio', 0):.2f}x promedio\n"
    )
    if regime_context:
        context += f"\nContexto de régimen:\n{regime_context}"

    try:
        response = client.messages.create(
            model="claude-haiku-4-20250514",
            max_tokens=150,
            system=SYSTEM_PROMPT_VETO,
            messages=[{"role": "user", "content": context}]
        )
        raw    = response.content[0].text.strip()
        tokens = response.usage.input_tokens + response.usage.output_tokens
        parsed = json.loads(raw)
        return {
            "veto":   bool(parsed.get("veto", False)),
            "reason": parsed.get("reason", ""),
            "tokens": tokens,
        }
    except json.JSONDecodeError:
        # Si no parsea el JSON, aprobamos — la señal técnica ya fue validada
        return {"veto": False, "reason": "parse error — señal aprobada por default", "tokens": 0}
    except Exception as e:
        return {"veto": False, "reason": f"error Claude: {e}", "tokens": 0}


def analyze_group_b(market_context: str) -> dict:
    """Análisis de momentum para activos Grupo B (sin régimen HMM)."""
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=800,
        system=SYSTEM_PROMPT_B,
        messages=[{"role": "user", "content": market_context}]
    )
    raw     = response.content[0].text
    signals = _parse_signals(raw)
    for s in signals:
        s['group'] = 'B'
    return {
        "raw_response":  raw,
        "signals":       signals,
        "input_tokens":  response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
    }


def analyze(market_context: str, regime_context: str = "") -> dict:
    """
    Envía el contexto a Claude y retorna:
    - raw_response: texto completo de Claude
    - signals: lista de señales parseadas por activo
    """
    full_context = f"{regime_context}\n\n{market_context}" if regime_context else market_context

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1500,
        system=SYSTEM_PROMPT,
        messages=[
            {"role": "user", "content": full_context}
        ]
    )

    raw     = response.content[0].text
    signals = _parse_signals(raw)

    return {
        "raw_response":  raw,
        "signals":       signals,
        "input_tokens":  response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
    }


def _parse_signals(text: str) -> list[dict]:
    """Extrae señales estructuradas del texto de Claude."""
    # Limpiar markdown bold que Claude a veces agrega
    clean = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    
    signals = []
    blocks = re.split(r'\n---\n', clean)

    for block in blocks:
        signal = {}

        patterns = {
            "symbol":     r"ACTIVO:\s*(.+)",
            "direction":  r"DIRECCIÓN:\s*(LONG|SHORT|NEUTRAL)",
            "conviction": r"CONVICCIÓN:\s*(\d+)",
            "entry":      r"ENTRADA:\s*(.+)",
            "stop_loss":  r"STOP-LOSS:\s*(.+)",
            "take_profit":r"TAKE-PROFIT:\s*(.+)",
            "ratio":      r"RATIO R/B:\s*(.+)",
            "thesis":     r"TESIS:\s*(.+)",
        }

        for key, pattern in patterns.items():
            m = re.search(pattern, block, re.IGNORECASE)
            if m:
                signal[key] = m.group(1).strip()

        if "symbol" in signal and "direction" in signal:
            conviction = int(signal.get("conviction", 0))
            signal["conviction"] = conviction
            signal["actionable"] = (
                signal["direction"] != "NEUTRAL"
                and conviction >= MIN_SIGNAL_CONVICTION
            )
            signals.append(signal)

    return signals
