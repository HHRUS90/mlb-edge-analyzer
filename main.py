import statsapi
import pandas as pd
import requests
import os
import csv
from datetime import date, timedelta

# --- CONFIGURATION ---
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
ODDS_API_KEY = os.getenv('ODDS_API_KEY')
CSV_FILE = 'prediction_history.csv'
USAGE_FILE = 'api_usage.csv' # New file to track our calls
UNIT_SIZE = 100 
HARD_STOP_THRESHOLD = 50 

def track_local_usage():
    """Tracks calls locally and handles monthly resets."""
    today = date.today()
    current_month = today.strftime("%Y-%m")
    
    # Create file if it doesn't exist
    if not os.path.exists(USAGE_FILE):
        with open(USAGE_FILE, 'w') as f:
            f.write("Month,Calls\n")
            f.write(f"{current_month},0\n")
    
    df = pd.read_csv(USAGE_FILE)
    
    # Reset if it's a new month
    if current_month not in df['Month'].values:
        new_row = pd.DataFrame([{'Month': current_month, 'Calls': 0}])
        df = pd.concat([df, new_row], ignore_index=True)
    
    return df, current_month

def get_mlb_odds():
    usage_df, current_month = track_local_usage()
    local_calls = usage_df.loc[usage_df['Month'] == current_month, 'Calls'].values[0]

    if not ODDS_API_KEY or local_calls >= 450: # Internal Hard Stop
        return {}, "N/A", "N/A", True, local_calls
    
    url = "https://api.the-odds-api.com/v4/sports/baseball_mlb/odds/"
    params = {'apiKey': ODDS_API_KEY, 'regions': 'us', 'markets': 'h2h', 'oddsFormat': 'american'}
    
    try:
        response = requests.get(url, params=params)
        
        # Increment local counter on success
        if response.status_code == 200:
            usage_df.loc[usage_df['Month'] == current_month, 'Calls'] += 1
            usage_df.to_csv(USAGE_FILE, index=False)
        
        used = response.headers.get('x-requests-used', '0')
        remaining = response.headers.get('x-requests-remaining', '0')
        
        data = response.json()
        odds_dict = {}
        for game in data:
            home = game['home_team']
            bookie = game['bookmakers'][0]
            for outcome in bookie['markets'][0]['outcomes']:
                odds_dict[f"{home}_{outcome['name']}"] = outcome['price']
        
        return odds_dict, used, remaining, False, local_calls + 1
    except:
        return {}, "0", "0", False, local_calls

# ... (audit_and_stats, get_player_id, get_smoothed_bvp functions remain the same) ...

def run_analysis():
    today = date.today().strftime("%m/%d/%Y")
    games = statsapi.schedule(date=today)
    
    # Updated to receive local_calls
    live_odds, api_used, api_remaining, hard_stop, local_calls = get_mlb_odds()
    
    # ... (prediction logic remains the same) ...
    # (Saving today's picks logic remains the same)

    yesterday_msg, lifetime_msg = audit_and_stats()
    
    # Constructing the New Ticker Message
    usage_msg = (
        f"💳 *API USAGE*\n"
        f"• Local Ticker: {local_calls} calls this month\n"
        f"• API Reported: {api_used} Used | {api_remaining} Left"
    )
    if hard_stop: 
        usage_msg = "🚨 *API HARD STOP: Local limit reached (450+)*"

    msg = f"⚾ *MLB QUANT REPORT: {today}*\n\n{yesterday_msg}\n{lifetime_msg}\n\n{usage_msg}\n\n"
    
    # ... (Best bet and all matchups logic remains same) ...
    
    send_telegram(msg)

if __name__ == "__main__":
    run_analysis()
