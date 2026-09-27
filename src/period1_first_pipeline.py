"""
first_pipeline.py
Download daily data for one ticker, compute returns,
plot the equity curve, save everything in standard structure.
"""

import yfinance as yf
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from pathlib import Path

# Standard folders, created if absent
ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
PROCESSED = ROOT / "data" / "processed"
RESULTS= ROOT / "results"
for p in (RAW, PROCESSED, RESULTS):
    p.mkdir(parents=True, exist_ok = True)

TICKER = "SPY"

# 1. Acquire: download once, store raw, reload from disk after
raw_file = RAW / f'{TICKER}_daily.csv'
if not raw_file.exists():
    data = yf.download(TICKER, start = '2010-01-01', auto_adjust=True)
    # Recent yfinance versions return a two-level (field, ticker) header,
    # even when only one ticker is requested. Keep the cached CSV simple.
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.get_level_values(0)
    data.to_csv(raw_file)

# Also support CSVs already written with yfinance's two-level header.
with raw_file.open(encoding='utf-8') as f:
    f.readline()
    has_ticker_header = f.readline().startswith('Ticker,')

header = [0, 1] if has_ticker_header else 0
prices = pd.read_csv(raw_file, header=header, index_col=0, parse_dates=True)
if isinstance(prices.columns, pd.MultiIndex):
    prices.columns = prices.columns.get_level_values(0)

# 2. Transform: simple returns from adjusted closes
close = prices['Close'].astype(float)
returns = close.pct_change().dropna()
returns.to_csv(PROCESSED/ f'{TICKER}_returns.csv')

# 3. Summarise: the quantities from sections 4 and 5
arith_mean_ann = returns.mean() * 252
vol_ann = returns.std() * (252 ** 0.5)
geo_approx = arith_mean_ann - (vol_ann ** 2) / 2
print(f'Arithmetic mean (annualised): {arith_mean_ann:.2%}')
print(f'Volatility (annualised): {vol_ann:.2%}')
print(f'Geometric approx: {geo_approx:.2%}')

# 4. Visualise: growth of one dollar, saved not shown
equity = (1 + returns).cumprod()
fig, ax = plt.subplots(figsize=(8,4))
ax.plot(equity.index, equity.values, color='black', linewidth = 1)
ax.set_title(f'{TICKER}: growth of 1 dollar')
ax.set_ylabel('Equity multiple')
fig.savefig(RESULTS / f'{TICKER}_equity.png', dpi=150, 
    bbox_inches='tight')
print(f'Saved figure to {RESULTS}')
