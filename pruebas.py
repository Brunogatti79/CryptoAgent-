import json
from datetime import datetime
data = {
  'last_update': datetime.now().isoformat(),
  'cycle': 0,
  'interval_minutes': 240,
  'halted': False,
  'fear_greed': {'value': 50, 'label': 'Neutral'},
  'balance_usdt': 0,
  'pair_stats': {},
  'open_trades': [],
  'trade_summary': {'total_closed':0,'wins':0,'losses':0,'open_count':0,'win_rate':0,'total_pnl':0},
  'regimes': {}
}
with open('dashboard_state.json','w') as f:
    json.dump(data, f, indent=2)
print('dashboard_state.json creado')