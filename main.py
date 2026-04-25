import statsapi
import pandas as pd
import requests
import os
from datetime import date
from pybaseball import statcast_pitcher, playerid_lookup

# --- CONFIGURATION ---
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

def get_pitcher_id(name):
    try:
        last, first = name.split(', ')
        lookup = playerid_lookup(last, first)
        return lookup['key_mlbam'].values[0]
    except:
        return None

def get_smoothed_bvp(pitcher_id, lineup_ids):
    start_date = '2023-01-01'
    end_date = date.today().strftime("%Y-%m-%d")
    
    try:
        pitches = statcast_pitcher(start_date, end_date, pitcher_id)
        matchups = pitches[pitches['batter'].isin(lineup_ids)].dropna(subset=['events'])
        
        if matchups.empty: return 0.320
        
        on_base = matchups['events'].isin(['single', 'double', 'triple', 'home_run', 'walk', 'hit_by_pitch']).sum()
        pa = len(matchups)
        
        return (on_base + 3.2) / (pa + 10)
    except:
        return 0.320

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {'chat_id': TELEGRAM_CHAT_ID, 'text': message, 'parse_mode': 'Markdown'}
    requests.post(url, data=payload)

def log_predictions(prediction_data):
    file_path = 'prediction_history.csv'
    df = pd.DataFrame(prediction_data)
    
    # If the file doesn't exist, include the header. Otherwise, append without header.
    file_exists = os.path.isfile(file_path)
    df.to_csv(file_path, mode='a', index=False, header=not file_exists)

def run_analysis():
    today = date.today().strftime("%m/%d/%Y")
    games = statsapi.schedule(date=today)
    results = []

    for game in games:
        gid = game['game_id']
        try:
            box = statsapi.boxscore_data(gid)
            h_lineup = [p['person']['id'] for p in box['home']['battingOrder']]
            a_lineup = [p['person']['id'] for p in box['away']['battingOrder']]
            
            h_p_id = game.get('home_probable_pitcher_id')
            a_p_id = game.get('away_probable_pitcher_id')

            h_edge = get_smoothed_bvp(a_p_id, h_lineup)
            a_edge = get_smoothed_bvp(h_p_id, a_lineup)

            confidence = abs(h_edge - a_edge) * 100
            winner = game['home_name'] if h_edge > a_edge else game['away_name']

            results.append({
                'Date': today,
                'Matchup': f"{game['away_name']} @ {game['home_name']}",
                'Predicted_Winner': winner,
                'Confidence_Pct': round(confidence, 1),
                'Home_Edge': round(h_edge, 3),
                'Away_Edge': round(a_edge, 3)
            })
        except:
            continue

    if not results:
        send_telegram("⚠️ *MLB Bot:* No confirmed lineups found yet.")
        return

    # Log to CSV
    log_predictions(results)

    # Format the Telegram Message
    best = max(results, key=lambda x: x['Confidence_Pct'])
    
    msg = f"⚾ *MLB DAILY EDGE: {today}*\n\n"
    msg += f"🔥 *BEST BET:* {best['Predicted_Winner']} ({best['Confidence_Pct']}% Edge)\n\n"
    msg += "*ALL MATCHUPS:*\n"
    
    for r in results:
        # Don't list the best bet twice
        if r['Matchup'] != best['Matchup']:
            msg += f"• {r['Matchup']}\n  👉 Edge: *{r['Predicted_Winner']}* ({r['Confidence_Pct']}%)\n\n"
    
    send_telegram(msg)

if __name__ == "__main__":
    run_analysis()
