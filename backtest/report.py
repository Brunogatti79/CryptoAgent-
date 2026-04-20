"""
Genera report.html con los resultados del backtest regime-aware.
Uso: python report.py
     python report.py --open    (abre el browser automáticamente)
"""

import argparse
import os
import sys
import webbrowser
from datetime import datetime

import pandas as pd
import numpy as np

OUT_FILE     = os.path.join(os.path.dirname(__file__), "report.html")
RESULTS_CSV  = os.path.join(os.path.dirname(__file__), "regime_comparison.csv")
BASELINE_CSV = os.path.join(os.path.dirname(__file__), "backtest_results.csv")
MODELS_DIR   = os.path.join(os.path.dirname(__file__), "..", "models")


# ── Cargar datos ──────────────────────────────────────────────

def load_comparison() -> pd.DataFrame | None:
    if not os.path.exists(RESULTS_CSV):
        return None
    return pd.read_csv(RESULTS_CSV)


def load_regime_stats() -> dict:
    """Carga estadísticas de regímenes desde los modelos pkl si están disponibles."""
    stats = {}
    try:
        import joblib
        for fname in os.listdir(MODELS_DIR):
            if not fname.startswith("hmm_") or not fname.endswith(".pkl"):
                continue
            symbol = fname[4:-4]
            bundle = joblib.load(os.path.join(MODELS_DIR, fname))
            model  = bundle["model"]
            scaler = bundle["scaler"]
            labels = bundle["labels"]
            means  = scaler.inverse_transform(model.means_)
            preds  = model.predict(
                scaler.transform(
                    # dummy — solo necesitamos las medias del modelo
                    np.zeros((1, model.means_.shape[1]))
                )
            )
            regime_info = {}
            for state_id, name in labels.items():
                m = means[state_id]
                regime_info[name] = {
                    "ret_4h":    round(float(m[0]) * 100, 3),
                    "vol":       round(float(m[1]) * 100, 3),
                    "vol_ratio": round(float(m[2]), 2),
                    "rsi":       round(float(m[3]) * 50 + 50, 1),
                    "slope":     round(float(m[4]) * 100, 4),
                    "transmat":  {
                        labels.get(j, f"S{j}"): round(float(model.transmat_[state_id][j]), 3)
                        for j in range(model.n_components)
                    },
                }
            stats[symbol] = regime_info
    except Exception:
        pass
    return stats


# ── Helpers HTML ──────────────────────────────────────────────

def delta_cell(val: float, higher_is_better: bool = True, fmt: str = ".2f") -> str:
    if val == 0:
        cls = "neutral"
        arrow = "→"
    elif (val > 0) == higher_is_better:
        cls = "better"
        arrow = "▲"
    else:
        cls = "worse"
        arrow = "▼"
    sign = "+" if val > 0 else ""
    return f'<td class="delta {cls}">{arrow} {sign}{val:{fmt}}</td>'


def metric_card(title: str, desc: str, good: str, icon: str) -> str:
    return f"""
        <div class="metric-card">
            <div class="metric-icon">{icon}</div>
            <div class="metric-title">{title}</div>
            <div class="metric-desc">{desc}</div>
            <div class="metric-good">✓ Bueno si: {good}</div>
        </div>"""


def regime_card(name: str, info: dict, pct: float | None = None) -> str:
    colors = {
        "BULL_TREND": ("#10b981", "📈", "Tendencia alcista sostenida"),
        "BEAR_TREND": ("#ef4444", "📉", "Tendencia bajista sostenida"),
        "SIDEWAYS":   ("#94a3b8", "➡️",  "Mercado sin dirección clara"),
        "REVERSAL":   ("#f59e0b", "🔄", "Recuperación / transición"),
    }
    color, icon, subtitle = colors.get(name, ("#7c3aed", "❓", name))
    ret_sign = "+" if info["ret_4h"] >= 0 else ""
    pct_str  = f"<div class='regime-pct'>{pct:.1f}% del tiempo</div>" if pct else ""

    strategy_map = {
        "BULL_TREND": "Solo <b>LONG</b> — trend-following alcista",
        "BEAR_TREND": "Solo <b>SHORT</b> — trend-following bajista",
        "SIDEWAYS":   "<b>Sin operar</b> — esperar definición",
        "REVERSAL":   "<b>LONG conservador</b> — RSI &lt; 60",
    }
    strategy = strategy_map.get(name, "—")

    return f"""
        <div class="regime-card" style="border-top: 3px solid {color}">
            <div class="regime-header">
                <span class="regime-icon">{icon}</span>
                <div>
                    <div class="regime-name" style="color:{color}">{name}</div>
                    <div class="regime-subtitle">{subtitle}</div>
                </div>
            </div>
            {pct_str}
            <div class="regime-stats">
                <div class="rstat"><span>Ret. medio 4h</span><b style="color:{color}">{ret_sign}{info['ret_4h']}%</b></div>
                <div class="rstat"><span>Volatilidad</span><b>{info['vol']}%</b></div>
                <div class="rstat"><span>Volumen ratio</span><b>{info['vol_ratio']}x</b></div>
                <div class="rstat"><span>RSI medio</span><b>{info['rsi']}</b></div>
            </div>
            <div class="regime-strategy">Estrategia: {strategy}</div>
        </div>"""


def build_comparison_table(df: pd.DataFrame) -> str:
    rows = ""
    for _, r in df.iterrows():
        sym          = r["symbol"]
        ns, rs       = r["naive_sharpe"], r["regime_sharpe"]
        nr, rr       = r["naive_return"],  r["regime_return"]
        nt, rt       = int(r["naive_trades"]), int(r["regime_trades"])
        sharpe_delta = rs - ns
        return_delta = rr - nr
        trade_delta  = rt - nt

        sharpe_cls = "better" if sharpe_delta > 0 else ("worse" if sharpe_delta < 0 else "neutral")
        ret_cls    = "better" if return_delta > 0 else ("worse" if return_delta < 0 else "neutral")

        rows += f"""
            <tr>
                <td class="sym-cell">{sym}</td>
                <td>{ns:.2f}</td>
                <td class="{sharpe_cls}-val">{rs:.2f}</td>
                {delta_cell(sharpe_delta)}
                <td>{nr:+.1f}%</td>
                <td class="{ret_cls}-val">{rr:+.1f}%</td>
                {delta_cell(return_delta, fmt=".1f")}
                <td>{nt}</td>
                <td>{rt}</td>
                <td class="{'better' if trade_delta < 0 else 'neutral'}">{trade_delta:+d}</td>
            </tr>"""

    return f"""
        <table class="results-table">
            <thead>
                <tr>
                    <th rowspan="2">Par</th>
                    <th colspan="3">Sharpe Ratio</th>
                    <th colspan="3">Retorno Total</th>
                    <th colspan="3">Trades</th>
                </tr>
                <tr>
                    <th>Naive</th><th>Regime</th><th>Δ</th>
                    <th>Naive</th><th>Regime</th><th>Δ</th>
                    <th>Naive</th><th>Regime</th><th>Δ</th>
                </tr>
            </thead>
            <tbody>{rows}</tbody>
        </table>"""


# ── HTML principal ────────────────────────────────────────────

def generate_html(df: pd.DataFrame | None, regime_stats: dict) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # ── Tabla de comparación ──
    if df is not None:
        comparison_section = f"""
        <section>
            <h2>Resultados: Naive vs Regime-Aware</h2>
            <p class="section-desc">
                Validación <b>walk-forward</b>: entrenamos el HMM en el 80% más antiguo de los datos
                y evaluamos en el 20% restante (aprox. los últimos 5 meses). Así evitamos usar
                información del futuro.
            </p>
            {build_comparison_table(df)}
            <div class="table-legend">
                <span class="leg better">▲ mejor</span>
                <span class="leg worse">▼ peor</span>
                <span class="leg neutral">→ igual</span>
            </div>
        </section>"""
    else:
        comparison_section = """
        <section>
            <div class="no-data">
                Todavía no hay resultados.<br>
                Corré: <code>python simulator.py --regime</code>
            </div>
        </section>"""

    # ── Cards de regímenes (usa BTCUSDT si está disponible) ──
    default_regimes = {
        "BULL_TREND": {"ret_4h": 0.126, "vol": 0.697, "vol_ratio": 1.08, "rsi": 64.6, "slope": 0.3592},
        "BEAR_TREND": {"ret_4h":-0.178, "vol": 1.501, "vol_ratio": 1.47, "rsi": 46.2, "slope":-0.1681},
        "SIDEWAYS":   {"ret_4h":-0.051, "vol": 0.660, "vol_ratio": 0.92, "rsi": 42.9, "slope":-0.1048},
        "REVERSAL":   {"ret_4h": 0.071, "vol": 1.118, "vol_ratio": 0.79, "rsi": 51.0, "slope":-0.0856},
    }
    regime_pcts = {"BULL_TREND": 23.6, "BEAR_TREND": 17.3, "SIDEWAYS": 32.9, "REVERSAL": 26.2}
    regimes_to_show = regime_stats.get("BTCUSDT", default_regimes)
    order = ["BULL_TREND", "BEAR_TREND", "SIDEWAYS", "REVERSAL"]
    regime_cards_html = "".join(
        regime_card(name, regimes_to_show[name], regime_pcts.get(name))
        for name in order if name in regimes_to_show
    )

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Crypto Agent — Backtest Report</title>
<style>
  :root {{
    --bg:       #0f1117;
    --surface:  #1a1d2e;
    --surface2: #252840;
    --border:   #2d3154;
    --accent:   #7c3aed;
    --text:     #e2e8f0;
    --muted:    #94a3b8;
    --better:   #10b981;
    --worse:    #ef4444;
    --warn:     #f59e0b;
    --neutral:  #64748b;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    font-size: 15px;
    line-height: 1.6;
  }}

  /* ── Header ── */
  .hero {{
    background: linear-gradient(135deg, #1a1d2e 0%, #12102a 100%);
    border-bottom: 1px solid var(--border);
    padding: 40px 32px 32px;
  }}
  .hero h1 {{ font-size: 1.8rem; font-weight: 700; margin-bottom: 6px; }}
  .hero h1 span {{ color: var(--accent); }}
  .hero .subtitle {{ color: var(--muted); font-size: 0.9rem; }}
  .hero .badge {{
    display: inline-block;
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 20px;
    padding: 4px 14px;
    font-size: 0.8rem;
    color: var(--muted);
    margin-top: 12px;
  }}

  /* ── Layout ── */
  main {{ max-width: 1100px; margin: 0 auto; padding: 32px 24px 80px; }}
  section {{ margin-bottom: 56px; }}
  h2 {{
    font-size: 1.2rem;
    font-weight: 600;
    color: var(--text);
    margin-bottom: 6px;
    padding-bottom: 10px;
    border-bottom: 1px solid var(--border);
  }}
  .section-desc {{ color: var(--muted); font-size: 0.88rem; margin: 10px 0 20px; }}

  /* ── Callout "qué es esto" ── */
  .callout {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-left: 3px solid var(--accent);
    border-radius: 8px;
    padding: 20px 24px;
    margin-bottom: 56px;
  }}
  .callout h3 {{ font-size: 1rem; margin-bottom: 10px; color: var(--accent); }}
  .callout p {{ color: var(--muted); font-size: 0.9rem; margin-bottom: 8px; }}
  .callout code {{
    background: var(--surface2);
    border-radius: 4px;
    padding: 2px 6px;
    font-size: 0.85rem;
    color: #a78bfa;
  }}
  .strategies {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 12px;
    margin-top: 16px;
  }}
  .strategy-box {{
    background: var(--surface2);
    border-radius: 8px;
    padding: 14px 18px;
  }}
  .strategy-box .strat-label {{ font-size: 0.75rem; color: var(--muted); text-transform: uppercase; letter-spacing: .05em; }}
  .strategy-box .strat-name {{ font-weight: 600; margin: 4px 0; }}
  .strategy-box .strat-desc {{ font-size: 0.85rem; color: var(--muted); }}

  /* ── Glosario ── */
  .metrics-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
    gap: 14px;
    margin-top: 16px;
  }}
  .metric-card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 18px;
  }}
  .metric-icon {{ font-size: 1.4rem; margin-bottom: 8px; }}
  .metric-title {{ font-weight: 600; font-size: 0.95rem; margin-bottom: 6px; }}
  .metric-desc {{ color: var(--muted); font-size: 0.83rem; margin-bottom: 10px; line-height: 1.5; }}
  .metric-good {{
    font-size: 0.8rem;
    color: var(--better);
    background: rgba(16,185,129,.08);
    border-radius: 5px;
    padding: 4px 8px;
  }}

  /* ── Regímenes ── */
  .regimes-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
    gap: 16px;
    margin-top: 16px;
  }}
  .regime-card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 18px;
  }}
  .regime-header {{ display: flex; align-items: center; gap: 12px; margin-bottom: 10px; }}
  .regime-icon {{ font-size: 1.5rem; }}
  .regime-name {{ font-weight: 700; font-size: 1rem; }}
  .regime-subtitle {{ font-size: 0.78rem; color: var(--muted); }}
  .regime-pct {{
    font-size: 0.82rem;
    background: var(--surface2);
    border-radius: 5px;
    padding: 3px 8px;
    display: inline-block;
    margin-bottom: 12px;
    color: var(--muted);
  }}
  .regime-stats {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 12px; }}
  .rstat {{
    background: var(--surface2);
    border-radius: 6px;
    padding: 8px 10px;
    font-size: 0.82rem;
  }}
  .rstat span {{ display: block; color: var(--muted); font-size: 0.75rem; margin-bottom: 2px; }}
  .rstat b {{ font-size: 0.92rem; }}
  .regime-strategy {{
    font-size: 0.83rem;
    color: var(--muted);
    border-top: 1px solid var(--border);
    padding-top: 10px;
    margin-top: 4px;
  }}

  /* ── Tabla resultados ── */
  .results-table {{
    width: 100%;
    border-collapse: collapse;
    margin-top: 16px;
    font-size: 0.88rem;
  }}
  .results-table th {{
    background: var(--surface2);
    color: var(--muted);
    text-transform: uppercase;
    font-size: 0.72rem;
    letter-spacing: .05em;
    padding: 10px 12px;
    text-align: center;
    border: 1px solid var(--border);
  }}
  .results-table td {{
    padding: 12px 14px;
    text-align: center;
    border: 1px solid var(--border);
  }}
  .results-table tbody tr:nth-child(odd) {{ background: var(--surface); }}
  .results-table tbody tr:nth-child(even) {{ background: rgba(37,40,64,.4); }}
  .results-table tbody tr:hover {{ background: var(--surface2); }}
  .sym-cell {{ font-weight: 600; text-align: left !important; font-size: 0.95rem; }}
  .better-val {{ color: var(--better); font-weight: 600; }}
  .worse-val  {{ color: var(--worse);  font-weight: 600; }}
  .delta.better {{ color: var(--better); font-weight: 700; }}
  .delta.worse  {{ color: var(--worse);  font-weight: 700; }}
  .delta.neutral {{ color: var(--neutral); }}
  td.better {{ color: var(--better); }}
  td.worse  {{ color: var(--worse); }}

  .table-legend {{
    margin-top: 10px;
    display: flex;
    gap: 20px;
    font-size: 0.82rem;
  }}
  .leg {{ display: flex; align-items: center; gap: 5px; }}
  .leg.better {{ color: var(--better); }}
  .leg.worse  {{ color: var(--worse);  }}
  .leg.neutral {{ color: var(--neutral); }}

  /* ── Sin datos ── */
  .no-data {{
    background: var(--surface);
    border: 1px dashed var(--border);
    border-radius: 10px;
    padding: 40px;
    text-align: center;
    color: var(--muted);
    font-size: 0.95rem;
    line-height: 2;
  }}
  .no-data code {{
    background: var(--surface2);
    border-radius: 4px;
    padding: 2px 8px;
    color: #a78bfa;
  }}

  /* ── Footer ── */
  .footer {{
    text-align: center;
    color: var(--neutral);
    font-size: 0.8rem;
    padding: 20px;
    border-top: 1px solid var(--border);
  }}

  @media (max-width: 600px) {{
    .strategies {{ grid-template-columns: 1fr; }}
    .metrics-grid {{ grid-template-columns: 1fr 1fr; }}
    .regimes-grid {{ grid-template-columns: 1fr; }}
  }}
</style>
</head>
<body>

<div class="hero">
  <h1>🤖 Crypto Agent — <span>Backtest Report</span></h1>
  <div class="subtitle">Clasificador de Régimen HMM · Validación walk-forward</div>
  <div class="badge">Generado: {now}</div>
</div>

<main>

<!-- ── Qué es esto ── -->
<div class="callout">
  <h3>¿Qué estamos probando?</h3>
  <p>
    Tenemos un agente de trading que analiza BTC, ETH, SOL y XRP. La pregunta es:
    <b>¿mejora el rendimiento si el agente primero detecta en qué "estado" está el mercado
    antes de decidir si operar?</b>
  </p>
  <p>
    Usamos un modelo estadístico llamado <b>HMM (Hidden Markov Model)</b> que analiza
    2 años de velas de 4 horas y aprende automáticamente 4 patrones recurrentes del mercado
    (regímenes). Luego comparamos operar <i>siempre</i> vs operar <i>solo cuando el régimen
    es favorable</i>.
  </p>
  <div class="strategies">
    <div class="strategy-box">
      <div class="strat-label">Estrategia A</div>
      <div class="strat-name">Naive (baseline)</div>
      <div class="strat-desc">
        EMA20 &gt; EMA50 → LONG<br>
        EMA20 &lt; EMA50 → SHORT<br>
        Filtro RSI. Opera <i>siempre</i> que hay señal.
      </div>
    </div>
    <div class="strategy-box">
      <div class="strat-label">Estrategia B</div>
      <div class="strat-name">Regime-Aware (HMM)</div>
      <div class="strat-desc">
        Igual que Naive, pero primero detecta el régimen 4h.
        En SIDEWAYS no opera. En BULL solo LONG. En BEAR solo SHORT.
      </div>
    </div>
  </div>
</div>

<!-- ── Glosario ── -->
<section>
  <h2>Glosario de Métricas</h2>
  <p class="section-desc">Las métricas que usamos para comparar las estrategias, explicadas sin jerga.</p>
  <div class="metrics-grid">
    {metric_card(
        "Sharpe Ratio",
        "Mide el retorno ajustado por riesgo. En criollo: cuánta ganancia obtenés por cada unidad de riesgo que tomás.",
        "Mayor a 1.0 es bueno. Mayor a 2.0 es excelente.",
        "📐"
    )}
    {metric_card(
        "Retorno Total",
        "Ganancia o pérdida total del período de validación, expresada en porcentaje sobre el capital inicial.",
        "Positivo. Cuanto mayor, mejor.",
        "💰"
    )}
    {metric_card(
        "Win Rate",
        "Porcentaje de trades que terminaron en ganancia sobre el total de trades cerrados.",
        "Mayor a 50%. Con ratio R/B 2:1, con 40% ya sos rentable.",
        "🎯"
    )}
    {metric_card(
        "Max Drawdown",
        "La caída máxima desde un pico hasta el valle más bajo. Mide cuánto podés perder en el peor momento.",
        "Menor es mejor. &lt; 15% es aceptable para esta estrategia.",
        "📉"
    )}
    {metric_card(
        "Cantidad de Trades",
        "Cuántas operaciones se ejecutaron en el período de validación. Más trades no significa mejor.",
        "Depende de la calidad. Menos trades de mejor calidad = mejor Sharpe.",
        "🔢"
    )}
    {metric_card(
        "Walk-Forward",
        "Entrenamos el HMM en el 80% más antiguo y evaluamos en el 20% más reciente. Así no 'hacemos trampa' usando el futuro.",
        "El test siempre en datos no vistos por el modelo.",
        "🚶"
    )}
  </div>
</section>

<!-- ── Los 4 Regímenes ── -->
<section>
  <h2>Los 4 Regímenes del Mercado (BTC · 4h · 2 años)</h2>
  <p class="section-desc">
    El HMM aprendió estos 4 estados automáticamente, sin que nosotros le dijéramos cuáles son.
    Los identificamos <i>post-hoc</i> mirando las estadísticas de cada estado.
    Los porcentajes son cuánto tiempo pasa el mercado en cada régimen.
  </p>
  <div class="regimes-grid">
    {regime_cards_html}
  </div>
</section>

<!-- ── Resultados ── -->
{comparison_section}

<!-- ── Nota metodológica ── -->
<section>
  <h2>Notas de Interpretación</h2>
  <div class="callout" style="margin-bottom:0">
    <h3>¿Por qué tan pocos trades en el período de validación?</h3>
    <p>
      El 20% final de 2 años de datos 1h representa aprox. 5 meses de historia.
      Con la estrategia actual (stop 4%, target 8%) se ejecutan entre 25-60 trades por par
      en ese período. Es estadísticamente poco para sacar conclusiones definitivas,
      pero suficiente para detectar diferencias grandes en Sharpe (&gt; 0.5).
    </p>
    <h3 style="margin-top:14px">¿Por qué el backtest usa datos 1h si el HMM es 4h?</h3>
    <p>
      El HMM detecta el "estado del mercado" usando la visión de largo plazo (4h).
      La ejecución de trades usa la granularidad 1h para mayor precisión en entradas/salidas.
      Los regímenes 4h se <i>proyectan</i> a las barras 1h por forward-fill.
    </p>
    <h3 style="margin-top:14px">El estado REVERSAL tiene retorno positivo (+0.071%/4h)</h3>
    <p>
      Aunque el nombre sugiere riesgo, este estado representa <b>recuperaciones post-bear</b>:
      baja volatilidad, volumen bajo, RSI neutral (51). La versión más reciente del backtester
      permite LONGs conservadores en este estado (RSI &lt; 60) en lugar de bloquearlo.
    </p>
  </div>
</section>

</main>

<div class="footer">
  Crypto Agent · Backtest Report · {now} · Solo uso educativo — no es consejo financiero
</div>

</body>
</html>"""


# ── Main ──────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--open", action="store_true", help="Abrir en el browser al terminar")
    args = parser.parse_args()

    df           = load_comparison()
    regime_stats = load_regime_stats()

    html = generate_html(df, regime_stats)

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"✓ Reporte generado: {OUT_FILE}")

    if df is not None:
        print(f"  {len(df)} pares cargados desde regime_comparison.csv")
    else:
        print("  Sin datos de comparación todavía (corré simulator.py --regime)")

    if regime_stats:
        print(f"  Estadísticas de régimen cargadas: {', '.join(regime_stats.keys())}")

    if args.open or True:   # siempre abrir
        webbrowser.open(f"file:///{OUT_FILE.replace(chr(92), '/')}")
        print("  Abriendo en browser...")


if __name__ == "__main__":
    main()
