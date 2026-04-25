import statsapi
import pandas as pd
import requests
import os
import csv
from datetime import date, timedelta, datetime

# --- CONFIGURATION ---
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
ODDS_API_KEY = os.getenv('ODDS_API_KEY')
CSV_FILE = 'prediction_history.csv'
USAGE_FILE = 'api_usage.csv' 
UNIT_SIZE = 100 
HARD_STOP_THRESHOLD = 50 

def track_local_usage():
    today = date.today()
    current_month = today.strftime("%Y-%m")
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

    if not ODDS_API_KEY or local_calls >= 450:
        return {}, "N/A", "N/A", True, local_calls
    
    url = "https://api.the-odds-api.com/v4/sports/baseball_mlb/odds/"
    params = {
        'apiKey': ODDS_API_KEY,
        'bookmakers': 'fanduel', # <--- This tells the API to only return FanDuel
        'markets': 'h2h',
        'oddsFormat': 'american'
    }
    
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
            
            # Since we filtered by bookmakers=fanduel, FanDuel will be the only entry 
            # in the list if they have odds posted for that game.
            if game.get('bookmakers'):
                bookie = game['bookmakers'][0] 
                for outcome in bookie['markets'][0]['outcomes']:
                    odds_dict[f"{home}_{outcome['name']}"] = outcome['price']
        
        return odds_dict, used, remaining, False, local_calls + 1
    except:
        return {}, "0", "0", False, local_calls

def format_odds(odds_val):
    """Adds a plus sign to positive odds for display and CSV storage."""
    try:
        val = int(odds_val)
        return f"+{val}" if val > 0 else str(val)
    except:
        return str(odds_val)

def calculate_payout(odds_str, stake):
    """Handles odds strings (like '+110') for payout calculations."""
    try:
        o = float(odds_str)
        if o > 0: return stake * (o / 100)
        return stake / (abs(o) / 100)
    except: return 0.0

def audit_and_stats():
    if not os.path.exists(CSV_FILE): return "No history found.", ""
    try:
        df = pd.read_csv(CSV_FILE, on_bad_lines='skip')
    except: return "Error reading history.", ""
    yesterday = (date.today() - timedelta(days=1)).strftime("%m/%d/%Y")
    updates_made = False
    for idx, row in df.iterrows():
        if str(row.get('Result')) == 'PENDING':
            actual_games = statsapi.schedule(date=row['Date'])
            for g in actual_games:
                matchup_str = f"{g['away_name']} @ {g['home_name']}"
                if matchup_str == row['Matchup'] and g['status'] == 'Final':
                    winner = g['winning_team']
                    df.at[idx, 'Result'] = 'WIN' if row['Predicted_Winner'] == winner else 'LOSS'
                    df.at[idx, 'Profit'] = calculate_payout(row['Odds'], UNIT_SIZE) if df.at[idx, 'Result'] == 'WIN' else -UNIT_SIZE
                    updates_made = True
    if updates_made:
        df.to_csv(CSV_FILE, index=False, quoting=csv.QUOTE_NONNUMERIC)
    final_df = df[df['Result'].isin(['WIN', 'LOSS'])]
    if final_df.empty: return "Waiting for first results...", ""
    y_df = final_df[final_df['Date'] == yesterday]
    y_wins, y_total = (y_df['Result'] == 'WIN').sum(), len(y_df)
    y_text = f"📊 *YESTERDAY:* {y_wins}/{y_total} ({y_df['Profit'].sum():+.2f}$)" if y_total > 0 else "📊 *YESTERDAY:* No games finalized."
    acc = ((final_df['Result'] == 'WIN').sum() / len(final_df)) * 100
    stats_text = f"📈 *LIFETIME:* {acc:.1f}% Accuracy | *${final_df['Profit'].sum():,.2f}*"
    return y_text, stats_text

def get_player_id_by_name(name):
    try:
        p = statsapi.lookup_player(name)
        return p[0]['id'] if p else None
    except: return None

def get_smoothed_bvp(pitcher_id, lineup_ids):
    try:
        from pybaseball import statcast_pitcher
        pitches = statcast_pitcher('2023-01-01', date.today().strftime("%Y-%m-%d"), pitcher_id)
        matchups = pitches[pitches['batter'].isin(lineup_ids)].dropna(subset=['events'])
        if matchups.empty: return 0.320
        on_base = matchups['events'].isin(['single','double','triple','home_run','walk','hit_by_pitch']).sum()
        return (on_base + 3.2) / (len(matchups) + 10)
    except: return 0.320

def format_mst_time(utc_string):
    try:
        utc_time = datetime.strptime(utc_string, "%Y-%m-%dT%H:%M:%SZ")
        mst_time = utc_time - timedelta(hours=7)
        return mst_time.strftime("%I:%M %p")
    except:
        return "TBD"

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, data={'chat_id': TELEGRAM_CHAT_ID, 'text': message, 'parse_mode': 'Markdown'})

def run_analysis():
    today = date.today().strftime("%m/%d/%Y")
    games = statsapi.schedule(date=today)
    live_odds, api_used, api_remaining, hard_stop, local_calls = get_mlb_odds()
    
    sorted_games = sorted(games, key=lambda x: x.get('game_datetime', ''))
    
    new_predictions = []
    display_list = []

    for game in sorted_games:
        status = game.get('status', 'Scheduled').upper()
        matchup = f"{game['away_name']} @ {game['home_name']}"
        mst_time = format_mst_time(game.get('game_datetime'))
        
        game_info = {'matchup': matchup, 'time': mst_time, 'status': status, 'is_active': False}

        if any(x in status for x in ['POSTPONED', 'CANCELLED', 'DELAYED']):
            game_info['status'] = f"🛑 {status}"
            display_list.append(game_info)
            continue
            
        try:
            box = statsapi.boxscore_data(game['game_id'])
            h_lineup, a_lineup = box.get('home', {}).get('battingOrder', []), box.get('away', {}).get('battingOrder', [])
            
            if not h_lineup or not a_lineup:
                game_info['status'] = '⏳ LINEUPS PENDING'
                display_list.append(game_info)
                continue
            
            h_p_id = game.get('home_probable_pitcher_id') or get_player_id_by_name(game.get('home_probable_pitcher'))
            a_p_id = game.get('away_probable_pitcher_id') or get_player_id_by_name(game.get('away_probable_pitcher'))
            
            if not h_p_id or not a_p_id:
                game_info['status'] = '🧢 PITCHERS PENDING'
                display_list.append(game_info)
                continue

            h_e, a_e = get_smoothed_bvp(a_p_id, h_lineup), get_smoothed_bvp(h_p_id, a_lineup)
            winner = game['home_name'] if h_e > a_e else game['away_name']
            conf = round(abs(h_e - a_e) * 100, 1)
            raw_odds = live_odds.get(f"{game['home_name']}_{winner}", -110)
            formatted_odds = format_odds(raw_odds)

            new_predictions.append({
                'Date': today, 'Matchup': matchup, 'Predicted_Winner': winner, 
                'Odds': formatted_odds, 'Confidence': conf, 'Result': 'PENDING', 'Profit': 0.0
            })
            
            game_info.update({'is_active': True, 'winner': winner, 'odds': formatted_odds, 'conf': conf})
            display_list.append(game_info)
        except:
            continue

    if new_predictions:
        df_new = pd.DataFrame(new_predictions)
        file_exists = os.path.exists(CSV_FILE)
        df_new.to_csv(CSV_FILE, mode='a', index=False, header=not file_exists, quoting=csv.QUOTE_NONNUMERIC)

    yesterday_msg, lifetime_msg = audit_and_stats()
    usage_msg = f"💳 *API USAGE*\n• Local Ticker: {local_calls}\n• API Reported: {api_used} Used | {api_remaining} Left"

    msg = f"⚾ *MLB QUANT REPORT: {today}*\n\n{yesterday_msg}\n{lifetime_msg}\n\n{usage_msg}\n\n"
    
    active_preds = [g for g in display_list if g['is_active']]
    if active_preds:
        best = max(active_preds, key=lambda x: x['conf'])
        msg += f"🔥 *BEST BET:* {best['matchup']}\n👉 {best['winner']} ({best['odds']}) — {best['conf']}% Edge\n\n"
    
    msg += "*DAILY SCHEDULE (MST):*\n"
    for g in display_list:
        if g['is_active']:
            star = " 🌟" if g['matchup'] == (active_preds and best['matchup']) else ""
            msg += f"• [{g['time']}] {g['matchup']}{star}\n  👉 Pick: {g['winner']} ({g['odds']}) — {g['conf']}% Edge\n\n"
        else:
            msg += f"• [{g['time']}] {g['matchup']}\n  {g['status']}\n\n"
    
    send_telegram(msg)

if __name__ == "__main__":
    run_analysis()
