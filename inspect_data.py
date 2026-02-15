
import yfinance as yf
import pandas as pd

def inspect_aapl():
    MAX_HISTORY = "10y"
    INTERVAL = "1wk"
    
    print("Downloading AAPL...")
    df = yf.download("AAPL", period=MAX_HISTORY, interval=INTERVAL, auto_adjust=False, progress=False, multi_level_index=False)
    
    # Check if empty
    if df.empty:
        print("Empty DataFrame")
        return

    # Compute MAs
    close = df['Close']
    df['MA5'] = close.rolling(5).mean()
    df['MA20'] = close.rolling(20).mean()
    df['MA50'] = close.rolling(50).mean()
    
    # Specific Dates
    # 2020-03-16 and 2022-03-07
    dates = ["2020-03-16", "2022-03-07", "2022-03-14"]
    
    print("\n--- Inspecting Specific Dates ---")
    for d_str in dates:
        try:
            ts = pd.Timestamp(d_str)
            # Find exact or nearest (weekly might start on MONDAY but data might vary slightly)
            # Just locate by string match if index is datetime
            idx = df.index.get_loc(d_str)
        except:
            # Try searching
            idx = df.index.searchsorted(pd.Timestamp(d_str))
            
        if idx < len(df):
            row = df.iloc[idx]
            dt = row.name.date()
            print(f"\nDate: {dt}")
            print(f"Open: {row['Open']:.2f}, Close: {row['Close']:.2f}")
            print(f"MA5: {row['MA5']:.2f}, MA20: {row['MA20']:.2f}")
            
            is_bearish = row['Close'] < row['Open']
            is_under_ma20 = row['Close'] < row['MA20']
            
            print(f"Bearish (Close < Open)? {is_bearish}")
            print(f"Under MA20 (Close < MA20)? {is_under_ma20}")
            print(f"Under MA5 (Close < MA5)? {row['Close'] < row['MA5']}")
        else:
            print(f"Date {d_str} not found.")

if __name__ == "__main__":
    inspect_aapl()
