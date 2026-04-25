import statsapi
import pandas as pd
import requests
import os
from datetime import date, timedelta

# --- CONFIGURATION ---
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
ODDS_API_KEY = os.getenv('ODDS_API_KEY')
CSV_FILE = 'prediction_history.csv'
UNIT_SIZE = 100 
HARD_STOP_THRESHOLD = 50 # Stop at 450 used (50 remaining)

def get_mlb_odds():
    """Fetches odds and tracks API usage via response headers."""
    if not ODDS_API_KEY: 
        return {}, "API Key Missing", "N/A"
    
    url = "https://api.the-odds-api.com/v4/sports/baseball_mlb/odds/"
    params = {'apiKey': ODDS_API_KEY, 'regions': 'us', 'markets': 'h2h', 'oddsFormat': 'american'}
    
    try:
        response = requests.get(url, params=params)
        
        # Pull usage stats from headers
        remaining = int(response.headers.get('x-requests-remaining', 0))
        used = int(response.headers.get('x-requests-used', 0))
        
        # Hard Stop Check
        if remaining < HARD_STOP_THRESHOLD:
            return {}, used, remaining, True # True triggers the hard-stop alert

        data = response.json()
        odds_dict = {}
        for game in data:
            home = game['home_team']
            bookie = game['bookmakers'][0]
            for outcome in bookie['markets'][0]['outcomes']:
                odds_dict[f"{home}_{outcome['name']}"] = outcome['price']
                
        return odds_dict, used, remaining, False
    except Exception as e:
        return {}, "Error", "Error", False

def calculate_payout(odds, stake):
    if odds > 0: return stake * (odds / 100)
    return stake / (abs(odds) / 100)

def audit_and_stats():
    if not os.path.exists(CSV_FILE): return "No history yet.", ""
    df = pd.read_csv(CSV_FILE)
    yesterday = (date.today() - timedelta(days=1)).strftime("%m/%d/%Y")
    
    updates_made = False
    for idx, row in df.iterrows():
        if row['Result'] == 'PENDING':
            actual_games = statsapi.schedule(date=row['Date'])
            for g in actual_games:
                matchup_str = f"{g['away_name']} @ {g['home_name']}"
                if matchup_str == row['Matchup'] and g['status'] == 'Final':
                    winner = g['winning_team']
                    df.at[idx, 'Result'] = 'WIN' if row['Predicted_Winner'] == winner else 'LOSS'
                    df.at[idx, 'Profit'] = calculate_payout(row['Odds'], UNIT_SIZE) if df.at[idx, 'Result'] == 'WIN' else -UNIT_SIZE
                    updates_made = True

    if updates_made: df.to_csv(CSV_FILE, index=False)
    final_df = df[df['Result'].isin(['WIN', 'LOSS'])]
    if final_df.empty: return "Waiting for results...", ""

    y_df = final_df[final_df['Date'] == yesterday]
    y_text = f"📊 *YESTERDAY:* { (y_df['Result'] == 'WIN').sum() }/{len(y_df)} ({y_df['Profit'].sum():+.2f}$)" if not y_df.empty else "📊 *YESTERDAY:* No games finalized."
    
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
        return (matchups['events'].isin(['single','double','triple','home_run','walk','hit_by_pitch']).sum() + 3.2) / (len(matchups) + 10)
    except: return 0.320

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, data={'chat_id': TELEGRAM_CHAT_ID, 'text': message, 'parse_mode': 'Markdown'})

def run_analysis():
    today = date.today().strftime("%m/%d/%Y")
    games = statsapi.schedule(date=today)
    
    # Odds fetching with hard-stop logic
    live_odds, used, remaining, hard_stop_triggered = get_mlb_odds()
    
    new_predictions = []
    for game in games:
        status = game.get('status', 'Scheduled')
        if any(x in status for x in ['Postponed', 'Cancelled']): continue
        try:
            box = statsapi.boxscore_data(game['game_id'])
            h_lineup, a_lineup = box.get('home', {}).get('battingOrder', []), box.get('away', {}).get('battingOrder', [])
            if not h_lineup or not a_lineup: continue
            h_p_id = game.get('home_probable_pitcher_id') or get_player_id_by_name(game.get('home_probable_pitcher'))
            a_p_id = game.get('away_probable_pitcher_id') or get_player_id_by_name(game.get('away_probable_pitcher'))
            if not h_p_id or not a_p_id: continue

            h_e, a_e = get_smoothed_bvp(a_p_id, h_lineup), get_smoothed_bvp(h_p_id, a_lineup)
            winner = game['home_name'] if h_e > a_e else game['away_name']
            
            # Default to -110 if hard stop triggered or odds missing
            odds = live_odds.get(f"{game['home_name']}_{winner}", -110)

            new_predictions.append({
                'Date': today, 'Matchup': f"{game['away_name']} @ {game['home_name']}",
                'Predicted_Winner': winner, 'Odds': odds, 'Confidence': round(abs(h_e - a_e)*100, 1),
                'Result': 'PENDING', 'Profit': 0.0
            })
        except: continue

    if new_predictions:
        df_new = pd.DataFrame(new_predictions)
        df_new.to_csv(CSV_FILE, mode='a', index=False, header=not os.path.exists(CSV_FILE))

    yesterday_msg, lifetime_msg = audit_and_stats()
    
    # Construct Message
    usage_msg = f"💳 *API USAGE:* {used} Used | {remaining} Left"
    if hard_stop_triggered:
        usage_msg = "🚨 *API HARD STOP:* Quota low (<50). Using default -110 odds."

    msg = f"⚾ *MLB QUANT REPORT: {today}*\n\n{yesterday_msg}\n{lifetime_msg}\n{usage_msg}\n\n"
    
    if new_predictions:
        best = max(new_predictions, key=lambda x: x['Confidence'])
        msg += f"🔥 *BEST BET:* {best['Matchup']}\n👉 {best['Predicted_Winner']} ({best['Odds']})\n\n*TODAY:* " + "".join([f"\n• {p['Matchup']}: {p['Predicted_Winner']}" for p in new_predictions])
    
    send_telegram(msg)

if __name__ == "__main__":
    run_analysis()
