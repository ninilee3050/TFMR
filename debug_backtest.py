
import yfinance as yf
import pandas as pd
from datetime import datetime

MAX_HISTORY = "10y"
INTERVAL = "1wk"

def download_weekly(ticker):
    try:
        df = yf.download(ticker, period=MAX_HISTORY, interval=INTERVAL, auto_adjust=False, progress=False, multi_level_index=False)
        return None if df.empty else df
    except: return None

def compute_ma(df):
    close = df['Close']
    df['MA5'] = close.rolling(5).mean()
    df['MA20'] = close.rolling(20).mean()
    df['MA50'] = close.rolling(50).mean()
    df['MA150'] = close.rolling(150).mean()
    df['MA200'] = close.rolling(200).mean()
    return df

def run_backtest_debug(df, ticker):
    # Parameters
    STEP_DROP_PCT = 0.03
    MAX_ROUNDS = 5
    UNIT_CASH = 1000.0 # Virtual unit size
    
    # State
    trade_log = []
    cycle_started = False
    gc_date = None
    
    in_pullback = False
    ready_for_pullback = False # Must be above MA20 first
    pullback_number = 0
    
    bearish_candle_idx = 0
    buy_round = 0
    prev_buy_close = 0.0
    
    # Position
    holding_units = 0
    total_cost = 0.0
    entry_date = None
    rounds_participated = 0
    current_buys = [] # Track buy details for chart marker

    # Loop
    # Valid data starts after MA200 is ready
    start_idx = 200
    
    # Find start idx for 2024-07-01 if possible
    # We will log everything for AAPL after 2024-07-01
    
    print(f"DEBUGGING AAPL BACKTEST from {df.index[start_idx]}")
    
    for i in range(start_idx, len(df)):
        curr = df.iloc[i]
        prev = df.iloc[i-1]
        dt = curr.name.date()
        
        # --- 1. MA Update (Done in df) ---
        
        # --- 2. GC Event ---
        # GC: MA20 crosses above MA50
        if prev['MA20'] <= prev['MA50'] and curr['MA20'] > curr['MA50']:
            cycle_started = True
            gc_date = curr.name.date()
            pullback_number = 0
            in_pullback = False
            ready_for_pullback = False # Reset state
            print(f"[{dt}] GC STARTED. Reset PB#=0")
        
        # Cycle Check
        if curr['MA20'] < curr['MA50']:
            cycle_started = False
            in_pullback = False
            ready_for_pullback = False
            # print(f"[{dt}] Cycle ENDED (MA20 < MA50)")
        
        # Additional Filters
        valid_trend = (curr['MA20'] > curr['MA50']) and \
                      (curr['MA150'] > curr['MA200']) and \
                      (curr['Close'] > curr['MA200'])

        # --- 3. Pullback Logic Update ---
        # Check for Recovery (Arms the trigger)
        if curr['Close'] > curr['MA20']:
            if not ready_for_pullback:
                # print(f"[{dt}] Recovered above MA20. Ready for PB.")
                ready_for_pullback = True
        
        # Condition B: Close < MA5 & MA20
        is_under_ma = (curr['Close'] < curr['MA5']) and (curr['Close'] < curr['MA20'])
        
        # Start Pullback (Only if recovered previously)
        if valid_trend and is_under_ma and not in_pullback and ready_for_pullback:
            in_pullback = True
            pullback_number += 1
            bearish_candle_idx = 0
            buy_round = 0
            prev_buy_close = 0.0
            ready_for_pullback = False # Trigger fired, must recover again for next #
            print(f"[{dt}] PULLBACK #{pullback_number} STARTED. Price={curr['Close']:.2f} < MA5/20")
            
        # End Pullback
        if in_pullback and (curr['Close'] > curr['MA5'] or curr['Close'] > curr['MA20']):
            in_pullback = False 
            print(f"[{dt}] PULLBACK ENDED. Price={curr['Close']:.2f} > MA5/20")

        # --- 4. Buyer Logic (Only if in_pullback) ---
        if in_pullback and valid_trend:
            # Check for Bearish Candle
            if curr['Close'] < curr['Open']:
                bearish_candle_idx += 1
                
                # Check Buy Round Trigger
                should_buy = False
                if buy_round == 0:
                    should_buy = True
                elif buy_round < MAX_ROUNDS:
                    if curr['Close'] <= prev_buy_close * (1.0 - STEP_DROP_PCT):
                        should_buy = True
                
                if should_buy:
                    buy_round += 1
                    
                    # Calculate drop from PREVIOUS buy price (if round > 1)
                    drop_val = 0.0
                    if buy_round > 1:
                        drop_val = (curr['Close'] - prev_buy_close) / prev_buy_close * 100.0
                    
                    prev_buy_close = float(curr['Close'])
                    
                    # EXECUTE BUY
                    step_units = buy_round 
                    holding_units += buy_round
                    total_cost += (float(curr['Close']) * buy_round)
                    rounds_participated = max(rounds_participated, buy_round)
                    
                    if entry_date is None:
                        entry_date = curr.name.date()
                        
                    # Record Buy Detail
                    current_buys.append({
                        "Date": curr.name, # Timestamp
                        "Price": float(curr['Close']),
                        "Round": buy_round,
                        "Drop": drop_val
                    })
                    print(f"[{dt}] BUY EXECUTED. Round={buy_round}, Price={curr['Close']:.2f}")

            else:
                # Bullish Candle inside Pullback?
                # print(f"[{dt}] Bullish/Doji inside Pullback. No Buy.")
                pass

        # --- 5. Exit Logic ---
        # Exit if Close > MA5 AND Close > MA20 (Take Profit)
        # OR Stop Loss? Strategy currently only mentions "Exit"
        
        # Check if we hold units
        if holding_units > 0:
            # Check Exit Condition: Close > MA5 AND Close > MA20
            # Strategy: "수익 청산: 종가 > 5일선 & 20일선"
            if (curr['Close'] > curr['MA5']) and (curr['Close'] > curr['MA20']):
                avg_price = total_cost / holding_units
                profit_pct = (curr['Close'] - avg_price) / avg_price * 100.0
                
                print(f"[{dt}] EXIT EXECUTED. Return={profit_pct:.2f}%")

                trade_log.append({
                    "Ticker": ticker,
                    "CycleStart": gc_date if gc_date else "Existing",
                    "Pullback#": pullback_number,
                    "EntryDate": entry_date.strftime("%Y-%m-%d"),
                    "ExitDate": curr.name.date().strftime("%Y-%m-%d"),
                    "Weeks": 0, # simplified
                    "Units": holding_units,
                    "AvgPrice": round(avg_price, 2),
                    "Return%": round(profit_pct, 2),
                    "Profit": 0, # simplified
                    "ExitPrice": float(curr['Close']),
                    "BuyDetails": list(current_buys),
                    "MaxRounds": rounds_participated
                })
                
                # Reset Position
                holding_units = 0
                total_cost = 0.0
                entry_date = None
                rounds_participated = 0
                current_buys = []

    return trade_log

if __name__ == "__main__":
    ticker = "AAPL"
    df = download_weekly(ticker)
    if df is not None:
        df = compute_ma(df)
        print("Data Loaded. Running Backtest...")
        logs = run_backtest_debug(df, ticker)
        print("Done.")
