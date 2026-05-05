# =============================================================
#  CRYPTO AGENT — TELEGRAM ALERTS
#  Envía mensajes y alertas al bot configurado
# =============================================================
 
import requests
from datetime import datetime
from config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
 
 
BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
 
 
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
    symbols_str   = " · ".join(s.replace("/USDT", "") for s in SYMBOLS)
    monitor_str   = f"{MONITOR_INTERVAL_MINUTES}min"
    analysis_str  = f"{ANALYSIS_INTERVAL_MINUTES // 60}h" if ANALYSIS_INTERVAL_MINUTES >= 60 else f"{ANALYSIS_INTERVAL_MINUTES}min"
    send(
        "🤖 <b>Crypto Agent iniciado</b>\n"
        f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        f"📡 Monitoreando {symbols_str}\n"
        f"🔍 Monitor cada {monitor_str} · Análisis Claude cada {analysis_str}\n"
        "────────────────────\n"
        "🖥 <a href=\"https://cryptoagent.up.railway.app\">Ver dashboard</a>"
    )
 
 
def send_signal(signal: dict, market_data: dict) -> None:
    """Formatea y envía una señal de trading."""
    sym = signal.get("symbol", "?")
    direction = signal.get("direction", "?")
    conviction = signal.get("conviction", 0)
    actionable = signal.get("actionable", False)
 
    price_now = ""
    if sym in market_data and "price" in market_data[sym]:
        price_now = f"${market_data[sym]['price']:,.2f}"
 
    dir_emoji = {"LONG": "📈", "SHORT": "📉", "NEUTRAL": "➡️"}.get(direction, "❓")
    action_tag = "⚡ <b>SEÑAL ACCIONABLE</b>" if actionable else "👁 <b>SEÑAL NEUTRAL</b>"
 
    msg = (
        f"{action_tag}\n"
        f"────────────────────\n"
        f"{dir_emoji} <b>{sym}</b>  →  <b>{direction}</b>\n"
        f"💡 Convicción: {conviction}/10\n"
        f"💰 Precio actual: {price_now}\n"
        f"🎯 Entrada:      {signal.get('entry', 'N/A')}\n"
        f"🛑 Stop-loss:    {signal.get('stop_loss', 'N/A')}\n"
        f"✅ Take-profit:  {signal.get('take_profit', 'N/A')}\n"
        f"⚖️ Ratio R/B:    {signal.get('ratio', 'N/A')}\n"
        f"📝 Tesis: {signal.get('thesis', 'N/A')}\n"
        f"────────────────────\n"
        f"⏰ {datetime.now().strftime('%H:%M:%S')}"
    )
    send(msg)
 
 
def send_cycle_summary(signals: list[dict], fng: dict, tokens_used: int,
                       balance_usdt: float = 0, regimes: dict = None) -> None:
    """Resumen al final de cada ciclo de análisis."""
    actionable = [s for s in signals if s.get("actionable")]
    neutral    = [s for s in signals if not s.get("actionable")]
 
    regime_icons = {
        "BULL_TREND": "📈", "BEAR_TREND": "📉",
        "SIDEWAYS":   "➡️",  "REVERSAL":   "🔄",
    }
 
    lines = [
        "📊 <b>Resumen del ciclo (4h)</b>",
        f"🧠 Fear &amp; Greed: {fng['value']}/100 ({fng['label']})",
    ]
 
    if regimes:
        lines.append("── Régimen HMM ──")
        for sym, info in regimes.items():
            if info.get("available"):
                icon = regime_icons.get(info["regime"], "❓")
                lines.append(
                    f"{icon} {sym}: <b>{info['regime']}</b> "
                    f"({info['hours_in_regime']}h)"
                )
 
    lines += [
        "────────────────────",
        f"⚡ Señales accionables: {len(actionable)}",
        f"➡️ Neutrales: {len(neutral)}",
        f"💵 Balance USDT: ${balance_usdt:,.2f}",
        f"🔤 Tokens Claude: {tokens_used}",
        f"⏰ {datetime.now().strftime('%H:%M:%S')}",
        "🖥 <a href=\"https://cryptoagent.up.railway.app\">Ver dashboard</a>",
    ]
    send("\n".join(lines), silent=True)
 
 
def send_error(context: str, error: str) -> None:
    send(
        f"⚠️ <b>Error en el agente</b>\n"
        f"📍 Contexto: {context}\n"
        f"❌ Error: {error[:200]}"
    )
 
 
def send_daily_limit_hit(loss_usd: float) -> None:
    send(
        f"🚨 <b>LÍMITE DIARIO ALCANZADO</b>\n"
        f"💸 Pérdida del día: ${loss_usd:.2f}\n"
        f"🛑 El agente se detiene hasta mañana.\n"
        f"📋 Revisá el log antes de reiniciar."
    )
 
 
def send_trade_closed(trade: dict) -> None:
    result    = trade.get("result", "?")
    emoji     = "✅" if result == "WIN" else "🔴"
    pnl       = trade.get("pnl_usd", 0)
    pnl_sign  = "+" if pnl >= 0 else ""
    dir_emoji = {"LONG": "📈", "SHORT": "📉"}.get(trade.get("direction"), "")
    send(
        f"{emoji} <b>TRADE CERRADO — {trade['symbol']}</b>\n"
        f"────────────────────\n"
        f"{dir_emoji} {trade['direction']} → <b>{result}</b>\n"
        f"💰 Entrada:  ${trade['entry_price']:,.4f}\n"
        f"🏁 Salida:   ${trade['exit_price']:,.4f}\n"
        f"💵 PnL:      {pnl_sign}${pnl:.2f}\n"
        f"⏰ {datetime.now().strftime('%H:%M:%S')}\n"
        "🖥 <a href=\"https://cryptoagent.up.railway.app\">Ver dashboard</a>"
    )
 
 
def send_execution_confirmation(result: dict) -> None:
    direction = result.get("direction", "?")
    dir_emoji = {"LONG": "📈", "SHORT": "📉"}.get(direction, "❓")
    msg = (
        f"✅ <b>ORDEN EJECUTADA — {result['symbol']}</b>\n"
        f"────────────────────\n"
        f"{dir_emoji} {direction}\n"
        f"💰 Precio entrada:  ${result['entry_price']:,.4f}\n"
        f"🛑 Stop-loss:       ${result['stop_loss']:,.4f}\n"
        f"🎯 Take-profit:     ${result['take_profit']:,.4f}\n"
        f"📦 Cantidad:        {result['quantity']} (~${result['usd_value']:.2f} USD)\n"
        f"🔑 Order ID:        {result['order_id']}\n"
        f"🗄 Trade DB ID:     #{result['trade_id']}\n"
        f"⏰ {datetime.now().strftime('%H:%M:%S')}\n"
        "🖥 <a href=\"https://cryptoagent.up.railway.app\">Ver dashboard</a>"
    )
    send(msg)
 
