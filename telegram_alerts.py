# =============================================================
#  CRYPTO AGENT — TELEGRAM ALERTS
#  Envía mensajes y alertas al bot configurado
# =============================================================

import requests
from datetime import datetime
from config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID


BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# Traducción de regímenes al castellano
REGIME_NOMBRES = {
    "BULL_TREND": "Tendencia alcista",
    "BEAR_TREND": "Tendencia bajista",
    "SIDEWAYS":   "Movimiento lateral",
    "REVERSAL":   "Recuperación",
    "UNKNOWN":    "Desconocido",
}

# Traducción de direcciones al castellano
DIRECTION_NOMBRES = {
    "LONG":    "Compra",
    "SHORT":   "Venta",
    "NEUTRAL": "Neutral",
}

# Traducción del Fear & Greed al castellano
FNG_NOMBRES = {
    "Extreme Fear":   "Miedo extremo",
    "Fear":           "Miedo",
    "Neutral":        "Neutral",
    "Greed":          "Codicia",
    "Extreme Greed":  "Codicia extrema",
}


def _fng_label(label: str) -> str:
    return FNG_NOMBRES.get(label, label)


def send(text: str, silent: bool = False) -> bool:
    """Envía un mensaje de texto al chat configurado."""
    try:
        r = requests.post(
            f"{BASE_URL}/sendMessage",
            json={
                "chat_id":              TELEGRAM_CHAT_ID,
                "text":                 text,
                "parse_mode":           "HTML",
                "disable_notification": silent,
            },
            timeout=10,
        )
        return r.status_code == 200
    except Exception as e:
        print(f"[Telegram ERROR] {e}")
        return False


def send_startup() -> None:
    from config import SYMBOLS, MONITOR_INTERVAL_MINUTES, ANALYSIS_INTERVAL_MINUTES
    simbolos_str  = " · ".join(s.replace("/USDT", "") for s in SYMBOLS)
    monitor_str   = f"{MONITOR_INTERVAL_MINUTES} minutos"
    analisis_str  = (
        f"{ANALYSIS_INTERVAL_MINUTES // 60} horas"
        if ANALYSIS_INTERVAL_MINUTES >= 60
        else f"{ANALYSIS_INTERVAL_MINUTES} minutos"
    )
    send(
        "🤖 <b>Agente de trading iniciado</b>\n"
        f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        f"📡 Monitoreando: {simbolos_str}\n"
        f"🔍 Control de posiciones cada {monitor_str}\n"
        f"🧠 Análisis Claude cada {analisis_str}\n"
        "────────────────────"
    )


def send_signal(signal: dict, market_data: dict) -> None:
    """Formatea y envía una señal de trading."""
    simbolo    = signal.get("symbol", "?")
    direccion  = signal.get("direction", "?")
    conviccion = signal.get("conviction", 0)
    accionable = signal.get("actionable", False)

    precio_actual = ""
    if simbolo in market_data and "price" in market_data[simbolo]:
        precio_actual = f"${market_data[simbolo]['price']:,.2f}"

    dir_emoji  = {"LONG": "📈", "SHORT": "📉", "NEUTRAL": "➡️"}.get(direccion, "❓")
    dir_nombre = DIRECTION_NOMBRES.get(direccion, direccion)
    encabezado = "⚡ <b>SEÑAL ACCIONABLE</b>" if accionable else "👁 <b>SEÑAL NEUTRAL</b>"

    msg = (
        f"{encabezado}\n"
        f"────────────────────\n"
        f"{dir_emoji} <b>{simbolo}</b>  →  <b>{dir_nombre}</b>\n"
        f"💡 Convicción: {conviccion}/10\n"
        f"💰 Precio actual: {precio_actual}\n"
        f"🎯 Precio de entrada:   {signal.get('entry', 'No disponible')}\n"
        f"🛑 Precio de stop loss: {signal.get('stop_loss', 'No disponible')}\n"
        f"✅ Precio objetivo:     {signal.get('take_profit', 'No disponible')}\n"
        f"⚖️ Ratio riesgo/beneficio: {signal.get('ratio', 'No disponible')}\n"
        f"📝 Tesis: {signal.get('thesis', 'No disponible')}\n"
        f"────────────────────\n"
        f"🕐 {datetime.now().strftime('%H:%M:%S')}"
    )
    send(msg)


def send_cycle_summary(signals: list[dict], fng: dict, tokens_used: int,
                       balance_usdt: float = 0, regimes: dict = None) -> None:
    """Resumen al final de cada ciclo de análisis."""
    accionables = [s for s in signals if s.get("actionable")]
    neutrales   = [s for s in signals if not s.get("actionable")]

    regime_icons = {
        "BULL_TREND": "📈",
        "BEAR_TREND": "📉",
        "SIDEWAYS":   "➡️",
        "REVERSAL":   "🔄",
    }

    lineas = [
        "📊 <b>Resumen del ciclo de análisis</b>",
        f"🧠 Índice Miedo y Codicia: {fng['value']}/100 ({_fng_label(fng['label'])})",
    ]

    if regimes:
        lineas.append("── Régimen de mercado (modelo HMM) ──")
        for simbolo, info in regimes.items():
            if info.get("available"):
                icono  = regime_icons.get(info["regime"], "❓")
                nombre = REGIME_NOMBRES.get(info["regime"], info["regime"])
                horas  = info["hours_in_regime"]
                lineas.append(
                    f"{icono} {simbolo}: <b>{nombre}</b> "
                    f"({horas} horas consecutivas)"
                )

    lineas += [
        "────────────────────",
        f"⚡ Señales accionables: {len(accionables)}",
        f"➡️ Señales neutrales: {len(neutrales)}",
        f"💵 Saldo disponible en USDT: ${balance_usdt:,.2f}",
        f"🔤 Tokens utilizados de Claude: {tokens_used}",
        f"🕐 {datetime.now().strftime('%H:%M:%S')}",
    ]
    send("\n".join(lineas), silent=True)


def send_error(context: str, error: str) -> None:
    send(
        f"⚠️ <b>Error en el agente</b>\n"
        f"📍 Contexto: {context}\n"
        f"❌ Detalle: {error[:200]}"
    )


def send_daily_limit_hit(loss_usd: float) -> None:
    send(
        f"🚨 <b>LÍMITE DIARIO DE PÉRDIDA ALCANZADO</b>\n"
        f"💸 Pérdida acumulada en el día: ${loss_usd:.2f}\n"
        f"🛑 El agente se detiene hasta mañana.\n"
        f"📋 Revisá el registro antes de reiniciar."
    )


def send_trade_closed(trade: dict) -> None:
    resultado  = trade.get("result", "?")
    emoji      = "✅" if resultado == "WIN" else "🔴"
    resultado_nombre = "Ganancia" if resultado == "WIN" else "Pérdida"
    pnl        = trade.get("pnl_usd", 0)
    pnl_signo  = "+" if pnl >= 0 else ""
    direccion  = trade.get("direction", "")
    dir_emoji  = {"LONG": "📈", "SHORT": "📉"}.get(direccion, "")
    dir_nombre = DIRECTION_NOMBRES.get(direccion, direccion)
    send(
        f"{emoji} <b>OPERACIÓN CERRADA — {trade['symbol']}</b>\n"
        f"────────────────────\n"
        f"{dir_emoji} Dirección: {dir_nombre}\n"
        f"📊 Resultado: <b>{resultado_nombre}</b>\n"
        f"💰 Precio de entrada: ${trade['entry_price']:,.4f}\n"
        f"🏁 Precio de salida:  ${trade['exit_price']:,.4f}\n"
        f"💵 Ganancia / Pérdida: {pnl_signo}${pnl:.2f}\n"
        f"🕐 {datetime.now().strftime('%H:%M:%S')}"
    )


def send_execution_confirmation(result: dict) -> None:
    direccion  = result.get("direction", "?")
    dir_emoji  = {"LONG": "📈", "SHORT": "📉"}.get(direccion, "❓")
    dir_nombre = DIRECTION_NOMBRES.get(direccion, direccion)
    msg = (
        f"✅ <b>ORDEN EJECUTADA — {result['symbol']}</b>\n"
        f"────────────────────\n"
        f"{dir_emoji} Dirección: {dir_nombre}\n"
        f"💰 Precio de entrada:       ${result['entry_price']:,.4f}\n"
        f"🛑 Precio de stop loss:     ${result['stop_loss']:,.4f}\n"
        f"🎯 Precio objetivo:         ${result['take_profit']:,.4f}\n"
        f"📦 Cantidad operada:        {result['quantity']} (aproximadamente ${result['usd_value']:.2f} dólares)\n"
        f"🔑 Identificador de orden:  {result['order_id']}\n"
        f"🗄 Número de operación:     #{result['trade_id']}\n"
        f"🕐 {datetime.now().strftime('%H:%M:%S')}"
    )
    send(msg)
