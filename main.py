# =============================================================
#  CRYPTO AGENT — MAIN v3 (régimen HMM + monitor posiciones + dashboard)
# =============================================================

import asyncio
import json
import os
import threading
import time
import logging
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

import config
import data as market_data_module
import brain
import regime as regime_module
import telegram_alerts as tg
import executor as exc

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("agent.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

DASHBOARD_FILE = os.path.join(os.path.dirname(__file__), "dashboard_state.json")
WEBROOT        = os.path.dirname(os.path.abspath(__file__))

state = {
    "daily_loss_usd":    0.0,
    "halted":            False,
    "cycles_run":        0,
    "analysis_cycles":   0,
    "last_reset_date":   datetime.now().date(),
    "last_analysis":     {},          # {symbol: datetime}
    "last_regimes":      {},          # {symbol: regime} — para detectar cambios
    "last_group_b_scan": None,        # date — para scan diario
    "group_b_symbols":   [],          # pares activos del Grupo B
    "pair_stats":        {sym: {"queries": 0, "actionable": 0, "discarded": 0, "last_signal": None,
                                "last_conviction": 0, "last_regime": None, "last_price": 0,
                                "last_rsi": 0, "last_trend": None}
                          for sym in config.SYMBOLS},
}


def needs_analysis(symbol: str) -> bool:
    """True si pasaron ANALYSIS_INTERVAL_MINUTES desde el último análisis de este par."""
    last = state["last_analysis"].get(symbol)
    if last is None:
        return True
    return (datetime.now() - last).total_seconds() >= config.ANALYSIS_INTERVAL_MINUTES * 60


# ── Helpers ───────────────────────────────────────────────────

def reset_daily_state_if_needed():
    today = datetime.now().date()
    if today != state["last_reset_date"]:
        log.info("Nuevo día — reseteando pérdida diaria.")
        state["daily_loss_usd"]  = 0.0
        state["halted"]          = False
        state["last_reset_date"] = today


def _check_regime_exits(regimes: dict, mkt: dict) -> list[dict]:
    """
    Cierra posiciones al mercado si el régimen cambió a uno incompatible.
    Solo aplica a Grupo A (tienen modelo HMM).
    """
    closed = []
    for sym in config.SYMBOLS:
        trade = exc.get_open_position(sym)
        if not trade:
            continue

        regime_info = regimes.get(sym, {})
        if not regime_info.get("available"):
            continue

        regime    = regime_info.get("regime")
        price     = mkt.get(sym, {}).get("price", 0)
        direction = trade["direction"]

        should_exit = (
            (direction == "LONG"  and regime == "BEAR_TREND") or
            (direction == "SHORT" and regime == "BULL_TREND")
        )

        if should_exit:
            log.info(f"  [regime exit] {sym}: {direction} cerrado — régimen cambió a {regime}")
            ct = exc.market_close_trade(trade, price, f"cambio de régimen a {regime}")
            exc.log_event("REGIME_EXIT",
                          f"{sym} {direction} cerrado — régimen → {regime}",
                          symbol=sym, level="WARNING",
                          details={"direction": direction, "regime": regime,
                                   "result": ct["result"], "pnl_usd": ct["pnl_usd"]})
            closed.append(ct)

    return closed


def _scan_group_b() -> list[str]:
    """Escanea top movers de Binance una vez por día."""
    today = datetime.now().date()
    if state["last_group_b_scan"] == today:
        return state["group_b_symbols"]

    if not config.GROUP_B_ENABLED:
        return []

    log.info("[Grupo B] Escaneando top movers del día...")
    movers = market_data_module.get_top_movers(
        symbols_a=config.SYMBOLS,
        n=config.GROUP_B_TOP_MOVERS,
        min_change_pct=config.GROUP_B_MIN_CHANGE_PCT,
        min_volume_usd=config.GROUP_B_MIN_VOLUME_USD,
    )

    state["last_group_b_scan"] = today
    state["group_b_symbols"]   = [m["symbol"] for m in movers]

    if movers:
        for m in movers:
            log.info(f"  [Grupo B] {m['symbol']} | {m['change_24h']:+.1f}% | vol ${m['volume_usd']/1e6:.0f}M")
            exc.log_event("MOVER_DETECTED", f"{m['symbol']} {m['change_24h']:+.1f}% en 24h",
                          symbol=m["symbol"], group="B", level="WARNING",
                          details={"change_24h": m["change_24h"],
                                   "volume_usd": m["volume_usd"], "price": m["price"]})
    else:
        log.info("  [Grupo B] Sin movers que superen el umbral hoy.")
        exc.log_event("GROUP_B_SCAN", "Scan diario: sin movers calificados",
                      level="INFO", details={"min_change": config.GROUP_B_MIN_CHANGE_PCT,
                                             "min_volume": config.GROUP_B_MIN_VOLUME_USD})
    return state["group_b_symbols"]


def _log_regimes(regimes: dict) -> None:
    for sym, info in regimes.items():
        if info.get("available"):
            log.info(
                f"  [régimen] {sym}: {info['regime']} "
                f"({info['bars_in_regime']} barras · {info['hours_in_regime']}h)"
            )


def _update_pair_stats(signals: list[dict], mkt: dict, regimes: dict) -> None:
    """Actualiza contadores por par en el state en memoria."""
    sig_map = {s["symbol"]: s for s in signals}

    all_tracked = list(config.SYMBOLS) + list(config.SYMBOLS_C)
    for sym in all_tracked:
        ps = state["pair_stats"].setdefault(sym, {
            "queries": 0, "actionable": 0, "discarded": 0,
            "last_signal": None, "last_conviction": 0,
            "last_regime": None, "last_price": 0,
            "last_rsi": 0, "last_trend": None,
        })
        ps["queries"] += 1

        sig = sig_map.get(sym)
        if sig:
            if sig.get("actionable"):
                ps["actionable"] += 1
            else:
                ps["discarded"] += 1
            ps["last_signal"]     = sig.get("direction")
            ps["last_conviction"] = sig.get("conviction", 0)

        d = mkt.get(sym, {})
        if not d.get("error"):
            ps["last_price"] = d.get("price", 0)
            ps["last_rsi"]   = d.get("rsi", 0)
            ps["last_trend"] = d.get("trend")

        r = regimes.get(sym, {})
        if r.get("available"):
            ps["last_regime"]       = r.get("regime")
            ps["last_regime_hours"] = r.get("hours_in_regime", 0)


def _upload_to_gist(content: str) -> None:
    """Sube el dashboard_state.json a GitHub Gist si están configurados token y gist_id."""
    token    = config.GITHUB_GIST_TOKEN
    gist_id  = config.GITHUB_GIST_ID
    if not token or not gist_id:
        return
    try:
        import requests as req
        r = req.patch(
            f"https://api.github.com/gists/{gist_id}",
            headers={"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"},
            json={"files": {"dashboard_state.json": {"content": content}}},
            timeout=10,
        )
        if r.status_code != 200:
            log.warning(f"[gist] Upload falló: {r.status_code}")
    except Exception as e:
        log.warning(f"[gist] Error: {e}")


def write_dashboard_state(mkt: dict, fng: dict, regimes: dict,
                          signals: list[dict], balance: float) -> None:
    """Escribe dashboard_state.json — leído por el dashboard PWA."""
    trade_stats = exc.get_all_trades_stats()

    for t in trade_stats["open_trades"]:
        price = mkt.get(t["symbol"], {}).get("price", 0)
        if price and t["entry_price"]:
            if t["direction"] == "LONG":
                t["pnl_pct"] = round((price - t["entry_price"]) / t["entry_price"] * 100, 2)
            else:
                t["pnl_pct"] = round((t["entry_price"] - price) / t["entry_price"] * 100, 2)
            t["current_price"] = price
        else:
            t["pnl_pct"] = 0

    payload = {
        "last_update":        datetime.now().isoformat(),
        "cycle":              state["cycles_run"],
        "analysis_cycles":    state["analysis_cycles"],
        "monitor_interval_minutes":  config.MONITOR_INTERVAL_MINUTES,
        "analysis_interval_minutes": config.ANALYSIS_INTERVAL_MINUTES,
        "last_analysis_times": {s: t.isoformat() for s, t in state["last_analysis"].items()},
        "halted":           state["halted"],
        "fear_greed":       fng,
        "balance_usdt":     balance,
        "pair_stats":       state["pair_stats"],
        "open_trades":      trade_stats["open_trades"],
        "trade_summary": {
            "total_closed": trade_stats["total_closed"],
            "wins":         trade_stats["wins"],
            "losses":       trade_stats["losses"],
            "open_count":   trade_stats["open_count"],
            "win_rate":     trade_stats["win_rate"],
            "total_pnl":    trade_stats["total_pnl"],
        },
        "regimes": {
            sym: {
                "regime":         info.get("regime", "UNKNOWN"),
                "hours":          info.get("hours_in_regime", 0),
                "persist_prob":   info.get("persist_prob", 0),
                "prev_regime":    info.get("prev_regime"),
                "available":      info.get("available", False),
            }
            for sym, info in regimes.items()
        },
    }

    content = json.dumps(payload, indent=2, default=str)
    with open(DASHBOARD_FILE, "w", encoding="utf-8") as f:
        f.write(content)
    _upload_to_gist(content)


# ── Servidor HTTP / API ──────────────────────────────────────

STATIC_TYPES = {'.html': 'text/html', '.json': 'application/json',
                '.js': 'text/javascript', '.css': 'text/css',
                '.png': 'image/png', '.ico': 'image/x-icon', '.webmanifest': 'application/manifest+json'}


class APIHandler(BaseHTTPRequestHandler):

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = self.path.split('?')[0]
        if path in ('/', '/index.html'):
            path = '/dashboard.html'

        if path == '/api/events':
            self._handle_events()
            return

        fpath = os.path.join(WEBROOT, path.lstrip('/'))
        if os.path.isfile(fpath):
            ext = os.path.splitext(fpath)[1]
            ct  = STATIC_TYPES.get(ext, 'application/octet-stream')
            with open(fpath, 'rb') as f:
                body = f.read()
            self.send_response(200)
            self.send_header('Content-Type', ct)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self._cors()
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def _handle_events(self):
        params      = dict(p.split('=') for p in self.path.split('?')[1].split('&') if '=' in p) \
                      if '?' in self.path else {}
        limit       = min(int(params.get('limit',  '100')), 500)
        offset      = int(params.get('offset', '0'))
        type_filter = params.get('type')
        sym_filter  = params.get('symbol')
        events, total = exc.get_events(limit, offset, type_filter, sym_filter)
        self._json(200, {'total': total, 'limit': limit, 'offset': offset, 'events': events})

    def do_POST(self):
        if self.path == '/api/close':
            self._handle_close()
        else:
            self.send_response(404)
            self.end_headers()

    def _handle_close(self):
        length = int(self.headers.get('Content-Length', 0))
        try:
            body = json.loads(self.rfile.read(length)) if length else {}
        except Exception:
            self._json(400, {'error': 'JSON inválido'})
            return

        if config.AGENT_API_TOKEN and body.get('token') != config.AGENT_API_TOKEN:
            self._json(401, {'error': 'Token inválido'})
            return

        trade_id = body.get('trade_id')
        if not trade_id:
            self._json(400, {'error': 'trade_id requerido'})
            return

        trade = exc.get_trade_by_id(int(trade_id))
        if not trade:
            self._json(404, {'error': f'Trade #{trade_id} no encontrado o ya cerrado'})
            return

        try:
            mkt   = market_data_module.get_prices_and_indicators([trade['symbol']])
            price = mkt.get(trade['symbol'], {}).get('price', 0)
            result = exc.market_close_trade(trade, price, 'cierre manual desde dashboard')
            state['last_analysis'].pop(trade['symbol'], None)
            tg.send_trade_closed(result)
            log.info(f"[API] Trade #{trade_id} cerrado manualmente | {result['result']} | PnL ${result['pnl_usd']}")
            self._json(200, result)
        except Exception as e:
            log.error(f"[API] Error cerrando #{trade_id}: {e}")
            self._json(500, {'error': str(e)})

    def _json(self, code, data):
        body = json.dumps(data, default=str).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        if args and str(args[1]) not in ('200', '204', '304'):
            log.info(f'[HTTP] {fmt % args}')


def start_api_server():
    server = HTTPServer(('0.0.0.0', config.PORT), APIHandler)
    log.info(f"HTTP server en puerto {config.PORT}")
    server.serve_forever()


# ── Ciclo principal ───────────────────────────────────────────

def run_cycle():
    cycle_num = state["cycles_run"] + 1
    log.info(f"── Ciclo #{cycle_num} iniciando ──")

    # 1. Scan Grupo B — una vez por día
    group_b_symbols = _scan_group_b()

    # 2. Datos de mercado — Grupo A + Grupo B + Grupo C
    all_symbols = (
        config.SYMBOLS
        + [s for s in group_b_symbols if s not in config.SYMBOLS]
        + [s for s in config.SYMBOLS_C if s not in config.SYMBOLS and config.GROUP_C_ENABLED]
    )
    try:
        mkt = market_data_module.get_prices_and_indicators(all_symbols)
        fng = market_data_module.get_fear_and_greed()
        log.info("Datos de mercado OK")
    except Exception as e:
        log.error(f"Error datos: {e}")
        tg.send_error("fetch datos", str(e))
        return

    # 3. Monitor posiciones abiertas — cerrar las que tocaron stop/target
    closed_trades = []
    try:
        closed_trades = exc.check_open_positions(mkt)
        for ct in closed_trades:
            tg.send_trade_closed(ct)
            state["last_analysis"].pop(ct["symbol"], None)
            level = "SUCCESS" if ct["result"] == "WIN" else "ERROR"
            exc.log_event("TRADE_CLOSE",
                          f"{ct['symbol']} {ct['direction']} → {ct['result']} | PnL ${ct['pnl_usd']:+.2f}",
                          symbol=ct["symbol"], level=level,
                          details={**ct, "reason": ct.get("reason", "stop/target")})
            if ct["result"] == "LOSS":
                state["daily_loss_usd"] += abs(ct["pnl_usd"])
                if state["daily_loss_usd"] >= config.MAX_DAILY_LOSS_USD:
                    state["halted"] = True
                    tg.send_daily_limit_hit(state["daily_loss_usd"])
                    exc.log_event("DAILY_HALT", f"Límite diario alcanzado: ${state['daily_loss_usd']:.2f}",
                                  level="ERROR", details={"daily_loss": state["daily_loss_usd"]})
                    log.warning("Límite de pérdida diaria alcanzado — agente detenido.")
    except Exception as e:
        log.error(f"Error monitor posiciones: {e}")

    # 4. Régimen HMM
    try:
        regimes        = regime_module.classify_all(config.SYMBOLS)
        regime_context = regime_module.format_regime_context(regimes)
        _log_regimes(regimes)
        for sym, info in regimes.items():
            if not info.get("available"):
                continue
            new_regime  = info.get("regime")
            prev_regime = state["last_regimes"].get(sym)
            if prev_regime and prev_regime != new_regime:
                exc.log_event("REGIME_CHANGE",
                              f"{sym}: {prev_regime} → {new_regime}",
                              symbol=sym, level="INFO",
                              details={"from": prev_regime, "to": new_regime,
                                       "hours": info.get("hours_in_regime", 0),
                                       "persist_prob": info.get("persist_prob", 0)})
            state["last_regimes"][sym] = new_regime
    except Exception as e:
        log.warning(f"Régimen no disponible: {e}")
        regimes        = {}
        regime_context = ""

    # 5. Salida por cambio de régimen (sin Claude)
    try:
        regime_closed = _check_regime_exits(regimes, mkt)
        for ct in regime_closed:
            tg.send_trade_closed(ct)
            state["last_analysis"].pop(ct["symbol"], None)
            closed_trades.append(ct)
            if ct["result"] == "LOSS":
                state["daily_loss_usd"] += abs(ct["pnl_usd"])
                if state["daily_loss_usd"] >= config.MAX_DAILY_LOSS_USD:
                    state["halted"] = True
                    tg.send_daily_limit_hit(state["daily_loss_usd"])
    except Exception as e:
        log.error(f"Error regime exits: {e}")

    signals = []
    tokens  = 0

    # 6a. Análisis Grupo A — filtro mecánico + veto Claude (Haiku)
    free_a  = [s for s in config.SYMBOLS if not exc.has_open_position(s)]
    due_a   = [s for s in free_a if needs_analysis(s)]
    blocked = [s for s in config.SYMBOLS if exc.has_open_position(s)]
    if blocked:
        log.info(f"  Skip análisis (posición abierta): {', '.join(blocked)}")

    if due_a and not state["halted"]:
        for sym in due_a:
            state["last_analysis"][sym] = datetime.now()
            try:
                cond = market_data_module.check_entry_conditions(
                    sym, mkt, regimes.get(sym, {})
                )
                d = mkt.get(sym, {})
                snapshot = {
                    "price":          d.get("price"),
                    "rsi":            d.get("rsi"),
                    "ema20":          d.get("ema20"),
                    "ema50":          d.get("ema50"),
                    "ema20_1h":       d.get("ema20_1h"),
                    "price_above_1h": d.get("price_above_ema20_1h"),
                    "price_below_1h": d.get("price_below_ema20_1h"),
                    "vol_ratio":      d.get("vol_ratio"),
                    "change_4h":      d.get("change_4h"),
                    "change_24h":     d.get("change_24h"),
                    "trend":          d.get("trend"),
                    "ema_cross_up":   d.get("ema_cross_up"),
                    "ema_cross_down": d.get("ema_cross_down"),
                    "rsi_recovery":   d.get("rsi_recovery"),
                    "rsi_rejection":  d.get("rsi_rejection"),
                    "regime":         regimes.get(sym, {}).get("regime"),
                    "regime_hours":   regimes.get(sym, {}).get("hours_in_regime"),
                    "fng":            fng.get("value"),
                    "signal_type":    cond.get("signal_type"),
                }

                if not cond["qualified"]:
                    log.info(f"  [A] {sym} no califica: {' | '.join(cond['blockers'])}")
                    exc.log_event("ENTRY_CHECK", f"{sym} — no califica [{cond.get('signal_type','?')}]",
                                  symbol=sym, group="A", level="INFO",
                                  details={**snapshot,
                                           "qualified": False,
                                           "blockers":  cond["blockers"],
                                           "reasons":   cond["reasons"]})
                    continue

                log.info(f"  [A] {sym} califica ({cond['direction']}) [{cond.get('signal_type')}]: {' | '.join(cond['reasons'])}")

                reg_ctx = regime_module.format_regime_context(
                    {sym: regimes[sym]} if sym in regimes else {}
                )
                veto = brain.analyze_veto(sym, cond["direction"], cond, mkt, reg_ctx)
                tokens += veto["tokens"]
                state["analysis_cycles"] += 1

                if veto["veto"]:
                    log.info(f"  [A] {sym} VETADO: {veto['reason']}")
                    exc.log_event("CLAUDE_VETO", f"{sym} vetado — {veto['reason']}",
                                  symbol=sym, group="A", level="WARNING",
                                  details={**snapshot,
                                           "direction": cond["direction"],
                                           "veto_reason": veto["reason"],
                                           "conditions": cond["reasons"]})
                    continue

                sig = {
                    "symbol":      sym,
                    "direction":   cond["direction"],
                    "conviction":  9,
                    "actionable":  True,
                    "thesis":      f"[{cond.get('signal_type')}] {', '.join(cond['reasons'])}",
                    "group":       "A",
                    "group_name":  "A",
                    "take_profit": "",
                    "stop_loss":   "",
                }
                signals.append(sig)
                exc.log_event("CLAUDE_SIGNAL",
                              f"{sym} → {cond['direction']} [{cond.get('signal_type')}] aprobado",
                              symbol=sym, group="A", level="WARNING",
                              details={**snapshot,
                                       "qualified":   True,
                                       "conviction":  9,
                                       "conditions":  cond["reasons"],
                                       "veto_reason": veto["reason"]})

            except Exception as e:
                log.error(f"Error análisis A {sym}: {e}")
                tg.send_error(f"análisis A {sym}", str(e))

    # 6b. Análisis Claude Grupo B — movers sin posición abierta
    if group_b_symbols and not state["halted"]:
        open_b = sum(1 for s in group_b_symbols if exc.has_open_position(s))
        free_b = [s for s in group_b_symbols
                  if not exc.has_open_position(s) and needs_analysis(s)]

        if free_b and open_b < config.GROUP_B_MAX_POSITIONS:
            try:
                b_mkt = {s: mkt[s] for s in free_b if s in mkt}
                b_ctx = market_data_module.format_market_context(b_mkt, fng)
                result_b  = brain.analyze_group_b(b_ctx)
                signals_b = result_b["signals"]
                tokens   += result_b["input_tokens"] + result_b["output_tokens"]
                log.info(f"Claude B OK — {len(signals_b)} señales")
                for s in free_b:
                    state["last_analysis"][s] = datetime.now()
                for sig in signals_b:
                    exc.log_event("CLAUDE_SIGNAL",
                                  f"[B] {sig['symbol']} → {sig['direction']} (conv {sig.get('conviction',0)}/10)",
                                  symbol=sig["symbol"], group="B",
                                  level="WARNING" if sig.get("actionable") else "INFO",
                                  details={"direction": sig["direction"],
                                           "conviction": sig.get("conviction"),
                                           "actionable": sig.get("actionable"),
                                           "thesis": sig.get("thesis","")})
                signals += signals_b
            except Exception as e:
                log.error(f"Error brain B: {e}")

    # 6c. Análisis Grupo C — mecánico puro sin HMM ni Claude
    if config.GROUP_C_ENABLED and not state["halted"]:
        open_c = sum(1 for s in config.SYMBOLS_C if exc.has_open_position(s))
        free_c = [s for s in config.SYMBOLS_C
                  if not exc.has_open_position(s) and needs_analysis(s)]

        # Asegurar datos de mercado para símbolos C que no estén en mkt
        missing_c = [s for s in free_c if s not in mkt]
        if missing_c:
            try:
                extra = market_data_module.get_prices_and_indicators(missing_c)
                mkt.update(extra)
            except Exception as e:
                log.warning(f"[C] Error fetch datos extra: {e}")

        for sym in free_c:
            if open_c >= config.GROUP_C_MAX_POSITIONS:
                log.info(f"  [C] Máx posiciones alcanzado ({config.GROUP_C_MAX_POSITIONS})")
                break
            state["last_analysis"][sym] = datetime.now()
            try:
                cond = market_data_module.check_entry_conditions_c(sym, mkt)

                if not cond["qualified"]:
                    log.info(f"  [C] {sym} no califica: {' | '.join(cond['blockers'])}")
                    exc.log_event("ENTRY_CHECK", f"{sym} — no califica [{cond.get('signal_type','?')}]",
                                  symbol=sym, group="C", level="INFO",
                                  details={"qualified": False,
                                           "blockers":  cond["blockers"],
                                           "reasons":   cond["reasons"]})
                    continue

                log.info(f"  [C] {sym} califica ({cond['direction']}) [{cond['signal_type']}]: {' | '.join(cond['reasons'])}")

                sig = {
                    "symbol":      sym,
                    "direction":   cond["direction"],
                    "conviction":  8,
                    "actionable":  True,
                    "thesis":      f"[{cond['signal_type']}] {', '.join(cond['reasons'])}",
                    "group":       "C",
                    "group_name":  "C",
                    "take_profit": "",
                    "stop_loss":   "",
                }
                signals.append(sig)
                open_c += 1
                exc.log_event("ENTRY_CHECK",
                              f"[C] {sym} → {cond['direction']} [{cond['signal_type']}] califica",
                              symbol=sym, group="C", level="WARNING",
                              details={"qualified":   True,
                                       "direction":   cond["direction"],
                                       "reasons":     cond["reasons"],
                                       "signal_type": cond["signal_type"]})

            except Exception as e:
                log.error(f"Error análisis C {sym}: {e}")

    # 7. Ejecutar señales accionables
    fng_value = fng.get("value", 50)
    for signal in signals:
        if signal.get("actionable") and not state["halted"]:
            # Filtro Fear & Greed
            if signal["direction"] == "LONG" and fng_value > 80:
                log.info(f"  Skip {signal['symbol']} LONG — F&G {fng_value} (Extreme Greed)")
                exc.log_event("ENTRY_CHECK", f"{signal['symbol']} bloqueado — F&G {fng_value} Extreme Greed",
                              symbol=signal["symbol"], level="WARNING",
                              details={"fng": fng_value, "direction": "LONG"})
                continue
            if signal["direction"] == "SHORT" and fng_value < 20:
                log.info(f"  Skip {signal['symbol']} SHORT — F&G {fng_value} (Extreme Fear)")
                exc.log_event("ENTRY_CHECK", f"{signal['symbol']} bloqueado — F&G {fng_value} Extreme Fear",
                              symbol=signal["symbol"], level="WARNING",
                              details={"fng": fng_value, "direction": "SHORT"})
                continue

            group    = signal.get("group", "A")
            stop_pct = (config.STOP_LOSS_PCT_B if group == "B"
                        else config.STOP_LOSS_PCT_C if group == "C"
                        else config.STOP_LOSS_PCT)
            max_usd  = config.MAX_TRADE_USD_C if group == "C" else None

            log.info(f"Ejecutando: {signal['symbol']} {signal['direction']} (Grupo {group})")
            signal["group_name"] = group
            res = exc.execute_signal(signal, mkt, stop_pct=stop_pct, max_trade_usd=max_usd)
            if res:
                tg.send_execution_confirmation(res)
                exc.log_event("TRADE_OPEN",
                              f"{res['symbol']} {res['direction']} @ ${res['entry_price']:,.4f}",
                              symbol=res["symbol"], group=group, level="INFO",
                              details={**res, "group": group})
            else:
                log.warning(f"No ejecutado: {signal['symbol']}")

    actionable = [s for s in signals if s.get("actionable")]
    if actionable:
        for sig in actionable:
            tg.send_signal(sig, mkt)

    # 8. Actualizar contadores y estado
    _update_pair_stats(signals, mkt, regimes)

    # 9. Dashboard state
    balance = exc.get_balance_usdt()
    write_dashboard_state(mkt, fng, regimes, signals, balance)

    # 10. Resumen Telegram
    if state["analysis_cycles"] % 3 == 0 and signals or closed_trades:
        tg.send_cycle_summary(signals, fng, tokens, balance, regimes)

    state["cycles_run"] += 1
    log.info(f"── Ciclo #{cycle_num} completado ──\n")


def main():
    log.info("=== CRYPTO AGENT v3 ARRANCANDO ===")
    log.info(f"DB path: {exc.DB_PATH}")
    log.info(f"Capital inicial: ${config.INITIAL_CAPITAL_USD:,.2f}")
    log.info(f"Símbolos A: {config.SYMBOLS}")
    log.info(f"Símbolos C: {config.SYMBOLS_C} (enabled={config.GROUP_C_ENABLED})")

    # HTTP server arranca primero — Railway necesita que el puerto esté escuchando
    threading.Thread(target=start_api_server, daemon=True, name="api-server").start()
    log.info(f"HTTP server arrancado en puerto {config.PORT}")

    # Motor async (WebSocket + TrailingStop)
    def _run_async_engine():
        from main_async import run_trailing_engine
        asyncio.run(run_trailing_engine())

    threading.Thread(target=_run_async_engine, daemon=True, name="trailing-engine").start()
    log.info("[async] TrailingEngine thread arrancado")

    exc.init_db()
    exc.log_event("STARTUP", "Agente iniciado",
                  level="INFO", details={
                      "symbols_a":       config.SYMBOLS,
                      "symbols_c":       config.SYMBOLS_C,
                      "group_b_enabled": config.GROUP_B_ENABLED,
                      "group_c_enabled": config.GROUP_C_ENABLED,
                      "testnet":         config.BINANCE_TESTNET,
                      "monitor_min":     config.MONITOR_INTERVAL_MINUTES,
                      "analysis_min":    config.ANALYSIS_INTERVAL_MINUTES,
                      "capital_inicial": config.INITIAL_CAPITAL_USD,
                  })
    tg.send_startup()

    while True:
        reset_daily_state_if_needed()
        if state["halted"]:
            log.info("Agente detenido por límite diario. Esperando reseteo.")
            time.sleep(3600)
            continue
        try:
            run_cycle()
        except KeyboardInterrupt:
            log.info("Detenido manualmente.")
            tg.send("🛑 Agente detenido manualmente.")
            break
        except Exception as e:
            log.error(f"Error loop: {e}")
            tg.send_error("loop principal", str(e))

        log.info(f"Próximo ciclo en {config.MONITOR_INTERVAL_MINUTES} min...")
        time.sleep(config.MONITOR_INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    main()
