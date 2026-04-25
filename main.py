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
UNIT_SIZE = 100 # $100 hypothetical bet

def get_mlb_odds():
    if not ODDS_API_KEY: return {}
    url = "https://api.the-odds-api.com/v4/sports/baseball_mlb/odds/"
    params = {'apiKey': ODDS_API_KEY, 'regions': 'us', 'markets': 'h2h', 'oddsFormat': 'american'}
    try:
        response = requests.get(url, params=params).json()
        odds_dict = {}
        for game in response:
            home = game['home_team']
            # Using the first available bookmaker
            bookie = game['bookmakers'][0]
            for outcome in bookie['markets'][0]['outcomes']:
                odds_dict[f"{home}_{outcome['name']}"] = outcome['price']
        return odds_dict
    except: return {}

def calculate_payout(odds, stake):
    """Calculates profit for a winning bet based on American odds."""
    if odds > 0: return stake * (odds / 100) # Underdog
    return stake / (abs(odds) / 100) # Favorite

def audit_and_stats():
    if not os.path.exists(CSV_FILE): return "No history yet.", ""
    
    df = pd.read_csv(CSV_FILE)
    yesterday = (date.today() - timedelta(days=1)).strftime("%m/%d/%Y")
    
    # 1. Update pending results
    updates_made = False
    for idx, row in df.iterrows():
        if row['Result'] == 'PENDING':
            # Check MLB API for result
            game_date = row['Date']
            actual_games = statsapi.schedule(date=game_date)
            for g in actual_games:
                matchup_str = f"{g['away_name']} @ {g['home_name']}"
                if matchup_str == row['Matchup'] and g['status'] == 'Final':
                    winner = g['winning_team']
                    df.at[idx, 'Result'] = 'WIN' if row['Predicted_Winner'] == winner else 'LOSS'
                    # Calculate P/L
                    if df.at[idx, 'Result'] == 'WIN':
                        df.at[idx, 'Profit'] = calculate_payout(row['Odds'], UNIT_SIZE)
                    else:
                        df.at[idx, 'Profit'] = -UNIT_SIZE
                    updates_made = True

    if updates_made:
        df.to_csv(CSV_FILE, index=False)

    # 2. Generate Stats
    final_df = df[df['Result'].isin(['WIN', 'LOSS'])]
    if final_df.empty: return "Waiting for first results...", ""

    # Yesterday's Performance
    y_df = final_df[final_df['Date'] == yesterday]
    y_correct = (y_df['Result'] == 'WIN').sum()
    y_total = len(y_df)
    y_profit = y_df['Profit'].sum()
    y_text = f"📊 *YESTERDAY:* {y_correct}/{y_total} ({y_profit:+.2f}$)" if y_total > 0 else "📊 *YESTERDAY:* No games finalized."

    # Lifetime Stats
    total_correct = (final_df['Result'] == 'WIN').sum()
    total_games = len(final_df)
    total_profit = final_df['Profit'].sum()
    accuracy = (total_correct / total_games) * 100
    
    stats_text = (
        f"📈 *LIFETIME STATS*\n"
        f"Correct: {total_correct}/{total_games} ({accuracy:.1f}%)\n"
        f"Total P/L: *${total_profit:,.2f}*"
    )
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
    live_odds = get_mlb_odds()
    new_predictions = []

    for game in games:
        # (Standard logic for status and lineups)
        status = game.get('status', 'Scheduled')
        if any(x in status for x in ['Postponed', 'Cancelled']): continue
        
        try:
            box = statsapi.boxscore_data(game['game_id'])
            h_lineup = box.get('home', {}).get('battingOrder', [])
            a_lineup = box.get('away', {}).get('battingOrder', [])
            if not h_lineup or not a_lineup: continue
            
            h_p_id = game.get('home_probable_pitcher_id') or get_player_id_by_name(game.get('home_probable_pitcher'))
            a_p_id = game.get('away_probable_pitcher_id') or get_player_id_by_name(game.get('away_probable_pitcher'))
            if not h_p_id or not a_p_id: continue

            h_edge = get_smoothed_bvp(a_p_id, h_lineup)
            a_edge = get_smoothed_bvp(h_p_id, a_lineup)
            
            winner = game['home_name'] if h_edge > a_edge else game['away_name']
            confidence = abs(h_edge - a_edge) * 100
            
            # Fetch current odds for the predicted winner
            odds = live_odds.get(f"{game['home_name']}_{winner}", -110) # Default to -110 if odds missing

            new_predictions.append({
                'Date': today, 'Matchup': f"{game['away_name']} @ {game['home_name']}",
                'Predicted_Winner': winner, 'Odds': odds, 'Confidence': round(confidence, 1),
                'Result': 'PENDING', 'Profit': 0.0
            })
        except: continue

    # Save Today's Picks
    if new_predictions:
        df_new = pd.DataFrame(new_predictions)
        df_new.to_csv(CSV_FILE, mode='a', index=False, header=not os.path.exists(CSV_FILE))

    # Audit & Format Message
    yesterday_msg, lifetime_msg = audit_and_stats()
    
    msg = f"⚾ *MLB QUANT REPORT: {today}*\n\n{yesterday_msg}\n{lifetime_msg}\n\n"
    if new_predictions:
        best = max(new_predictions, key=lambda x: x['Confidence'])
        msg += f"🔥 *BEST BET:* {best['Matchup']}\n👉 {best['Predicted_Winner']} ({best['Odds']})\n\n*TODAY'S SLATE:*\n"
        for p in new_predictions:
            msg += f"• {p['Matchup']}: {p['Predicted_Winner']} ({p['Odds']})\n"
    
    send_telegram(msg)

if __name__ == "__main__":
    run_analysis()
