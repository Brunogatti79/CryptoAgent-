# =============================================================
#  CRYPTO AGENT — CONFIG
#  Las secrets vienen de variables de entorno (.env local o Railway vars)
# =============================================================

import os
from dotenv import load_dotenv

load_dotenv()  # carga .env si existe (local); en Railway usa env vars directas

# --- Binance ---
BINANCE_API_KEY    = os.environ["BINANCE_API_KEY"]
BINANCE_API_SECRET = os.environ["BINANCE_API_SECRET"]
BINANCE_TESTNET    = os.getenv("BINANCE_TESTNET", "true").lower() == "true"

# --- Anthropic ---
ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]

# --- Telegram ---
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# --- GitHub Gist (dashboard remoto) ---
GITHUB_GIST_TOKEN = os.getenv("GITHUB_GIST_TOKEN", "")
GITHUB_GIST_ID    = os.getenv("GITHUB_GIST_ID", "")

# --- Pares que el agente monitorea ---
SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]

# --- Reglas de riesgo (hardcoded, el agente no las puede cambiar) ---
MAX_TRADE_USD         = float(os.getenv("MAX_TRADE_USD",      "50"))  # por operación en USD
STOP_LOSS_PCT         = 0.04                                           # 4% fallback si ATR falla
MAX_DAILY_LOSS_USD    = float(os.getenv("MAX_DAILY_LOSS_USD", "30"))  # ~3 pérdidas ATR × $50
MAX_OPEN_POSITIONS    = 2                                              # máx posiciones simultáneas
MIN_SIGNAL_CONVICTION = 8                                              # convicción mínima Claude

# --- API interna ---
AGENT_API_TOKEN = os.getenv("AGENT_API_TOKEN", "")   # protege /api/close
PORT            = int(os.getenv("PORT", "8080"))       # Railway lo setea automáticamente

# --- Grupo B — altcoins volátiles (top movers diarios) ---
GROUP_B_ENABLED        = os.getenv("GROUP_B_ENABLED",       "true").lower() == "true"
GROUP_B_MAX_POSITIONS  = int(os.getenv("GROUP_B_MAX_POSITIONS",  "1"))
GROUP_B_TOP_MOVERS     = int(os.getenv("GROUP_B_TOP_MOVERS",     "2"))
GROUP_B_MIN_CHANGE_PCT = float(os.getenv("GROUP_B_MIN_CHANGE_PCT", "5.0"))
GROUP_B_MIN_VOLUME_USD = float(os.getenv("GROUP_B_MIN_VOLUME_USD", "50000000"))
STOP_LOSS_PCT_B        = float(os.getenv("STOP_LOSS_PCT_B",  "0.03"))
TAKE_PROFIT_PCT_B      = float(os.getenv("TAKE_PROFIT_PCT_B","0.15"))

# --- Intervalos ---
MONITOR_INTERVAL_MINUTES  = int(os.getenv("MONITOR_INTERVAL_MINUTES",  "15"))   # SL/TP check
ANALYSIS_INTERVAL_MINUTES = int(os.getenv("ANALYSIS_INTERVAL_MINUTES", "240"))  # régimen + Claude
