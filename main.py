import statsapi
import pandas as pd
import requests
import os
import csv
import sys
import pytz
import time
import json
import argparse
from datetime import datetime, timedelta

# Force unbuffered output for GitHub logs
sys.stdout.reconfigure(line_buffering=True)

# --- CONFIGURATION ---
ODDS_CALL_LIMIT = 450         
UNIT_SIZE = 100               
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
ODDS_API_KEY = os.getenv('ODDS_API_KEY')
CSV_FILE = 'prediction_history.csv'
USAGE_FILE = 'api_usage.csv' 
EVAL_LOG = 'evaluation_log.txt'
BVP_CACHE_FILE = 'bvp_cache.json'

# --- GLOBAL COUNTER ---
stats_api_calls = 0

def call_stats_api(endpoint, params=None):
    """Wrapper to track every single access to MLB-Stats-API."""
    global stats_api_calls
    stats_api_calls += 1
    return statsapi.get(endpoint, params or {})

def get_mst_now():
    """Returns current time in America/Denver."""
    tz = pytz.timezone('America/Denver')
    return datetime.now(tz)

# --- CACHE & BVP LOGIC ---

def load_bvp_cache():
    if os.path.exists(BVP_CACHE_FILE):
        try:
            with open(BVP_CACHE_FILE, 'r') as f:
                return json.load(f)
        except: return {}
    return {}

def save_bvp_cache(cache_data):
    with open(BVP_CACHE_FILE, 'w') as f:
        json.dump(cache_data, f, indent=4)

def get_smoothed_bvp(pitcher_id, lineup_ids, p_hand, name_map):
    cache = load_bvp_cache()
    details = []
    total_ob_events, total_pas, total_abs = 0, 0, 0
    cache_updated = False
    league_default = 0.310 if p_hand == 'L' else 0.320

    for b_id in lineup_ids:
        b_name = name_map.get(b_id) or f"ID:{b_id}"
        cache_key = f"{pitcher_id}_{b_id}_v5"
        
        if cache_key in cache:
            s = cache[cache_key]
            h, bb, hbp, pa, ab = s['h'], s['bb'], s['hbp'], s['pa'], s.get('ab', 0)
        else:
            time.sleep(0.1) 
            try:
                data = call_stats_api('people', {'personIds': b_id, 'hydrate': f'stats(group=[hitting],type=[vsPlayer],opposingPlayerId={pitcher_id},gameType=[R,P,W])'})
                h, bb, hbp, pa, ab = 0, 0, 0, 0, 0
                if 'people' in data and data['people']:
                    player_stats = data['people'][0].get('stats', [])
                    for stat_group in player_stats:
                        if stat_group.get('type', {}).get('displayName') == 'vsPlayerTotal':
                            for split in stat_group.get('splits', []):
                                st = split.get('stat', {})
                                h, bb, hbp, pa, ab = int(st.get('hits', 0)), int(st.get('baseOnBalls', 0)), int(st.get('hitByPitch', 0)), int(st.get('plateAppearances', 0)), int(st.get('atBats', 0))
                                break
                cache[cache_key] = {'h': h, 'bb': bb, 'hbp': hbp, 'pa': pa, 'ab': ab}
                cache_updated = True
            except: h, bb, hbp, pa, ab = 0, 0, 0, 0, 0

        if pa > 0:
            ob_events = h + bb + hbp
            total_ob_events += ob_events
            total_pas += pa
            total_abs += ab
            player_obp = ob_events / pa
            details.append(f"    - {b_name}: {ob_events}/{pa} OBP: {player_obp:.3f} (AB: {ab})")
        else:
            try:
                s_data = call_stats_api('person', {'personId': b_id, 'hydrate': 'stats(group=[hitting],type=[season],season=2026)'})
                season_obp = float(s_data['people'][0]['stats'][0]['splits'][0]['stat']['obp'])
                label = "2026 Season OBP"
            except:
                season_obp = league_default
                label = "Rookie (League Default)"
            
            total_ob_events += (season_obp * 10)
            total_pas += 10
            details.append(f"    - {b_name}: NO HISTORY ({label}: {season_obp:.3f})")

    if cache_updated: save_bvp_cache(cache)
    smoothed = total_ob_events / total_pas if total_pas > 0 else league_default
    return smoothed, total_pas, details, total_abs

# --- ODDS & AUDIT LOGIC ---

def get_mlb_odds():
    now_mst = get_mst_now()
    current_month = now_mst.strftime("%Y-%m")
    if not os.path.exists(USAGE_FILE):
        with open(USAGE_FILE, 'w') as f: f.write("Month,Calls\n" + f"{current_month},0\n")
    usage_df = pd.read_csv(USAGE_FILE)
    
    if current_month not in usage_df['Month'].values:
        usage_df = pd.concat([usage_df, pd.DataFrame([{'Month': current_month, 'Calls': 0}])], ignore_index=True)
    
    # Use the value DIRECTLY from the freshly read file
    local_calls = int(usage_df.loc[usage_df['Month'] == current_month, 'Calls'].values[0])
    if not ODDS_API_KEY or local_calls >= ODDS_CALL_LIMIT: 
        return {}, "N/A", "N/A", True, local_calls
    
    try:
        url = "https://api.the-odds-api.com/v4/sports/baseball_mlb/odds/"
        params = {'apiKey': ODDS_API_KEY, 'bookmakers': 'fanduel', 'markets': 'h2h', 'oddsFormat': 'american'}
        resp = requests.get(url, params=params)
        if resp.status_code == 200:
            # Increment the FRESH value and save immediately
            new_call_count = local_calls + 1
            usage_df.loc[usage_df['Month'] == current_month, 'Calls'] = new_call_count
            usage_df.to_csv(USAGE_FILE, index=False)
            data = resp.json()
            used = resp.headers.get('x-requests-used', '0')
            rem = resp.headers.get('x-requests-remaining', '0')
            odds_dict = {f"{g['home_team']}_{o['name']}": o['price'] for g in data if g.get('bookmakers') for o in g['bookmakers'][0]['markets'][0]['outcomes']}
            return odds_dict, used, rem, False, new_call_count
    except: pass
    return {}, "0", "0", False, local_calls

def format_odds(odds_val):
    try:
        if odds_val == "N/A" or odds_val is None: return "N/A"
        val = int(float(odds_val))
        return f"+{val}" if val > 0 else str(val)
    except: return str(odds_val)

def audit_and_stats():
    if not os.path.exists(CSV_FILE): 
        return "📊 TODAY: 0/0 (0.0%) | $0.00", "📊 YESTERDAY: 0/0 (0.0%) | $0.00", "0/0 (0.0%) | $0.00"
    
    df = pd.read_csv(CSV_FILE)
    now_mst = get_mst_now()
    today_str = now_mst.strftime("%m/%d/%Y")
    yesterday_str = (now_mst - timedelta(days=1)).strftime("%m/%d/%Y")
    
    updated = False
    for idx, row in df.iterrows():
        if str(row.get('Result')).upper() == 'PENDING':
            sched_data = call_stats_api('schedule', {'date': row['Date'], 'sportId': 1})
            dates = sched_data.get('dates', [])
            if not dates: continue
            
            games = dates[0].get('games', [])
            for g in games:
                h_name = g.get('teams', {}).get('home', {}).get('team', {}).get('name', '')
                if h_name in row['Matchup'] and int(g.get('gameNumber', 1)) == int(row.get('Game_Num', 1)):
                    if g['status']['abstractGameState'] == 'Final' and g['status']['detailedState'] != 'Postponed':
                        winning_team = g['teams']['home']['team']['name'] if g['teams']['home'].get('isWinner') else g['teams']['away']['team']['name']
                        win = 'WIN' if row['Predicted_Winner'] == winning_team else 'LOSS'
                        try:
                            o = float(row['Odds'])
                            prof = (UNIT_SIZE * (o/100) if o > 0 else UNIT_SIZE/(abs(o)/100)) if win == 'WIN' else -UNIT_SIZE
                            df.at[idx, 'Result'], df.at[idx, 'Profit'] = win, prof
                            updated = True
                        except: pass
                    elif g['status']['detailedState'] == 'Postponed':
                        df.at[idx, 'Result'], df.at[idx, 'Profit'] = 'PPD', 0.0
                        updated = True
    if updated: df.to_csv(CSV_FILE, index=False)
    
    def get_stat_line(date_str, label):
        sub = df[df['Date'] == date_str]
        fin = sub[sub['Result'].isin(['WIN', 'LOSS'])]
        if fin.empty: return f"📊 {label}: 0/0 (0.0%) | $0.00"
        w = (fin['Result'] == 'WIN').sum()
        p = fin['Profit'].sum()
        win_pct = (w / len(fin)) * 100
        return f"📊 {label}: {w}/{len(fin)} ({win_pct:.1f}%) | {'+$' if p>=0 else '-$'}{abs(p):,.2f}"

    total_fin = df[df['Result'].isin(['WIN', 'LOSS'])]
    l_w = (total_fin['Result'] == 'WIN').sum()
    l_p = total_fin['Profit'].sum()
    l_count = len(total_fin)
    l_pct = (l_w / l_count * 100) if l_count > 0 else 0.0
    lifetime = f"{l_w}/{l_count} ({l_pct:.1f}%) | {'+$' if l_p>=0 else '-$'}{abs(l_p):,.2f}"
    
    return get_stat_line(today_str, "TODAY"), get_stat_line(yesterday_str, "YESTERDAY"), lifetime

# --- UTILS ---

def get_player_info(pid):
    try:
        p = call_stats_api('person', {'personId': pid, 'hydrate': 'stats(group=[pitching],type=[season])'})
        hand = p['people'][0].get('pitchHand', {}).get('code', 'R')
        name = p['people'][0].get('fullName', f"ID:{pid}")
        era = "0.00"
        stats = p['people'][0].get('stats', [])
        for s in stats:
            if s.get('type', {}).get('displayName') == 'season':
                era = s.get('splits', [{}])[0].get('stat', {}).get('era', '0.00')
                break
        return hand, name, era
    except: return 'R', f"ID:{pid}", "0.00"

def get_pro_lineup(tid):
    try:
        lg = statsapi.last_game(tid)
        if lg:
            box = statsapi.boxscore_data(lg)
            key = 'home' if box['home']['team']['id'] == tid else 'away'
            return box[key].get('battingOrder', []), "Starters of Last Game"
    except: pass
    return [], "None Found"

def format_mst_time(utc):
    try:
        dt = datetime.strptime(utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=pytz.UTC).astimezone(pytz.timezone('America/Denver'))
        return dt, dt.strftime("%I:%M %p")
    except: return None, "TBD"

def send_telegram(msg):
    requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", data={'chat_id': TELEGRAM_CHAT_ID, 'text': msg, 'parse_mode': 'Markdown'})

# --- MAIN RUN ---

def run_analysis():
    # Set up argument parsing to check for reporting conditions
    parser = argparse.ArgumentParser()
    parser.add_argument('--send-report', action='store_true', help='Triggers Telegram output report generation')
    args = parser.parse_args()
    
    now_mst = get_mst_now()
    today_date_str = now_mst.strftime("%m/%d/%Y")
    full_timestamp_str = now_mst.strftime("%m/%d/%Y (%I:%M %p MDT)")
    
    # --- AUTO-REGENERATE CSV IF MISSING ---
    if not os.path.exists(CSV_FILE):
        headers = ['Date', 'Matchup', 'Predicted_Winner', 'Odds', 'Confidence', 'Result', 'Profit', 'Game_Num']
        with open(CSV_FILE, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(headers)
    
    games_raw = call_stats_api('schedule', {'sportId': 1, 'date': today_date_str, 'hydrate': 'probablePitcher,lineups'})
    games = [g for d in games_raw.get('dates', []) for g in d.get('games', [])]
    
    live_odds, odds_used, odds_rem, _, local_tracker = get_mlb_odds()
    t_msg, y_msg, life = audit_and_stats() # Get existing ledger stats
    new_preds, display_list = [], []

    # Persistent rolling log layout logic
    eval_log_contents = []
    if os.path.exists(EVAL_LOG):
        try:
            with open(EVAL_LOG, 'r') as f:
                existing_lines = f.readlines()
                if len(existing_lines) > 7:
                    # Keep all specific player diagnostic breakdowns from earlier in the day
                    eval_log_contents = existing_lines[8:]
        except:
            pass
            
    # Build header metadata template arrays dynamically 
    eval_log_headers = [
        f"DETAILED EVALUATION LOG - {full_timestamp_str}\n" + "="*50 + "\n",
        f"{t_msg}\n",
        f"{y_msg}\n",
        f"LIFETIME: {life}\n",
        "", # Index 4: Placeholder for Odds-API
        "", # Index 5: Placeholder for MLB-Stats-API
        "="*50 + "\n" # Index 6: The single static bottom separator
    ]
    
    # Load history for logic and merging
    history_df = pd.read_csv(CSV_FILE)

    for game in games:
        name_map = {}
        game_id = game['gamePk']
        h_p_id = game.get('teams', {}).get('home', {}).get('probablePitcher', {}).get('id')
        a_p_id = game.get('teams', {}).get('away', {}).get('probablePitcher', {}).get('id')
        game_num = int(game.get('gameNumber', 1))
        parent_match_time = game.get('gameDate')
        mst_dt, mst_time = format_mst_time(parent_match_time)
        
        status = game.get('status', {}).get('abstractGameState', 'Pre-Game')
        detailed_status = game.get('status', {}).get('detailedState', '')
        away_name = game['teams']['away']['team']['name']
        home_name = game['teams']['home']['team']['name']
        
        h_hand, h_name, h_era = get_player_info(h_p_id) if h_p_id else ('R', 'TBD', '0.00')
        a_hand, a_name, a_era = get_player_info(a_p_id) if a_p_id else ('R', 'TBD', '0.00')
        pitcher_header = f"_{a_name} ({a_era}) vs {h_name} ({h_era})_"

        # Persistence logic
        saved_game = history_df[(history_df['Date'] == today_date_str) & 
                                (history_df['Matchup'].str.contains(home_name)) & 
                                (history_df['Game_Num'].astype(int) == game_num)]

        is_live_or_final = status in ['Live', 'In Progress', 'Final'] or detailed_status == 'In Progress'
        
        if is_live_or_final and not saved_game.empty:
            matchup_txt = saved_game.iloc[0]['Matchup']
            w_odds = saved_game.iloc[0]['Odds']
        else:
            current_away_o = live_odds.get(f"{home_name}_{away_name}")
            current_home_o = live_odds.get(f"{home_name}_{home_name}")
            away_o_str = format_odds(current_away_o or "N/A")
            home_o_str = format_odds(current_home_o or "N/A")
            matchup_txt = f"{away_name} ({away_o_str}) @ {home_name} ({home_o_str})"
            w_odds = None

        score_str = ""
        if detailed_status == 'Postponed':
            score_str = f"❌ **POSTPONED**"
        elif status in ['Live', 'In Progress'] or detailed_status == 'In Progress':
            score_str = f"🔥 **LIVE: {game['teams']['away'].get('score', 0)} - {game['teams']['home'].get('score', 0)}**"
        elif status == 'Final':
            score_str = f"✅ **FINAL: {game['teams']['away'].get('score', 0)} - {game['teams']['home'].get('score', 0)}**"

        game_info = {
            'matchup': matchup_txt, 'time': mst_time, 'raw_time': mst_dt, 
            'is_active': False, 'status': detailed_status if detailed_status == 'Postponed' else status, 
            'score': score_str, 'away_team': away_name, 'home_team': home_name, 'game_num': game_num,
            'pitchers': pitcher_header
        }

        # LOCKING ENHANCEMENT: Skip analytical computations entirely if game is active/final and already logged.
        if is_live_or_final and not saved_game.empty:
            # Game is active/done; reload frozen records from CSV to ensure edge never shifts during live execution.
            winner = saved_game.iloc[0]['Predicted_Winner']
            conf = float(saved_game.iloc[0]['Confidence'])
            w_odds = saved_game.iloc[0]['Odds']
            game_info.update({'is_active': True, 'winner': winner, 'conf': conf, 'odds': format_odds(w_odds), 'src': "Locked Pregame Edge"})
            
            # OPTIMIZATION: Do absolutely nothing else. 
            # No boxscore calls, no lineup checking, no BvP evaluations.
            # Your rolling eval_log_contents [8:] logic already preserves the text from earlier runs.
            pass
            
        elif h_p_id and a_p_id and detailed_status != 'Postponed':
            try:
                box = statsapi.boxscore_data(game_id)
                for side in ['home', 'away']:
                    for pid, p in box.get(side, {}).get('players', {}).items():
                        name_map[int(pid.replace('ID',''))] = p['person']['fullName']
                
                h_l, a_l = box.get('home',{}).get('battingOrder',[]), box.get('away',{}).get('battingOrder',[])
                lineup_src = "Official Boxscore" if (h_l and a_l) else "Starters of Last Game"
                
                if not h_l: h_l, _ = get_pro_lineup(game['teams']['home']['team']['id'])
                if not a_l: a_l, _ = get_pro_lineup(game['teams']['away']['team']['id'])

                if h_l and a_l:
                    h_e, h_pa, h_det, h_ab = get_smoothed_bvp(a_p_id, h_l, a_hand, name_map)
                    a_e, a_pa, a_det, a_ab = get_smoothed_bvp(h_p_id, a_l, h_hand, name_map)
                    
                    winner = home_name if h_e > a_e else away_name
                    conf = round(abs(h_e - a_e) * 100, 2)
                    
                    if w_odds is None:
                        w_odds = live_odds.get(f"{home_name}_{winner}", -110)

                    # Only append new analysis to logging contents if it hasn't been added yet
                    game_lbl = f"GAME: {away_name} @ {home_name} (G{game_num})\n"
                    if not any(game_lbl in str(line) for line in eval_log_contents):
                        eval_log_contents.append(game_lbl)
                        eval_log_contents.append(f"  Lineup Source: {lineup_src}\n")
                        eval_log_contents.append(f"  {home_name} Hitting (vs {a_name}):\n")
                        eval_log_contents.extend([line + "\n" for line in h_det])
                        eval_log_contents.append(f"  Aggregated Home OBP: {h_e:.3f} (Total AB: {h_ab})\n\n")
                        eval_log_contents.append(f"  {away_name} Hitting (vs {h_name}):\n")
                        eval_log_contents.extend([line + "\n" for line in a_det])
                        eval_log_contents.append(f"  Aggregated Away OBP: {a_e:.3f} (Total AB: {a_ab})\n")
                        eval_log_contents.append(f"  PROJECTION: {winner} | {conf}% Edge\n")
                        eval_log_contents.append("-" * 50 + "\n")

                    if saved_game.empty:
                        new_preds.append({'Date': today_date_str, 'Matchup': matchup_txt, 'Predicted_Winner': winner, 'Odds': w_odds, 'Confidence': conf, 'Result': 'PENDING', 'Profit': 0.0, 'Game_Num': game_num})
                    else:
                        if not is_live_or_final:
                            history_df.loc[saved_game.index, 'Matchup'] = matchup_txt
                            history_df.loc[saved_game.index, 'Odds'] = w_odds
                    
                    game_info.update({'is_active': True, 'winner': winner, 'conf': conf, 'odds': format_odds(w_odds), 'src': lineup_src})
            except: pass
        display_list.append(game_info)

    # --- SAVE UPDATED HISTORY WITHOUT WIPING ---
    if new_preds:
        history_df = pd.concat([history_df, pd.DataFrame(new_preds)], ignore_index=True)
    history_df.to_csv(CSV_FILE, index=False)

    # Inject calculations into correct log array positions
    eval_log_headers[4] = f"ODDS-API: {local_tracker} Calls (Used: {odds_used} | Rem: {odds_rem})\n"
    eval_log_headers[5] = f"MLB-STATS-API: {stats_api_calls} Total Calls\n"
    
    # Merge daily audit stats headers smoothly with rolling list components
    complete_log_output = eval_log_headers + eval_log_contents
    with open(EVAL_LOG, 'w') as f: 
        f.writelines(complete_log_output)

    # Transmit execution output if explicitly requested by wrapper logic
    if args.send_report:
        t_msg, y_msg, life = audit_and_stats()
        report = f"⚾ *MLB REPORT: {full_timestamp_str}*\n\n{t_msg}\n{y_msg}\n📈 *LIFETIME:* {life}\n"
        report += f"🔑 *ODDS-API:* {local_tracker} Calls (Used: {odds_used} | Rem: {odds_rem})\n"
        report += f"📊 *MLB-STATS-API:* {stats_api_calls} Calls this run\n\n"
        
        active_games = [g for g in display_list if g.get('is_active')]
        if active_games:
            best = max(active_games, key=lambda x: x['conf'])
            report += f"⭐ *BEST PICK:* {best['away_team']} @ {best['home_team']}\n"
            report += f"👉 PROJECTION: {best['winner']} ({best['odds']}) | {best['conf']}% Edge\n\n"
    
        for g in sorted(display_list, key=lambda x: (x['raw_time'] or datetime.max)):
            report += f"• [{g['time']}] {g['matchup']}\n"
            report += f"  {g['pitchers']}\n"
            if g['score']:
                report += f"  {g['score']}\n"
            if g.get('is_active'):
                report += f"  👉 *{g['winner']}* ({g['odds']}) | {g['conf']}% Edge ({g['src']})\n\n"
            else:
                report += f"  ⏳ {g['status']}\n\n"
        
        send_telegram(report)

if __name__ == "__main__": run_analysis()
