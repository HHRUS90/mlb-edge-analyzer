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

def format_odds(odds_val):
    try:
        val = int(odds_val)
        return f"+{val}" if val > 0 else str(val)
    except: return str(odds_val)

def calculate_payout(odds_str, stake):
    try:
        o = float(odds_str)
        if o > 0: return stake * (o / 100)
        return stake / (abs(o) / 100)
    except: return 0.0

def audit_and_stats():
    if not os.path.exists(CSV_FILE): return "📊 *TODAY:* N/A", "📊 *YESTERDAY:* N/A", "📈 *LIFETIME:* N/A"
    try:
        df = pd.read_csv(CSV_FILE)
    except: return "Error", "Error", "Error"
    now_mst = get_mst_now()
    today_str = now_mst.strftime("%m/%d/%Y")
    yesterday_str = (now_mst - timedelta(days=1)).strftime("%m/%d/%Y")
    updates_made = False
    for idx, row in df.iterrows():
        if str(row.get('Result')) == 'PENDING':
            actual_games = statsapi.schedule(date=row['Date'])
            for g in actual_games:
                dh_suffix = f" (Game {g.get('game_num')})" if g.get('doubleheader') in ['Y','S'] else ""
                matchup_str = f"{g['away_name']} @ {g['home_name']}{dh_suffix}"
                if matchup_str in row['Matchup'] and g['status'] == 'Final':
                    winner = g['winning_team']
                    df.at[idx, 'Result'] = 'WIN' if row['Predicted_Winner'] == winner else 'LOSS'
                    df.at[idx, 'Profit'] = calculate_payout(row['Odds'], UNIT_SIZE) if df.at[idx, 'Result'] == 'WIN' else -UNIT_SIZE
                    updates_made = True
    if updates_made: df.to_csv(CSV_FILE, index=False)
    def get_line_stats(target_df, label):
        if target_df.empty: return f"📊 *{label}:* N/A"
        wins = (target_df['Result'] == 'WIN').sum()
        total = len(target_df)
        acc = (wins / total) * 100 if total > 0 else 0
        profit = target_df['Profit'].sum()
        p_str = f"{'+$' if profit >= 0 else '-$'}{abs(profit):,.2f}"
        return f"📊 *{label}:* {wins}/{total} ({acc:.1f}%) | {p_str}"
    finalized = df[df['Result'].isin(['WIN', 'LOSS'])]
    t_msg = get_line_stats(finalized[finalized['Date'] == today_str], "TODAY")
    y_msg = get_line_stats(finalized[finalized['Date'] == yesterday_str], "YESTERDAY")
    l_msg = "📈 *LIFETIME:* N/A"
    if not finalized.empty:
        df['Date_DT'] = pd.to_datetime(df['Date'], format='%m/%d/%Y')
        l_acc = ((finalized['Result'] == 'WIN').sum() / len(finalized)) * 100
        l_profit = finalized['Profit'].sum()
        l_p_str = f"{'+$' if l_profit >= 0 else '-$'}{abs(l_profit):,.2f}"
        l_msg = f"📈 *LIFETIME (Since {df['Date_DT'].min().strftime('%m/%d/%Y')}):* {l_acc:.1f}% Acc | *{l_p_str}*"
    return t_msg, y_msg, l_msg

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

def get_pro_lineup(team_id):
    """
    Handles trades, major call-ups, and injury status.
    Prioritizes new arrivals (trades) and healthy regulars.
    """
    try:
        now_mst = get_mst_now()
        today_iso = now_mst.strftime("%Y-%m-%d")
        
        # 1. Check today's transactions for arrivals (Trades, Recalls, Activations)
        trans = statsapi.get('transactions', {'teamId': team_id, 'startDate': today_iso})
        new_arrival_ids = [t['personId'] for t in trans if t['typeCode'] in ['TR', 'RE', 'AC']]
        
        # 2. Get the healthy roster (Status code 'A' for Active)
        roster = statsapi.get('team_roster', {'teamId': team_id})['roster']
        healthy_ids = [
            p['person']['id'] for p in roster 
            if p.get('status', {}).get('code') == 'A' 
            and "Injured" not in p.get('status', {}).get('description', '')
        ]
        
        # 3. Get regulars by games played this season
        leaders = statsapi.team_leader_data(team_id, 'gamesPlayed', limit=20)
        regular_ids = [leader[0] for leader in leaders]
        
        # 4. Build the 9-man priority list
        final_lineup = []
        # Priority 1: New arrivals that are on the healthy roster
        for p_id in new_arrival_ids:
            if p_id in healthy_ids: final_lineup.append(p_id)
        
        # Priority 2: Healthy regulars
        for p_id in regular_ids:
            if p_id in healthy_ids and p_id not in final_lineup:
                final_lineup.append(p_id)
            if len(final_lineup) >= 9: break
            
        # Safety: Take first 9 healthy if still short
        if len(final_lineup) < 9:
            for p_id in healthy_ids:
                if p_id not in final_lineup: final_lineup.append(p_id)
                if len(final_lineup) >= 9: break
                
        return final_lineup[:9]
    except Exception as e:
        print(f"Pro Lineup Error: {e}")
        return []

def format_mst_time(utc_string):
    try:
        utc_dt = datetime.strptime(utc_string, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=pytz.UTC)
        denver_dt = utc_dt.astimezone(pytz.timezone('America/Denver'))
        return denver_dt, denver_dt.strftime("%I:%M %p")
    except: return None, "TBD"

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, data={'chat_id': TELEGRAM_CHAT_ID, 'text': message, 'parse_mode': 'Markdown'})

def run_analysis():
    now_mst = get_mst_now()
    today_str = now_mst.strftime("%m/%d/%Y")
    games = statsapi.schedule(date=today_str)
    live_odds, api_used, api_remaining, _, local_calls = get_mlb_odds()
    new_predictions, display_list = [], []
    history_df = pd.read_csv(CSV_FILE) if os.path.exists(CSV_FILE) else pd.DataFrame()

    for game in games:
        h_p_id, a_p_id = game.get('home_probable_pitcher_id'), game.get('away_probable_pitcher_id')
        h_p_name, a_p_name = game.get('home_probable_pitcher', 'TBD'), game.get('away_probable_pitcher', 'TBD')
        status = game.get('status', 'Scheduled').upper()
        dh_label = f" (Game {game['game_num']})" if game.get('doubleheader') in ['Y', 'S'] else ""
        away_odds = format_odds(live_odds.get(f"{game['home_name']}_{game['away_name']}", "N/A"))
        home_odds = format_odds(live_odds.get(f"{game['home_name']}_{game['home_name']}", "N/A"))
        matchup_display = f"{game['away_name']} ({away_odds}) @ {game['home_name']} ({home_odds}){dh_label}"
        mst_dt, mst_time_str = format_mst_time(game.get('game_datetime'))
        game_info = {'matchup': matchup_display, 'pitchers': f"({a_p_name} vs {h_p_name})", 'time': mst_time_str, 'status': status, 'is_active': False, 'raw_time': mst_dt}

        if any(x in status for x in ['POSTPONED', 'CANCELLED']):
            display_list.append(game_info); continue

        if not history_df.empty:
            existing = history_df[(history_df['Date'] == today_str) & (history_df['Matchup'] == matchup_display)]
            if not existing.empty:
                row = existing.iloc[0]
                game_info.update({'is_active': True, 'winner': row['Predicted_Winner'], 'odds': row['Odds'], 'conf': row['Confidence'], 'status': '✅ PREDICTED'})
                display_list.append(game_info); continue

        try:
            box = statsapi.boxscore_data(game['game_id'])
            h_l = box.get('home', {}).get('battingOrder', [])
            a_l = box.get('away', {}).get('battingOrder', [])
            lineup_type = "✅ OFFICIAL"
            
            if not h_l or not a_l:
                h_l, a_l = get_pro_lineup(game['home_id']), get_pro_lineup(game['away_id'])
                lineup_type = "📊 PRO-ESTIMATED"

            if h_p_id and a_p_id and h_l and a_l:
                h_p_hand, a_p_hand = get_player_info(h_p_id), get_player_info(a_p_id)
                h_e, a_e = get_smoothed_bvp(a_p_id, h_l, a_p_hand), get_smoothed_bvp(h_p_id, a_l, h_p_hand)
                winner = game['home_name'] if h_e > a_e else game['away_name']
                conf = round(abs(h_e - a_e) * 100, 1)
                start_odds = format_odds(live_odds.get(f"{game['home_name']}_{winner}", -110))
                new_predictions.append({'Date': today_str, 'Matchup': matchup_display, 'Predicted_Winner': winner, 'Odds': start_odds, 'Confidence': conf, 'Result': 'PENDING', 'Profit': 0.0})
                game_info.update({'is_active': True, 'winner': winner, 'odds': start_odds, 'conf': conf, 'status': lineup_type})
            else: game_info['status'] = '⏳ DATA PENDING'
        except Exception as e: print(f"Error {game['game_id']}: {e}"); continue
        display_list.append(game_info)

    if new_predictions: pd.DataFrame(new_predictions).to_csv(CSV_FILE, mode='a', index=False, header=not os.path.exists(CSV_FILE), quoting=csv.QUOTE_NONNUMERIC)
    t_msg, y_msg, l_msg = audit_and_stats()
    usage_msg = f"💳 *API USAGE:* {local_calls} calls ({api_remaining} left)"
    msg = f"⚾ *MLB PRO REPORT: {today_str}*\n\n{t_msg}\n{y_msg}\n{l_msg}\n\n{usage_msg}\n\n"
    active_preds = [g for g in display_list if g.get('is_active')]
    if active_preds:
        best = max(active_preds, key=lambda x: x['conf'])
        msg += f"🔥 *BEST BET:* {best['matchup']}\n👉 {best['winner']} ({best['odds']}) — {best['conf']}% Edge\n\n"
    msg += "*DAILY SCHEDULE:*\n"
    display_list.sort(key=lambda x: x['raw_time'] if x['raw_time'] else datetime.max)
    for g in display_list:
        if g.get('is_active'):
            msg += f"• [{g['time']}] {g['matchup']}\n  _{g['pitchers']}_\n  👉 {g['winner']} ({g['odds']}) | {g['conf']}% | {g['status']}\n\n"
        else:
            msg += f"• [{g['time']}] {g['matchup']}\n  _{g['pitchers']}_\n  {g['status']}\n\n"
    send_telegram(msg)

if __name__ == "__main__": run_analysis()
