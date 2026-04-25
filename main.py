import statsapi
import pandas as pd
import requests
import os
from datetime import date
from pybaseball import statcast_pitcher, playerid_lookup

# --- CONFIGURATION ---
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

def get_player_id_by_name(name):
    """Fallback to find player ID if the schedule object doesn't provide it."""
    if not name: return None
    try:
        player = statsapi.lookup_player(name)
        if player:
            return player[0]['id']
    except:
        return None
    return None

def get_smoothed_bvp(pitcher_id, lineup_ids):
    start_date = '2023-01-01'
    end_date = date.today().strftime("%Y-%m-%d")
    
    try:
        # Get statcast data for the pitcher
        pitches = statcast_pitcher(start_date, end_date, pitcher_id)
        # Filter for the specific batters in today's lineup
        matchups = pitches[pitches['batter'].isin(lineup_ids)].dropna(subset=['events'])
        
        if matchups.empty: return 0.320 # League average fallback
        
        on_base = matchups['events'].isin(['single', 'double', 'triple', 'home_run', 'walk', 'hit_by_pitch']).sum()
        pa = len(matchups)
        
        # Bayesian smoothing (adds a "prior" of 10 average at-bats)
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
    file_exists = os.path.isfile(file_path)
    df.to_csv(file_path, mode='a', index=False, header=not file_exists)

def run_analysis():
    today = date.today().strftime("%m/%d/%Y")
    games = statsapi.schedule(date=today)
    results = []

    for game in games:
        gid = game['game_id']
        status = game.get('status', 'Scheduled')
        is_doubleheader = game.get('doubleheader', 'N') != 'N'
        game_num = game.get('game_num', 1)
        
        matchup_name = f"{game['away_name']} @ {game['home_name']}"
        if is_doubleheader:
            matchup_name += f" (Game {game_num})"

        if any(x in status for x in ['Postponed', 'Cancelled', 'Delayed']):
            results.append({'Date': today, 'Matchup': matchup_name, 'Status': status.upper(), 'Confidence_Pct': 0})
            continue
            
        try:
            box = statsapi.boxscore_data(gid)
            h_lineup = box.get('home', {}).get('battingOrder', [])
            a_lineup = box.get('away', {}).get('battingOrder', [])
            
            if not h_lineup or not a_lineup:
                results.append({'Date': today, 'Matchup': matchup_name, 'Status': 'LINEUPS PENDING', 'Confidence_Pct': 0})
                continue
            
            # IMPROVED PITCHER LOOKUP
            # First try the direct ID from the schedule
            h_p_id = game.get('home_probable_pitcher_id')
            a_p_id = game.get('away_probable_pitcher_id')
            
            # If IDs are missing, look up by the name string
            if not h_p_id:
                h_p_id = get_player_id_by_name(game.get('home_probable_pitcher'))
            if not a_p_id:
                a_p_id = get_player_id_by_name(game.get('away_probable_pitcher'))

            if not h_p_id or not a_p_id:
                results.append({'Date': today, 'Matchup': matchup_name, 'Status': 'PITCHER PENDING', 'Confidence_Pct': 0})
                continue

            # Run analysis
            h_edge = get_smoothed_bvp(a_p_id, h_lineup)
            a_edge = get_smoothed_bvp(h_p_id, a_lineup)

            confidence = abs(h_edge - a_edge) * 100
            winner = game['home_name'] if h_edge > a_edge else game['away_name']

            results.append({
                'Date': today,
                'Matchup': matchup_name,
                'Predicted_Winner': winner,
                'Confidence_Pct': round(confidence, 1),
                'Status': 'ACTIVE'
            })
        except:
            continue

    if not results:
        send_telegram("⚠️ *MLB Bot:* No games found.")
        return

    # Filter and format
    active_results = [r for r in results if r['Status'] == 'ACTIVE']
    if active_results:
        log_predictions(active_results)

    msg = f"⚾ *MLB DAILY SLATE: {today}*\n\n"
    if active_results:
        best = max(active_results, key=lambda x: x['Confidence_Pct'])
        msg += f"🔥 *BEST BET:* {best['Matchup']}\n"
        msg += f"👉 *Pick:* {best['Predicted_Winner']} ({best['Confidence_Pct']}% Edge)\n\n"
    
    msg += "*ALL MATCHUPS:*\n"
    for r in results:
        if r['Status'] == 'ACTIVE':
            star = " 🌟" if (active_results and r['Matchup'] == best['Matchup']) else ""
            msg += f"• {r['Matchup']}{star}\n  👉 Pick: {r['Predicted_Winner']} ({r['Confidence_Pct']}%)\n\n"
        else:
            msg += f"• {r['Matchup']}\n  🚫 Status: *{r['Status']}*\n\n"
    
    send_telegram(msg)

if __name__ == "__main__":
    run_analysis()
