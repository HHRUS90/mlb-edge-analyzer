import statsapi
import pandas as pd
import requests
import os
import csv
import sys
from datetime import datetime, timedelta

# --- CONFIGURATION ---
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
ODDS_API_KEY = os.getenv('ODDS_API_KEY')
CSV_FILE = 'prediction_history.csv'
USAGE_FILE = 'api_usage.csv' 
UNIT_SIZE = 100 

# MDT is UTC-6 (Summer/Daylight) | MST is UTC-7 (Winter/Standard)
# Since it is April, we use 6.
UTC_OFFSET = 6 

def get_mst_now():
    """Returns the current datetime object in MDT (UTC-6)."""
    return datetime.utcnow() - timedelta(hours=UTC_OFFSET)

# ... [track_local_usage, get_mlb_odds, audit_and_stats remain same as previous version] ...

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
        utc_time = datetime.strptime(utc_string, "%Y-%m-%dT%H:%M:%SZ")
        mst_time = utc_time - timedelta(hours=UTC_OFFSET)
        return mst_time, mst_time.strftime("%I:%M %p")
    except: return None, "TBD"

def run_analysis():
    now_mst = get_mst_now()
    today_str = now_mst.strftime("%m/%d/%Y")
    
    # schedule() returns doubleheader info in the 'doubleHeader' and 'game_num' fields
    games = statsapi.schedule(date=today_str)
    live_odds, api_used, api_remaining, hard_stop, local_calls = get_mlb_odds()
    
    new_predictions, display_list = [], []
    history_df = pd.read_csv(CSV_FILE) if os.path.exists(CSV_FILE) else pd.DataFrame()

    for game in games:
        status = game.get('status', 'Scheduled').upper()
        
        # DOUBLEHEADER LOGIC
        dh_label = ""
        if game.get('doubleHeader') in ['Y', 'S']:
            dh_label = f" (Game {game.get('game_num')})"
        
        matchup = f"{game['away_name']} @ {game['home_name']}{dh_label}"
        mst_dt, mst_time_str = format_mst_time(game.get('game_datetime'))
        
        game_info = {'matchup': matchup, 'time': mst_time_str, 'status': status, 'is_active': False, 'raw_time': mst_dt}

        # Skip logic remains same...
        if not history_df.empty and not history_df[(history_df['Date'] == today_str) & (history_df['Matchup'] == matchup)].empty:
            existing = history_df[(history_df['Date'] == today_str) & (history_df['Matchup'] == matchup)].iloc[0]
            game_info.update({'is_active': True, 'winner': existing['Predicted_Winner'], 'odds': existing['Odds'], 'conf': existing['Confidence']})
            display_list.append(game_info); continue

        try:
            box = statsapi.boxscore_data(game['game_id'])
            h_l, a_l = box.get('home', {}).get('battingOrder', []), box.get('away', {}).get('battingOrder', [])
            
            # IMPROVED LINEUP DETECTION
            if not h_l or not a_l:
                # If it's within 2 hours of game time, we try to use the roster if lineups aren't set
                time_diff = mst_dt - now_mst
                if 0 < time_diff.total_seconds() < 7200: 
                    # Fallback: get top 9 players from the team's current season roster
                    # (Simplified for now to just wait for official lineups)
                    game_info['status'] = '⏳ WAITING FOR LINEUPS'
                else:
                    game_info['status'] = '📅 SCHEDULED'
                display_list.append(game_info); continue
            
            h_p_id = game.get('home_probable_pitcher_id')
            a_p_id = game.get('away_probable_pitcher_id')
            if not h_p_id or not a_p_id:
                game_info['status'] = '🧢 PITCHERS PENDING'; display_list.append(game_info); continue

            h_p_hand = get_player_info(h_p_id)
            a_p_hand = get_player_info(a_p_id)

            h_e = get_smoothed_bvp(a_p_id, h_l, a_p_hand)
            a_e = get_smoothed_bvp(h_p_id, a_l, h_p_hand)
            
            winner = game['home_name'] if h_e > a_e else game['away_name']
            conf = round(abs(h_e - a_e) * 100, 1)
            
            # Cleanup matchup for odds lookup (remove Game 1/2 label)
            odds_matchup = game['home_name']
            f_odds = format_odds(live_odds.get(f"{odds_matchup}_{winner}", -110))

            new_predictions.append({'Date': today_str, 'Matchup': matchup, 'Predicted_Winner': winner, 'Odds': f_odds, 'Confidence': conf, 'Result': 'PENDING', 'Profit': 0.0})
            game_info.update({'is_active': True, 'winner': winner, 'odds': f_odds, 'conf': conf})
            display_list.append(game_info)
        except: continue

    # Sort display list by game time properly
    display_list.sort(key=lambda x: x['raw_time'] if x['raw_time'] else datetime.max)
    
    # ... [Telegram sending logic remains same] ...
