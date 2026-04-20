"""
core/binance_ws.py
──────────────────
WebSocket client para Binance. Suscribe múltiples pares en una sola
conexión usando el endpoint de streams combinados.

Uso:
    ws = BinanceWebSocket(symbols=["BTC/USDT", "ETH/USDT"])
    await ws.start(on_price=my_callback)

El callback recibe: (symbol: str, price: float, high: float, low: float)
Se reconecta automáticamente con backoff exponencial.
"""

import asyncio
import json
import logging
import time

import websockets

log = logging.getLogger(__name__)

# Binance combined stream endpoint
WS_BASE = "wss://stream.binance.com:9443/stream"


class BinanceWebSocket:
    def __init__(self, symbols: list[str], reconnect_delay: float = 2.0):
        """
        symbols: lista en formato "BTC/USDT" o "BTCUSDT" — normaliza internamente.
        """
        self._symbols       = [s.replace("/", "").lower() for s in symbols]
        self._reconnect_delay = reconnect_delay
        self._running       = False
        self._on_price      = None
        # Último precio conocido por símbolo — usado por trailing stop
        self.last_prices: dict[str, dict] = {}

    # ── Interfaz pública ──────────────────────────────────────

    async def start(self, on_price):
        """
        Arranca el listener. on_price es un coroutine o función async:
            async def on_price(symbol, price, high, low): ...
        """
        self._on_price = on_price
        self._running  = True
        await self._run_with_reconnect()

    def stop(self):
        self._running = False

    # ── Internals ─────────────────────────────────────────────

    def _stream_url(self) -> str:
        streams = "/".join(f"{sym}@miniTicker" for sym in self._symbols)
        return f"{WS_BASE}?streams={streams}"

    async def _run_with_reconnect(self):
        delay = self._reconnect_delay
        while self._running:
            try:
                log.info(f"[WS] Conectando a {len(self._symbols)} streams...")
                async with websockets.connect(
                    self._stream_url(),
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    delay = self._reconnect_delay  # reset en conexión exitosa
                    log.info("[WS] Conectado ✓")
                    await self._listen(ws)
            except websockets.ConnectionClosedOK:
                if not self._running:
                    break
                log.warning("[WS] Conexión cerrada limpiamente — reconectando...")
            except Exception as e:
                log.warning(f"[WS] Error: {e} — reconectando en {delay:.0f}s")

            if self._running:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)  # backoff máx 60s

    async def _listen(self, ws):
        async for raw in ws:
            if not self._running:
                break
            try:
                msg  = json.loads(raw)
                data = msg.get("data", {})
                if data.get("e") != "24hrMiniTicker":
                    continue

                raw_sym = data["s"]                          # "BTCUSDT"
                sym     = raw_sym[:-4] + "/USDT"             # "BTC/USDT"
                price   = float(data["c"])                   # close/last price
                high    = float(data["h"])                   # high 24h
                low     = float(data["l"])                   # low 24h

                self.last_prices[sym] = {
                    "price": price, "high": high, "low": low,
                    "ts":    time.time(),
                }

                if self._on_price:
                    await self._on_price(sym, price, high, low)

            except Exception as e:
                log.warning(f"[WS] Error procesando mensaje: {e}")
