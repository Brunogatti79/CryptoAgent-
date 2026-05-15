#!/usr/bin/env python3
"""
gate_analysis.py
────────────────
Análisis de contribución por gate y expectancy por signal_type/régimen.

Uso:
    python gate_analysis.py                    # análisis completo
    python gate_analysis.py --gates            # solo gates
    python gate_analysis.py --expectancy       # solo expectancy
    python gate_analysis.py --mfe              # MFE/MAE analysis

Requiere: trades.db en DATA_DIR (o directorio actual)
"""

import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict

DB_PATH = os.path.join(os.getenv('DATA_DIR', '.'), 'trades.db')


def _connect():
    if not os.path.exists(DB_PATH):
        print(f"❌ Base de datos no encontrada: {DB_PATH}")
        sys.exit(1)
    return sqlite3.connect(DB_PATH)


# ═══════════════════════════════════════════════════════════════
# 1. Contribución por gate
# ═══════════════════════════════════════════════════════════════

def analyze_gates():
    """
    Muestra qué gates bloquean más señales.
    Si un gate bloquea >70% de las señales sin mejorar expectancy,
    probablemente está destruyendo valor.
    """
    conn = _connect()

    # Total de checks de entrada
    total = conn.execute(
        "SELECT COUNT(*) FROM events WHERE type = 'ENTRY_CHECK'"
    ).fetchone()[0]

    # Extraer blockers de cada evento
    rows = conn.execute(
        "SELECT details FROM events WHERE type = 'ENTRY_CHECK' AND details IS NOT NULL"
    ).fetchall()

    blocker_counts = Counter()
    for (details_json,) in rows:
        try:
            details = json.loads(details_json)
            blockers = details.get('blockers', [])
            for b in blockers:
                # Normalizar: extraer el tipo de blocker
                if 'régimen' in b.lower() or 'regimen' in b.lower():
                    blocker_counts['Gate 1: Régimen (SIDEWAYS/REVERSAL)'] += 1
                elif 'ema' in b.lower() and 'alinea' in b.lower():
                    blocker_counts['Gate 3: EMA no alineada'] += 1
                elif 'rsi' in b.lower():
                    blocker_counts['Gate 4: RSI fuera de rango'] += 1
                elif 'volumen' in b.lower():
                    blocker_counts['Gate 5: Volumen bajo'] += 1
                elif '1h' in b.lower() or 'intradiaria' in b.lower():
                    blocker_counts['Gate 6: Confirmación 1h'] += 1
                elif 'f&g' in b.lower() or 'greed' in b.lower() or 'fear' in b.lower():
                    blocker_counts['Gate 7: Fear & Greed'] += 1
                else:
                    blocker_counts[f'Otro: {b[:50]}'] += 1
        except (json.JSONDecodeError, TypeError):
            continue

    # Señales que pasaron todos los gates
    approved = conn.execute(
        "SELECT COUNT(*) FROM events WHERE type = 'CLAUDE_SIGNAL'"
    ).fetchone()[0]

    # Vetados por Claude
    vetoed = conn.execute(
        "SELECT COUNT(*) FROM events WHERE type = 'CLAUDE_VETO'"
    ).fetchone()[0]

    conn.close()

    print("\n" + "=" * 60)
    print("  CONTRIBUCIÓN POR GATE")
    print("=" * 60)
    print(f"\n  Total checks de entrada:  {total}")
    print(f"  Señales aprobadas:        {approved}")
    print(f"  Vetadas por Claude:       {vetoed}")
    print(f"  Bloqueadas por gates:     {total - approved - vetoed}")
    print()

    if blocker_counts:
        print(f"  {'Gate':<45} {'Bloqueos':>8} {'%':>6}")
        print("  " + "-" * 60)
        for gate, count in blocker_counts.most_common():
            pct = count / total * 100 if total else 0
            bar = "█" * int(pct / 2)
            print(f"  {gate:<45} {count:>8} {pct:>5.1f}% {bar}")
    else:
        print("  Sin datos de bloqueo todavía.")

    print()


# ═══════════════════════════════════════════════════════════════
# 2. Expectancy por signal_type y régimen
# ═══════════════════════════════════════════════════════════════

def analyze_expectancy():
    """
    Calcula expectancy = (winrate × avg_win) - (lossrate × avg_loss)
    separado por signal_type y régimen.
    """
    conn = _connect()

    # Verificar que las columnas existen
    cols = [r[1] for r in conn.execute("PRAGMA table_info(trades)").fetchall()]
    has_signal_type = 'signal_type' in cols
    has_regime      = 'regime_at_entry' in cols

    if not has_signal_type and not has_regime:
        print("\n  ⚠️  Columnas signal_type/regime_at_entry no existen todavía.")
        print("     Desplegá la versión con metadata v2 y esperá nuevos trades.\n")
        conn.close()
        return

    # Trades cerrados con metadata
    query = """
        SELECT symbol, direction, status, pnl_usd, entry_price, exit_price,
               signal_type, regime_at_entry, atr_at_entry
        FROM trades
        WHERE status IN ('WIN', 'LOSS')
    """
    rows = conn.execute(query).fetchall()
    conn.close()

    if not rows:
        print("\n  Sin trades cerrados todavía.\n")
        return

    # Agrupar por diferentes dimensiones
    groups = defaultdict(list)
    for r in rows:
        symbol, direction, status, pnl, entry, exit_p, sig_type, regime, atr = r
        trade = {
            'pnl': pnl or 0,
            'win': status == 'WIN',
            'entry': entry,
            'exit': exit_p,
            'atr': atr,
        }
        groups['GLOBAL'].append(trade)
        if sig_type:
            groups[f'signal:{sig_type}'].append(trade)
        if regime:
            groups[f'regime:{regime}'].append(trade)
        groups[f'pair:{symbol}'].append(trade)

    print("\n" + "=" * 60)
    print("  EXPECTANCY POR DIMENSIÓN")
    print("=" * 60)
    print(f"\n  {'Dimensión':<30} {'Trades':>6} {'WR%':>6} {'AvgW':>8} {'AvgL':>8} {'Expect':>8}")
    print("  " + "-" * 68)

    for key in sorted(groups.keys()):
        trades = groups[key]
        n      = len(trades)
        wins   = [t for t in trades if t['win']]
        losses = [t for t in trades if not t['win']]
        wr     = len(wins) / n * 100 if n else 0
        avg_w  = sum(t['pnl'] for t in wins) / len(wins) if wins else 0
        avg_l  = sum(abs(t['pnl']) for t in losses) / len(losses) if losses else 0
        expect = (wr / 100 * avg_w) - ((1 - wr / 100) * avg_l)
        emoji  = "✅" if expect > 0 else "❌"

        print(
            f"  {key:<30} {n:>6} {wr:>5.1f}% "
            f"${avg_w:>7.2f} ${avg_l:>7.2f} "
            f"${expect:>7.2f} {emoji}"
        )

    print()


# ═══════════════════════════════════════════════════════════════
# 3. MFE/MAE Analysis
# ═══════════════════════════════════════════════════════════════

def analyze_mfe_mae():
    """
    Analiza MFE/MAE para entender si los stops/targets son óptimos.

    Métricas clave:
    - Capture ratio = PnL real / MFE → qué % de la ganancia disponible capturaste
    - Pain ratio    = MAE / SL distance → qué tan cerca del stop llegó el precio
    - Edge ratio    = avg MFE / avg MAE → > 1 significa que el sistema tiene edge
    """
    conn = _connect()

    cols = [r[1] for r in conn.execute("PRAGMA table_info(trades)").fetchall()]
    if 'mfe_price' not in cols:
        print("\n  ⚠️  Columnas MFE/MAE no existen todavía.")
        print("     Desplegá la versión con MFE/MAE tracking y esperá nuevos trades.\n")
        conn.close()
        return

    rows = conn.execute("""
        SELECT symbol, direction, status, entry_price, exit_price, pnl_usd,
               stop_loss, take_profit, mfe_price, mae_price, atr_at_entry,
               signal_type, regime_at_entry
        FROM trades
        WHERE status IN ('WIN', 'LOSS')
          AND mfe_price IS NOT NULL
          AND mae_price IS NOT NULL
    """).fetchall()
    conn.close()

    if not rows:
        print("\n  Sin trades con datos MFE/MAE todavía.\n")
        return

    print("\n" + "=" * 60)
    print("  ANÁLISIS MFE/MAE")
    print("=" * 60)

    captures = []
    edges    = []

    for r in rows:
        symbol, direction, status, entry, exit_p, pnl, sl, tp, mfe, mae, atr, sig_type, regime = r

        if direction == 'LONG':
            mfe_r = (mfe - entry) / entry * 100 if entry else 0   # % ganancia máxima disponible
            mae_r = (entry - mae) / entry * 100 if entry else 0   # % drawdown máximo
            pnl_r = (exit_p - entry) / entry * 100 if entry else 0
        else:
            mfe_r = (entry - mfe) / entry * 100 if entry else 0
            mae_r = (mae - entry) / entry * 100 if entry else 0
            pnl_r = (entry - exit_p) / entry * 100 if entry else 0

        capture = (pnl_r / mfe_r * 100) if mfe_r > 0 else 0
        captures.append(capture)

        if mae_r > 0:
            edges.append(mfe_r / mae_r)

        emoji = "✅" if status == 'WIN' else "🔴"
        print(
            f"  {emoji} #{r[0] if len(r) > 13 else '?'} {symbol:<12} {direction:<5} "
            f"MFE={mfe_r:>+6.2f}% MAE={mae_r:>6.2f}% "
            f"PnL={pnl_r:>+6.2f}% Capture={capture:>5.1f}%"
        )

    print()
    if captures:
        avg_capture = sum(captures) / len(captures)
        print(f"  Capture ratio promedio: {avg_capture:.1f}%")
        print(f"    → 100% = capturaste todo el MFE")
        print(f"    → <50% = estás dejando mucha ganancia en la mesa")
        print(f"    → >100% = error en datos (imposible)")

    if edges:
        avg_edge = sum(edges) / len(edges)
        print(f"\n  Edge ratio promedio (MFE/MAE): {avg_edge:.2f}")
        print(f"    → >1.5 = edge fuerte")
        print(f"    → 1.0-1.5 = edge débil")
        print(f"    → <1.0 = sin edge (el mercado va más en contra que a favor)")

    print()


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════

if __name__ == '__main__':
    args = sys.argv[1:]

    if not args or '--all' in args:
        analyze_gates()
        analyze_expectancy()
        analyze_mfe_mae()
    else:
        if '--gates' in args:
            analyze_gates()
        if '--expectancy' in args:
            analyze_expectancy()
        if '--mfe' in args:
            analyze_mfe_mae()
