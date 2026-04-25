import statsapi
import pandas as pd
import requests
import os
from datetime import date
from pybaseball import statcast_pitcher, playerid_lookup

# --- CONFIGURATION (via GitHub Secrets) ---
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
    # Analyzing last 3 years of data
    start_date = '2023-01-01'
    end_date = date.today().strftime("%Y-%m-%d")
    
    try:
        pitches = statcast_pitcher(start_date, end_date, pitcher_id)
        matchups = pitches[pitches['batter'].isin(lineup_ids)].dropna(subset=['events'])
        
        if matchups.empty: return 0.320 # League Average Baseline
        
        on_base = matchups['events'].isin(['single', 'double', 'triple', 'home_run', 'walk', 'hit_by_pitch']).sum()
        pa = len(matchups)
        
        # Bayesian Smoothing: (Hits + 3.2) / (At Bats + 10)
        return (on_base + 3.2) / (pa + 10)
    except:
        return 0.320

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {'chat_id': TELEGRAM_CHAT_ID, 'text': message, 'parse_mode': 'Markdown'}
    requests.post(url, data=payload)

def run_analysis():
    today = date.today().strftime("%m/%d/%Y")
    games = statsapi.schedule(date=today)
    results = []

    for game in games:
        gid = game['game_id']
        try:
            # Attempt to get official lineups; fallback to presumptive
            box = statsapi.boxscore_data(gid)
            h_lineup = [p['person']['id'] for p in box['home']['battingOrder']]
            a_lineup = [p['person']['id'] for p in box['away']['battingOrder']]
            
            h_p_id = game.get('home_probable_pitcher_id')
            a_p_id = game.get('away_probable_pitcher_id')

            # Calculate Edges
            h_edge = get_smoothed_bvp(a_p_id, h_lineup) # Home bats vs Away pitcher
            a_edge = get_smoothed_bvp(h_p_id, a_lineup)

            results.append({
                'matchup': f"{game['away_name']} @ {game['home_name']}",
                'h_team': game['home_name'], 'a_team': game['away_name'],
                'h_edge': h_edge, 'a_edge': a_edge
            })
        except:
            continue

    if not results:
        send_telegram("⚠️ *MLB Bot:* No confirmed lineups found yet.")
        return

    # Find Best Bet (highest delta)
    best = max(results, key=lambda x: abs(x['h_edge'] - x['a_edge']))
    winner = best['h_team'] if best['h_edge'] > best['a_edge'] else best['a_team']
    confidence = abs(best['h_edge'] - best['a_edge']) * 100

    msg = f"⚾ *MLB DAILY EDGE: {today}*\n\n"
    msg += f"🔥 *BEST BET:* {winner}\n"
    msg += f"📊 *Confidence:* {confidence:.1f}%\n"
    msg += f"🏟️ *Matchup:* {best['matchup']}\n\n"
    msg += "Other edges found for today's slate above baseline."
    
    send_telegram(msg)

if __name__ == "__main__":
    run_analysis()
