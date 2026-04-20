import ccxt

exchange = ccxt.binance({
    'apiKey': 'c9hai68pdRlICbHB2o6obgazvvSua13rhuePLYY5p08H87bdfde39sOwrPJOiLPl',
    'secret': 'jKeGb2ZyVbTm3KSXUEmCIdjxEFMtny2HItZ4CEKW9U4Xicy8QXG9gTVD53ui1qVK',
    'options': {
        'defaultType': 'spot',
        'adjustForTimeDifference': True,
    },
    'urls': {
        'api': {
            'public':  'https://testnet.binance.vision/api',
            'private': 'https://testnet.binance.vision/api',
            'v3':      'https://testnet.binance.vision/api/v3',
        },
    },
})

exchange.set_sandbox_mode(True)

try:
    balance = exchange.fetch_balance()
    totals = {k: v for k, v in balance['total'].items() if v > 0}
    print("Balance:", totals)
except Exception as e:
    print("Error:", e)