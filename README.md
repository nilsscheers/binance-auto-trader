# Binance Auto Trader

Automated crypto trading bot for Binance with AI pair selection, parameter optimisation, real-time web dashboard, and auto-updates from GitHub.

## Files

| File | Purpose |
|---|---|
| `auto_trader.py` | Main bot — never edit this directly |
| `config.py` | **Your settings** — edit this file |
| `updater.py` | Auto-update module |
| `VERSION` | Current version number |

## Quick Start

1. Install dependencies:
```
pip install python-binance pandas numpy requests
```

2. Edit `config.py` with your API keys and settings

3. Run:
```
python auto_trader.py
```

4. Open the dashboard at http://localhost:8888

## Auto-Updates

The bot checks GitHub for updates on every startup. To enable:

1. Fork this repository to your own GitHub account
2. In `config.py` set:
```python
GITHUB_USER   = "your_github_username"
GITHUB_REPO   = "binance-auto-trader"
AUTO_UPDATE   = True
```

When I push a new version, your bot will download and restart automatically on next startup. Your `config.py` is **never overwritten** by updates.

## Configuration

All settings are in `config.py`. Key settings:

| Setting | Default | Description |
|---|---|---|
| `TRADE_VALUE_USDC` | 100 | Trade size per pair in USDC |
| `TOP_N` | 0 | 0 = auto from balance |
| `MAX_PAIRS` | 20 | Maximum simultaneous pairs |
| `RESCAN_INTERVAL_HRS` | 6 | How often to rescan pairs |
| `DASHBOARD_PORT` | 8888 | Web dashboard port |

## Disclaimer

For educational purposes only. Not financial advice. Always test on Testnet first.
