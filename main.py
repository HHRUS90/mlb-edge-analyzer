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

def get_mst_now():
    tz = pytz.timezone('America/Denver')
    return datetime.now(tz)

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

def get_player_info(player_id):
    try:
        p = statsapi.get('person', {'personId': player_id})
        return p['people'][0].get('pitchHand', {}).get('code', 'R')
    except: return 'R'

def get_smoothed_bvp(pitcher_id, lineup_ids, p_hand):
    original_stdout = sys.stdout
    sys.stdout = open(os.devnull, 'w')
    try:
        from pybaseball import statcast_pitcher
        now_mst = get_mst_now()
        pitches = statcast_pitcher('2021-01-01', now_mst.strftime("%Y-%m-%d"), pitcher_id)
        sys.stdout = original_stdout
        matchups = pitches[pitches['batter'].isin(lineup_ids)].dropna(subset=['events'])
        
        default_obp = 0.310 if p_hand == 'L' else 0.320
        if matchups.empty: return default_obp
        
        on_base = matchups['events'].isin(['single','double','triple','home_run','walk','hit_by_pitch']).sum()
        return (on_base + (default_obp * 10)) / (len(matchups) + 10)
    except:
        sys.stdout = original_stdout
        return 0.315

def format_mst_time(utc_string):
    try:
        utc_dt = datetime.strptime(utc_string, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=pytz.UTC)
        denver_dt = utc_dt.astimezone(pytz.timezone('America/Denver'))
        return denver_dt, denver_dt.strftime("%I:%M %p")
    except: return None, "TBD"

def run_analysis():
    now_mst = get_mst_now()
    today_str = now_mst.strftime("%m/%d/%Y")
    
    games = statsapi.schedule(date=today_str)
    live_odds, api_used, api_remaining, _, local_calls = get_mlb_odds()
    
    display_list = []
    history_df = pd.read_csv(CSV_FILE) if os.path.exists(CSV_FILE) else pd.DataFrame()

    for game in games:
        h_p_id = game.get('home_probable_pitcher_id')
        a_p_id = game.get('away_probable_pitcher_id')
        h_p_name = game.get('home_probable_pitcher', 'TBD')
        a_p_name = game.get('away_probable_pitcher', 'TBD')
        
        mst_dt, mst_time_str = format_mst_time(game.get('game_datetime'))
        game_info = {
            'matchup': f"{game['away_name']} @ {game['home_name']}",
            'pitchers': f"({a_p_name} vs {h_p_name})",
            'time': mst_time_str,
            'status': game.get('status', 'Scheduled').upper(),
            'is_active': False,
            'raw_time': mst_dt
        }

        # Skip if game is cancelled
        if any(x in game_info['status'] for x in ['POSTPONED', 'CANCELLED']):
            display_list.append(game_info); continue

        # --- RECOVERY LOGIC: Get the best available lineup ---
        try:
            box = statsapi.boxscore_data(game['game_id'])
            h_l = box.get('home', {}).get('battingOrder', [])
            a_l = box.get('away', {}).get('battingOrder', [])
            
            # If boxscore is empty, fallback to the top 9 players on the active roster
            if not h_l or not a_l:
                h_roster = statsapi.get('team_roster', {'teamId': game['home_id']})['roster']
                a_roster = statsapi.get('team_roster', {'teamId': game['away_id']})['roster']
                h_l = [p['person']['id'] for p in h_roster[:9]]
                a_l = [p['person']['id'] for p in a_roster[:9]]
                game_info['status'] = '📊 ESTIMATED LINEUP'
            else:
                game_info['status'] = '✅ OFFICIAL LINEUP'

            if h_p_id and a_p_id:
                h_p_hand = get_player_info(h_p_id)
                a_p_hand = get_player_info(a_p_id)

                h_e = get_smoothed_bvp(a_p_id, h_l, a_p_hand)
                a_e = get_smoothed_bvp(h_p_id, a_l, h_p_hand)
                
                winner = game['home_name'] if h_e > a_e else game['away_name']
                conf = round(abs(h_e - a_e) * 100, 1)
                
                odds = live_odds.get(f"{game['home_name']}_{winner}", -110)
                game_info.update({'is_active': True, 'winner': winner, 'odds': odds, 'conf': conf})
            
        except Exception as e:
            print(f"Error: {e}")
        
        display_list.append(game_info)

    # (Sorting and Telegram sending logic remains same...)
