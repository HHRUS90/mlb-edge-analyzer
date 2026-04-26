import statsapi
import pandas as pd
import requests
import os
import csv
import sys
import pytz
from datetime import datetime, timedelta

# --- CONFIGURATION ---
ODDS_CALL_LIMIT = 450         
UNIT_SIZE = 100               
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
ODDS_API_KEY = os.getenv('ODDS_API_KEY')
CSV_FILE = 'prediction_history.csv'
USAGE_FILE = 'api_usage.csv' 
CACHE_DIR = 'pitcher_cache'
EVAL_LOG = 'evaluation_log.txt'

if not os.path.exists(CACHE_DIR):
    os.makedirs(CACHE_DIR)

def get_mst_now():
    tz = pytz.timezone('America/Denver')
    return datetime.now(tz)

def get_cache_stats():
    local_size_bytes = 0
    for dirpath, dirnames, filenames in os.walk(CACHE_DIR):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            local_size_bytes += os.path.getsize(fp)
    
    local_mb = local_size_bytes / (1024 * 1024)
    # Estimated GitHub overhead (2x Setup-Python + compressed archives)
    gh_overhead_mb = 325.0 
    
    total_estimated_mb = local_mb + gh_overhead_mb
    gh_limit_mb = 10 * 1024 
    percent_used = (total_estimated_mb / gh_limit_mb) * 100
    
    return (f"📂 *TOTAL STORAGE USAGE*\n"
            f"• Live Data: {local_mb:.2f} MB\n"
            f"• Env & Archives: {gh_overhead_mb:.2f} MB\n"
            f"• GH Limit: 10 GB ({percent_used:.2f}%)")

def track_local_usage():
    now_mst = get_mst_now()
    current_month = now_mst.strftime("%Y-%m")
    if not os.path.exists(USAGE_FILE):
        with open(USAGE_FILE, 'w') as f:
            f.write("Month,Calls\n")
            f.write(f"{current_month},0\n")
    df = pd.read_csv(USAGE_FILE)
    if current_month not in df['Month'].values:
        new_row = pd.DataFrame([{'Month': current_month, 'Calls': 0}])
        df = pd.concat([df, new_row], ignore_index=True)
    return df, current_month

def get_mlb_odds():
    usage_df, current_month = track_local_usage()
    local_calls = int(usage_df.loc[usage_df['Month'] == current_month, 'Calls'].values[0])
    if not ODDS_API_KEY or local_calls >= ODDS_CALL_LIMIT:
        return {}, "N/A", "N/A", True, local_calls
    url = "https://api.the-odds-api.com/v4/sports/baseball_mlb/odds/"
    params = {'apiKey': ODDS_API_KEY, 'bookmakers': 'fanduel', 'markets': 'h2h', 'oddsFormat': 'american'}
    try:
        response = requests.get(url, params=params)
        if response.status_code == 200:
            usage_df.loc[usage_df['Month'] == current_month, 'Calls'] += 1
            usage_df.to_csv(USAGE_FILE, index=False)
        used = response.headers.get('x-requests-used', '0')
        remaining = response.headers.get('x-requests-remaining', '0')
        data = response.json()
        odds_dict = {}
        for game in data:
            home = game['home_team']
            if game.get('bookmakers'):
                bookie = game['bookmakers'][0]
                for outcome in bookie['markets'][0]['outcomes']:
                    odds_dict[f"{home}_{outcome['name']}"] = outcome['price']
        return odds_dict, used, remaining, False, local_calls + 1
    except: return {}, "0", "0", False, local_calls

def format_odds(odds_val):
    try:
        if odds_val == "N/A" or odds_val is None: return "N/A"
        val = int(float(odds_val))
        return f"+{val}" if val > 0 else str(val)
    except: return str(odds_val)

def calculate_payout(odds_str, stake):
    try:
        o = float(odds_str)
        if o > 0: return stake * (o / 100)
        return stake / (abs(o) / 100)
    except: return 0.0

def audit_and_stats():
    if not os.path.exists(CSV_FILE): 
        return "📊 *TODAY:* N/A", "📊 *YESTERDAY:* N/A", "📈 *LIFETIME:* N/A"
    try:
        df = pd.read_csv(CSV_FILE)
        df['Date'] = df['Date'].astype(str)
    except: return "Error", "Error", "Error"

    now_mst = get_mst_now()
    today_str = now_mst.strftime("%m/%d/%Y")
    yesterday_str = (now_mst - timedelta(days=1)).strftime("%m/%d/%Y")
    updates_made = False

    for idx, row in df.iterrows():
        if str(row.get('Result')).strip().upper() == 'PENDING':
            actual_games = statsapi.schedule(date=row['Date'])
            for g in actual_games:
                matchup_key = f"{g['home_name']}"
                if matchup_key in row['Matchup'] and g['status'] == 'Final':
                    winner = g['winning_team']
                    result = 'WIN' if row['Predicted_Winner'] == winner else 'LOSS'
                    profit = calculate_payout(row['Odds'], UNIT_SIZE) if result == 'WIN' else -UNIT_SIZE
                    df.at[idx, 'Result'] = result
                    df.at[idx, 'Profit'] = profit
                    updates_made = True

    if updates_made: df.to_csv(CSV_FILE, index=False)

    def get_line_stats(target_df, label):
        finalized = target_df[target_df['Result'].isin(['WIN', 'LOSS'])]
        if finalized.empty: return f"📊 *{label}:* N/A"
        wins = (finalized['Result'] == 'WIN').sum()
        total = len(finalized)
        acc = (wins / total) * 100
        profit = finalized['Profit'].sum()
        p_str = f"{'+$' if profit >= 0 else '-$'}{abs(profit):,.2f}"
        return f"📊 *{label}:* {wins}/{total} ({acc:.1f}%) | {p_str}"

    t_msg = get_line_stats(df[df['Date'] == today_str], "TODAY")
    y_msg = get_line_stats(df[df['Date'] == yesterday_str], "YESTERDAY")
    all_finalized = df[df['Result'].isin(['WIN', 'LOSS'])]
    if all_finalized.empty:
        l_msg = "📈 *LIFETIME:* N/A"
    else:
        l_acc = ((all_finalized['Result'] == 'WIN').sum() / len(all_finalized)) * 100
        l_profit = all_finalized['Profit'].sum()
        l_p_str = f"{'+$' if l_profit >= 0 else '-$'}{abs(l_profit):,.2f}"
        df['DT'] = pd.to_datetime(df['Date'], format='%m/%d/%Y')
        l_msg = f"📈 *LIFETIME (Since {df['DT'].min().strftime('%m/%d/%Y')}):* {l_acc:.1f}% Acc | *{l_p_str}*"
    return t_msg, y_msg, l_
