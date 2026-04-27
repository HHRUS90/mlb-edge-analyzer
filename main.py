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
        if 'Game_Num' not in df.columns: df['Game_Num'] = 1
        df['Date'] = df['Date'].astype(str)
        df['Game_Num'] = df['Game_Num'].fillna(1).astype(int)
    except: return "Error", "Error", "Error"

    now_mst = get_mst_now()
    today_str = now_mst.strftime("%m/%d/%Y")
    yesterday_str = (now_mst - timedelta(days=1)).strftime("%m/%d/%Y")
    updates_made = False

    for idx, row in df.iterrows():
        if str(row.get('Result')).strip().upper() == 'PENDING':
            actual_games = statsapi.schedule(date=row['Date'])
            for g in actual_games:
                if g['home_name'] in row['Matchup'] and int(g.get('game_num', 1)) == int(row['Game_Num']):
                    if g['status'] == 'Final':
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

    return get_line_stats(df[df['Date'] == today_str], "TODAY"), \
           get_line_stats(df[df['Date'] == yesterday_str], "YESTERDAY"), \
           "📈 *LIFETIME:* " + (f"{((df[df['Result'].isin(['WIN','LOSS'])]['Result'] == 'WIN').sum() / len(df[df['Result'].isin(['WIN','LOSS'])])) * 100:.1f}% Acc" if not df[df['Result'].isin(['WIN','LOSS'])].empty else "N/A")

def get_player_info(player_id):
    try:
        p = statsapi.get('person', {'personId': player_id})
        return p['people'][0].get('pitchHand', {}).get('code', 'R'), p['people'][0].get('fullName', f"Unknown")
    except: return 'R', f"Unknown"

def get_smoothed_bvp(pitcher_id, lineup_ids, p_hand):
    cache_path = os.path.join(CACHE_DIR, f"{pitcher_id}.csv")
    use_cache = False
    if os.path.exists(cache_path):
        if (datetime.now() - datetime.fromtimestamp(os.path.getmtime(cache_path))).days < 1: use_cache = True
    
    original_stdout = sys.stdout
    sys.stdout = open(os.devnull, 'w')
    try:
        from pybaseball import statcast_pitcher
        if use_cache: 
            pitches = pd.read_csv(cache_path)
        else:
            pitches = statcast_pitcher('2021-01-01', datetime.now().strftime("%Y-%m-%d"), pitcher_id)
            essential_cols = ['batter', 'events', 'description', 'game_date']
            pitches = pitches[pitches.columns.intersection(essential_cols)]
            pitches.to_csv(cache_path, index=False)
        sys.stdout = original_stdout
        
        matchups = pitches[pitches['batter'].isin(lineup_ids)].dropna(subset=['events'])
        default_obp = 0.310 if p_hand == 'L' else 0.320
        if matchups.empty: return default_obp, 0
        on_base = matchups['events'].isin(['single','double','triple','home_run','walk','hit_by_pitch']).sum()
        smoothed = (on_base + (default_obp * 10)) / (len(matchups) + 10)
        return smoothed, len(matchups)
    except:
        sys.stdout = original_stdout
        return 0.315, 0

def get_pro_lineup(team_id):
    try:
        roster = statsapi.get('team_roster', {'teamId': team_id})['roster']
        healthy_ids = [p['person']['id'] for p in roster if p.get('status', {}).get('code') == 'A']
        leaders = statsapi.team_leader_data(team_id, 'gamesPlayed', limit=20)
        regular_ids = [leader[0] for leader in leaders]
        return [p_id for p_id in regular_ids if p_id in healthy_ids][:9]
    except: return []

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
    live_odds, _, _, _, _ = get_mlb_odds()
    new_predictions, display_list = [], []
    eval_log_lines = [f"EVALUATION LOG - {today_str}\n" + "="*40 + "\n"]
    history_df = pd.read_csv(CSV_FILE) if os.path.exists(CSV_FILE) else pd.DataFrame()
    
    raw_schedule = statsapi.get('schedule', {'sportId': 1, 'date': today_str, 'hydrate': 'probablePitcher'})
    raw_games_map = {rg['gamePk']: rg for d in raw_schedule.get('dates', []) for rg in d.get('games', [])}

    for game in games:
        rg = raw_games_map.get(game['game_id'], {})
        h_p_name_api = game.get('home_probable_pitcher') or "TBD"
        a_p_name_api = game.get('away_probable_pitcher') or "TBD"
        status = game.get('status', 'Scheduled').upper()
        is_live_or_final = any(x in status for x in ["IN PROGRESS", "LIVE", "FINAL"])
        game_num = int(game.get('game_num', 1))
        
        existing_row = pd.Series()
        if not history_df.empty:
            matches = history_df[(history_df['Date'] == today_str) & (history_df['Matchup'].str.contains(game['home_name']))]
            if len(matches) > 1:
                matches = matches[matches['Game_Num'].astype(int) == game_num]
            if not matches.empty: existing_row = matches.iloc[0]

        away_o_h = format_odds(live_odds.get(f"{game['home_name']}_{game['away_name']}", "N/A"))
        home_o_h = format_odds(live_odds.get(f"{game['home_name']}_{game['home_name']}", "N/A"))
        score = f" | 🏟 *SCORE: {game.get('away_score', 0)} - {game.get('home_score', 0)}*" if is_live_or_final else ""
        matchup_display = f"{game['away_name']} ({away_o_h}) @ {game['home_name']} ({home_o_h}){score}"
        mst_dt, mst_time_str = format_mst_time(game.get('game_datetime'))
        game_info = {'matchup': matchup_display, 'pitchers': f"({a_p_name_api} vs {h_p_name_api})", 'time': mst_time_str, 'status': status, 'is_active': False, 'raw_time': mst_dt}

        try:
            box = statsapi.boxscore_data(game['game_id'])
            # Create a name map for the log
            name_map = {}
            for team in ['home', 'away']:
                for pid, pdata in box[team]['players'].items():
                    name_map[int(pid.replace('ID', ''))] = pdata['person']['fullName']

            h_p_id = rg.get('teams', {}).get('home', {}).get('probablePitcher', {}).get('id') or (box['home']['pitchers'][0] if box.get('home', {}).get('pitchers') else None)
            a_p_id = rg.get('teams', {}).get('away', {}).get('probablePitcher', {}).get('id') or (box['away']['pitchers'][0] if box.get('away', {}).get('pitchers') else None)

            if h_p_id and a_p_id:
                h_p_hand, h_p_name = get_player_info(h_p_id)
                a_p_hand, a_p_name = get_player_info(a_p_id)
                h_l, a_l = box.get('home', {}).get('battingOrder', []), box.get('away', {}).get('battingOrder', [])
                lineup_type = "✅ OFF" if (h_l and a_l) else "📊 EST"
                if not h_l or not a_l: h_l, a_l = get_pro_lineup(game['home_id']), get_pro_lineup(game['away_id'])
                
                h_e, h_samples = get_smoothed_bvp(a_p_id, h_l, a_p_hand)
                a_e, a_samples = get_smoothed_bvp(h_p_id, a_l, h_p_hand)
                winner = game['home_name'] if h_e > a_e else game['away_name']
                conf = round(abs(h_e - a_e) * 100, 1)

                # --- DETAILED EVAL LOG ---
                eval_log_lines.append(f"GAME: {game['away_name']} @ {game['home_name']} (Game {game_num}) [{lineup_type}]\n")
                
                # Away Pitcher vs Home Lineup
                h_lineup_names = [name_map.get(pid, f"Unknown({pid})") for pid in h_l]
                eval_log_lines.append(f"  - Pitcher: {a_p_name} (Away)\n")
                eval_log_lines.append(f"  - Target Lineup: {', '.join(h_lineup_names)}\n")
                eval_log_lines.append(f"  - Result: {h_e:.3f} OBP over {h_samples} historical ABs\n\n")

                # Home Pitcher vs Away Lineup
                a_lineup_names = [name_map.get(pid, f"Unknown({pid})") for pid in a_l]
                eval_log_lines.append(f"  - Pitcher: {h_p_name} (Home)\n")
                eval_log_lines.append(f"  - Target Lineup: {', '.join(a_lineup_names)}\n")
                eval_log_lines.append(f"  - Result: {a_e:.3f} OBP over {a_samples} historical ABs\n")
                eval_log_lines.append(f"  - PICK: {winner} | Edge: {conf}%\n")
                eval_log_lines.append("-" * 40 + "\n")

                if not existing_row.empty:
                    game_info.update({'is_active': True, 'winner': existing_row['Predicted_Winner'], 'odds': format_odds(existing_row['Odds']), 'conf': existing_row['Confidence'], 'status': f'✅ PRED ({status})'})
                else:
                    start_odds = format_odds(live_odds.get(f"{game['home_name']}_{winner}", -110))
                    new_predictions.append({'Date': today_str, 'Matchup': matchup_display, 'Predicted_Winner': winner, 'Odds': start_odds, 'Confidence': conf, 'Result': 'PENDING', 'Profit': 0.0, 'Game_Num': game_num})
                    game_info.update({'is_active': True, 'winner': winner, 'odds': start_odds, 'conf': conf, 'status': f"{lineup_type} ({status})"})
            else: game_info['status'] = f'⏳ DATA ({status})'
        except Exception: continue
        display_list.append(game_info)

    if new_predictions: pd.DataFrame(new_predictions).to_csv(CSV_FILE, mode='a', index=False, header=not os.path.exists(CSV_FILE), quoting=csv.QUOTE_NONNUMERIC)
    
    with open(EVAL_LOG, 'w') as f: f.writelines(eval_log_lines)
    
    t_msg, y_msg, l_msg = audit_and_stats()
    msg = f"⚾ *MLB PRO REPORT: {today_str}*\n\n{t_msg}\n{y_msg}\n{l_msg}\n\n{get_cache_stats()}\n\n"
    active_preds = [g for g in display_list if g.get('is_active')]
    if active_preds:
        best = max(active_preds, key=lambda x: x['conf'])
        msg += f"🔥 *BEST BET:* {best['matchup']}\n👉 {best['winner']} ({best['odds']}) — {best['conf']}% Edge\n\n"
    
    display_list.sort(key=lambda x: x['raw_time'] if x['raw_time'] else datetime.max)
    for g in display_list:
        if g.get('is_active'):
            msg += f"• [{g['time']}] {g['matchup']}\n  _{g['pitchers']}_\n  👉 {g['winner']} ({g['odds']}) | {g['conf']}% | {g['status']}\n\n"
        else:
            msg += f"• [{g['time']}] {g['matchup']}\n  _{g['pitchers']}_\n  {g['status']}\n\n"
    send_telegram(msg)

if __name__ == "__main__": run_analysis()
