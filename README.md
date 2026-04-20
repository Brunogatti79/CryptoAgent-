# Crypto Agent — Guía de arranque (Windows)

## Estructura del proyecto
```
crypto_agent/
├── config.py          ← TUS KEYS VAN ACÁ
├── data.py            ← obtiene precios e indicadores
├── brain.py           ← llama a Claude, parsea señales
├── telegram_alerts.py ← envía mensajes al bot
├── main.py            ← loop principal
├── requirements.txt   ← dependencias
└── agent.log          ← se crea automáticamente al correr
```

## Setup (una sola vez)

### 1. Crear carpeta y entorno virtual
```cmd
mkdir crypto_agent
cd crypto_agent
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Completar config.py
Abrí `config.py` y reemplazá los valores:
```python
BINANCE_API_KEY    = "tu key real de testnet.binance.vision"
BINANCE_API_SECRET = "tu secret real"
ANTHROPIC_API_KEY  = "sk-ant-..."
TELEGRAM_TOKEN     = "123456:ABC..."
TELEGRAM_CHAT_ID   = "123456789"
```

### 3. Correr el agente
```cmd
# Con el entorno virtual activado:
python main.py
```

O desde Claude Code:
```bash
claude
# dentro de Claude Code:
> python main.py
```

## Qué vas a ver en Telegram

**Al arrancar:**
```
🤖 Crypto Agent iniciado
⏰ 2025-01-15 14:30
📡 Monitoreando BTC · ETH · SOL
```

**Cada 15 minutos, una señal por activo:**
```
👁 SEÑAL NEUTRAL
────────────────
➡️ BTC/USDT  →  NEUTRAL
💡 Convicción: 5/10
💰 Precio actual: $97,432.00
📝 Tesis: RSI en zona neutral, tendencia alcista pero sin catalizador...
```

**Si hay señal accionable (convicción >= 7):**
```
⚡ SEÑAL ACCIONABLE
────────────────
📈 ETH/USDT  →  LONG
💡 Convicción: 8/10
💰 Precio actual: $3,241.00
🎯 Entrada:     $3,200.00
🛑 Stop-loss:   $3,072.00
✅ Take-profit:  $3,520.00
⚖️ Ratio R/B:   2.5:1
```

## Detener el agente
`Ctrl + C` en la terminal. El agente manda alerta a Telegram.

## Próximo paso: agregar ejecución en Testnet
Una vez que el pipeline funciona (ves señales en Telegram), agregamos
el módulo `executor.py` que conecta con Binance Testnet para ejecutar
las órdenes automáticamente.
