import statsapi
import pandas as pd
import requests
import os
import csv
import sys
import pytz
import traceback
import time
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
    """Returns current Denver time."""
    tz = pytz.timezone('America/Denver')
    return datetime.now(tz)

def get_cache_stats():
    """Calculates storage usage for GitHub Actions safety."""
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
    """Monitors monthly API usage to stay under free-tier caps."""
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
    """Retrieves odds from FanDuel via The-Odds-API."""
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
    """Formats float odds into American ML strings."""
    try:
        if odds_val == "N/A" or odds_val is None: return "N/A"
        val = int(float(odds_val))
        return f"+{val}" if val > 0 else str(val)
    except: return str(odds_val)

def calculate_payout(odds_str, stake):
    """Determines profit for ROI tracking."""
    try:
        o = float(odds_str)
        if o > 0: return stake * (o / 100)
        return stake / (abs(o) / 100)
    except: return 0.0

def audit_and_stats():
    """Updates PENDING games and calculates performance metrics."""
    if not os.path.exists(CSV_FILE): 
        return "📊 *TODAY:* N/A", "📊 *YESTERDAY:* N/A", "0/0 (0.0%) | $0.00"
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

    finalized_all = df[df['Result'].isin(['WIN', 'LOSS'])]
    if finalized_all.empty:
        lifetime_str = "0/0 (0.0%) | $0.00"
    else:
        l_wins = (finalized_all['Result'] == 'WIN').sum()
        l_total = len(finalized_all)
        l_acc = (l_wins / l_total) * 100
        l_profit = finalized_all['Profit'].sum()
        l_p_str = f"{'+$' if l_profit >= 0 else '-$'}{abs(l_profit):,.2f}"
        lifetime_str = f"{l_wins}/{l_total} ({l_acc:.1f}%) | {l_p_str}"

    return get_line_stats(df[df['Date'] == today_str], "TODAY"), \
           get_line_stats(df[df['Date'] == yesterday_str], "YESTERDAY"), \
           lifetime_str

def get_player_info(player_id):
    """Retrieves biographical data from MLB API."""
    try:
        p = statsapi.get('person', {'personId': player_id})
        return p['people'][0].get('pitchHand', {}).get('code', 'R'), p['people'][0].get('fullName', f"ID:{player_id}")
    except: return 'R', f"ID:{player_id}"

def get_pro_lineup(team_id):
    """
    SMART FALLBACK: If today's lineup isn't out, fetch the lineup from the last game played.
    This provides a much more accurate prediction than just grabbing the top 9 roster players.
    """
    try:
        # Get the team's most recently completed game ID
        last_game_id = statsapi.last_game(team_id)
        if last_game_id:
            box = statsapi.boxscore_data(last_game_id)
            # Find whether the team was 'home' or 'away' in that last game
            team_key = 'home' if box['home']['team']['id'] == team_id else 'away'
            # Return the batting order from that game
            return box[team_key].get('battingOrder', [])
    except: pass
    return []

def get_smoothed_bvp(pitcher_id, lineup_ids, p_hand, name_map):
    """Calculates OBP using career Statcast data and Bayesian smoothing."""
    cache_path = os.path.join(CACHE_DIR, f"{pitcher_id}.csv")
    use_cache = False
    if os.path.exists(cache_path):
        if (datetime.now() - datetime.fromtimestamp(os.path.getmtime(cache_path))).days < 1: 
            use_cache = True
    
    details = []
    original_stdout = sys.stdout
    sys.stdout = open(os.devnull, 'w')
    
    try:
        from pybaseball import statcast_pitcher
        pitches = None
        
        if use_cache: 
            pitches = pd.read_csv(cache_path)
        else:
            for attempt in range(3):
                try:
                    pitches = statcast_pitcher('2021-01-01', datetime.now().strftime("%Y-%m-%d"), pitcher_id)
                    if not pitches.empty:
                        essential_cols = ['batter', 'events', 'description', 'game_date']
                        pitches = pitches[pitches.columns.intersection(essential_cols)]
                        pitches.to_csv(cache_path, index=False)
                        break
                except:
                    time.sleep(2)
                    continue
        
        sys.stdout = original_stdout
        
        default_obp = 0.310 if p_hand == 'L' else 0.320
        # If still no data after retries, return the default baseline
        if pitches is None or pitches.empty:
            return default_obp, 0, [f"    - NO RECENT HISTORY FOUND FOR PITCHER {pitcher_id}"]
            
        total_hits = 0
        total_at_bats = 0
        
        # Calculate BvP for every player in the provided lineup
        for b_id in lineup_ids:
            matchups = pitches[pitches['batter'] == b_id].dropna(subset=['events'])
            # Attempt to resolve name from local map or API
            b_name = name_map.get(b_id) or get_player_info(b_id)[1]
            
            if matchups.empty:
                details.append(f"    - {b_name}: NO HISTORY (Defaulting {default_obp})")
            else:
                on_base = matchups['events'].isin(['single','double','triple','home_run','walk','hit_by_pitch']).sum()
                total_hits += on_base
                total_at_bats += len(matchups)
                details.append(f"    - {b_name}: {on_base}/{len(matchups)} ({(on_base/len(matchups)):.3f})")

        # Smoothing formula to stabilize small samples
        smoothed = (total_hits + (default_obp * 10)) / (total_at_bats + 10)
        return smoothed, total_at_bats, details
    except:
        sys.stdout = original_stdout
        return 0.315, 0, [f"    - DATA RETRIEVAL ERROR FOR PITCHER {pitcher_id}"]

def format_mst_time(utc_string):
    """Converts game start time to MST."""
    try:
        utc_dt = datetime.strptime(utc_string, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=pytz.UTC)
        denver_dt = utc_dt.astimezone(pytz.timezone('America/Denver'))
        return denver_dt, denver_dt.strftime("%I:%M %p")
    except: return None, "TBD"

def send_telegram(message):
    """Sends the summary report to Telegram."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, data={'chat_id': TELEGRAM_CHAT_ID, 'text': message, 'parse_mode': 'Markdown'})

def run_analysis():
    """Main loop: Identifies matchups, calculates edges, and logs details."""
    now_mst = get_mst_now()
    today_str = now_mst.strftime("%m/%d/%Y")
    games = statsapi.schedule(date=today_str)
    live_odds, _, _, _, _ = get_mlb_odds()
    new_predictions, display_list = [], []
    eval_log_lines = [f"DETAILED EVALUATION LOG - {today_str}\n" + "="*50 + "\n"]
    history_df = pd.read_csv(CSV_FILE) if os.path.exists(CSV_FILE) else pd.DataFrame()
    csv_updated = False
    
    raw_schedule = statsapi.get('schedule', {'sportId': 1, 'date': today_str, 'hydrate': 'probablePitcher'})
    raw_games_map = {rg['gamePk']: rg for d in raw_schedule.get('dates', []) for rg in d.get('games', [])}

    for game in games:
        name_map = {} 
        rg = raw_games_map.get(game['game_id'], {})
        h_p_name_api = game.get('home_probable_pitcher') or "TBD"
        a_p_name_api = game.get('away_probable_pitcher') or "TBD"
        status = game.get('status', 'Scheduled').upper()
        is_live_or_final = any(x in status for x in ["IN PROGRESS", "LIVE", "FINAL"])
        game_num = int(game.get('game_num', 1))
        
        # ID existing predictions to avoid duplicates
        existing_idx = -1
        existing_row = pd.Series()
        if not history_df.empty:
            matches = history_df[(history_df['Date'] == today_str) & (history_df['Matchup'].str.contains(game['home_name'])) & (history_df['Game_Num'].astype(int) == game_num)]
            if not matches.empty:
                existing_idx = matches.index[0]
                existing_row = matches.iloc[0]

        away_o_h = format_odds(live_odds.get(f"{game['home_name']}_{game['away_name']}", "N/A"))
        home_o_h = format_odds(live_odds.get(f"{game['home_name']}_{game['home_name']}", "N/A"))
        score = f" | 🏟 *SCORE: {game.get('away_score', 0)} - {game.get('home_score', 0)}*" if is_live_or_final else ""
        matchup_display = f"{game['away_name']} ({away_o_h}) @ {game['home_name']} ({home_o_h}){score}"
        mst_dt, mst_time_str = format_mst_time(game.get('game_datetime'))
        game_info = {'matchup': matchup_display, 'pitchers': f"({a_p_name_api} vs {h_p_name_api})", 'time': mst_time_str, 'status': status, 'is_active': False, 'raw_time': mst_dt}

        eval_log_lines.append(f"GAME: {game['away_name']} @ {game['home_name']} (G{game_num})\n")

        try:
            box = {}
            try:
                box = statsapi.boxscore_data(game['game_id'])
                for team in ['home', 'away']:
                    players = box.get(team, {}).get('players', {})
                    for pid, pdata in players.items():
                        try:
                            name_map[int(pid.replace('ID', ''))] = pdata['person']['fullName']
                        except: continue
            except: pass

            h_p_id = rg.get('teams', {}).get('home', {}).get('probablePitcher', {}).get('id')
            if not h_p_id:
                h_pitchers = box.get('home', {}).get('pitchers', [])
                if h_pitchers: h_p_id = h_pitchers[0]

            a_p_id = rg.get('teams', {}).get('away', {}).get('probablePitcher', {}).get('id')
            if not a_p_id:
                a_pitchers = box.get('away', {}).get('pitchers', [])
                if a_pitchers: a_p_id = a_pitchers[0]

            if h_p_id and a_p_id:
                h_p_hand, h_p_name = get_player_info(h_p_id)
                a_p_hand, a_p_name = get_player_info(a_p_id)
                
                # Check for official lineup first
                h_l, a_l = box.get('home', {}).get('battingOrder', []) if box else [], box.get('away', {}).get('battingOrder', []) if box else []
                
                lineup_source = "OFFICIAL BOXSCORE" if (h_l and a_l) else "ESTIMATED LAST GAME"
                # If official is missing, trigger the Smart Fallback (Last Game)
                if not h_l or not a_l: 
                    h_l = h_l if h_l else get_pro_lineup(game['home_id'])
                    a_l = a_l if a_l else get_pro_lineup(game['away_id'])
                
                # If still empty (e.g. season opener), log the skip
                if not h_l or not a_l:
                    eval_log_lines.append(f"  SKIPPED: No Lineup Data (H:{len(h_l)} A:{len(a_l)})\n" + "-"*50 + "\n")
                    game_info['status'] = f'⏳ LINEUP ({status})'
                    display_list.append(game_info)
                    continue

                h_e, h_samples, h_details = get_smoothed_bvp(a_p_id, h_l, a_p_hand, name_map)
                a_e, a_samples, a_details = get_smoothed_bvp(h_p_id, a_l, h_p_hand, name_map)
                winner = game['home_name'] if h_e > a_e else game['away_name']
                conf = round(abs(h_e - a_e) * 100, 2)

                eval_log_lines.append(f"  Source: {lineup_source}\n")
                eval_log_lines.append(f"  [OFFENSE: {game['home_name']} vs {a_p_name}]\n")
                eval_log_lines.extend([d + "\n" for d in h_details])
                eval_log_lines.append(f"  >> Aggregated Home OBP: {h_e:.3f}\n\n")

                eval_log_lines.append(f"  [OFFENSE: {game['away_name']} vs {h_p_name}]\n")
                eval_log_lines.extend([d + "\n" for d in a_details])
                eval_log_lines.append(f"  >> Aggregated Away OBP: {a_e:.3f}\n\n")

                eval_log_lines.append(f"  CALC: abs({h_e:.3f} - {a_e:.3f}) * 100 = {conf}%\n")
                eval_log_lines.append(f"  RESULT: {winner}\n" + "-"*50 + "\n")

                # Update logic to overwrite "Estimated" predictions with "Official" ones if confidence changes
                should_update_csv = (existing_idx == -1)
                if not existing_row.empty and existing_row.get('Confidence', 0) <= 1.0 and conf > 1.0:
                    should_update_csv = True

                if should_update_csv:
                    start_odds = format_odds(live_odds.get(f"{game['home_name']}_{winner}", -110))
                    pred_data = {'Date': today_str, 'Matchup': matchup_display, 'Predicted_Winner': winner, 'Odds': start_odds, 'Confidence': conf, 'Result': 'PENDING', 'Profit': 0.0, 'Game_Num': game_num}
                    if existing_idx != -1:
                        history_df = history_df.astype(object)
                        history_df.iloc[existing_idx] = pd.Series(pred_data)
                        csv_updated = True
                    else:
                        new_predictions.append(pred_data)
                    game_info.update({'is_active': True, 'winner': winner, 'odds': start_odds, 'conf': conf, 'status': f"{'✅ OFF' if (lineup_source == 'OFFICIAL BOXSCORE') else '📊 EST'} ({status})"})
                else:
                    game_info.update({'is_active': True, 'winner': existing_row['Predicted_Winner'], 'odds': format_odds(existing_row['Odds']), 'conf': existing_row['Confidence'], 'status': f'✅ PRED ({status})'})
            else:
                eval_log_lines.append(f"  SKIPPED: Missing Pitcher IDs (H:{h_p_id} A:{a_p_id})\n" + "-"*50 + "\n")
                game_info['status'] = f'⏳ PITCHER ({status})'
        except Exception:
            eval_log_lines.append(f"  CRITICAL ERROR IN G{game_num}:\n{traceback.format_exc()}\n" + "-"*50 + "\n")
            continue
        display_list.append(game_info)

    if new_predictions: pd.DataFrame(new_predictions).to_csv(CSV_FILE, mode='a', index=False, header=not os.path.exists(CSV_FILE), quoting=csv.QUOTE_NONNUMERIC)
    if csv_updated: history_df.to_csv(CSV_FILE, index=False)
    with open(EVAL_LOG, 'w') as f: f.writelines(eval_log_lines)
    
    t_msg, y_msg, lifetime_val = audit_and_stats()
    msg = f"⚾ *MLB PRO REPORT: {today_str}*\n\n{t_msg}\n{y_msg}\n📈 *LIFETIME:* {lifetime_val}\n\n{get_cache_stats()}\n\n"
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
