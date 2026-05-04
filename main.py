import statsapi
import pandas as pd
import requests
import os
import csv
import sys
import pytz
import time
import json
from datetime import datetime, timedelta

# Force unbuffered output for real-time logging
sys.stdout.reconfigure(line_buffering=True)

# --- CONFIGURATION ---
ODDS_API_KEY = os.getenv('ODDS_API_KEY')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
UNIT_SIZE = 100               
ODDS_CALL_LIMIT = 450         
CSV_FILE = 'prediction_history.csv'
USAGE_FILE = 'api_usage.csv' 
EVAL_LOG = 'evaluation_log.txt'
BVP_CACHE_FILE = 'bvp_cache.json'
SEASON_STATS_CACHE = {} # In-memory cache to prevent redundant API calls in a single run

# --- GLOBAL COUNTER ---
stats_api_calls = 0

def call_stats_api(endpoint, params=None):
    global stats_api_calls
    stats_api_calls += 1
    return statsapi.get(endpoint, params or {})

def get_mst_now():
    tz = pytz.timezone('America/Denver')
    return datetime.now(tz)

# --- SEASON OBP LOOKUP (NEW LOGIC) ---
def get_season_obp(player_id, p_hand):
    """Fetches 2026 season OBP for a player to use as a personalized fallback."""
    if player_id in SEASON_STATS_CACHE:
        return SEASON_STATS_CACHE[player_id]
    
    # Base fallback if the player has no 2026 stats (e.g., a rookie)
    default_val = 0.310 if p_hand == 'L' else 0.320
    try:
        data = call_stats_api('person', {'personId': player_id, 'hydrate': 'stats(group=[hitting],type=[season],season=2026)'})
        stats = data.get('people', [{}])[0].get('stats', [])
        for s in stats:
            if s.get('type', {}).get('displayName') == 'season':
                obp = s.get('splits', [{}])[0].get('stat', {}).get('obp')
                if obp and obp != '.---':
                    val = float(obp)
                    SEASON_STATS_CACHE[player_id] = val
                    return val
    except: pass
    
    SEASON_STATS_CACHE[player_id] = default_val
    return default_val

# --- BULLPEN FATIGUE LOGIC ---
def get_key_relievers(team_id):
    key_ids = {}
    try:
        depth = call_stats_api('teams', {'teamId': team_id, 'hydrate': 'depthChart'})
        depth_data = depth.get('teams', [{}])[0].get('depthChart', [])
        for entry in depth_data:
            pos = entry.get('position', {}).get('abbreviation')
            if pos in ['CL', 'SU']:
                p_id = entry.get('player', {}).get('id')
                p_name = entry.get('player', {}).get('fullName')
                if p_id: key_ids[p_id] = f"{p_name} ({pos})"
    except: pass
    return key_ids

def check_bullpen_fatigue(team_id, team_name):
    key_arms = get_key_relievers(team_id)
    if not key_arms: return ""
    now = get_mst_now()
    fatigued_names = []
    lookback_days = [(now - timedelta(days=i)).strftime("%m/%d/%Y") for i in range(1, 4)]
    usage_data = {pid: {'pitches': 0, 'appearances': 0} for pid in key_arms}
    
    for date_str in lookback_days:
        try:
            games = call_stats_api('schedule', {'sportId': 1, 'date': date_str, 'teamId': team_id})
            for g in games.get('dates', [{}])[0].get('games', []):
                box = statsapi.boxscore_data(g['gamePk'])
                for side in ['home', 'away']:
                    for p_id_str in box[side]['pitchers']:
                        p_id = int(p_id_str.replace('ID',''))
                        if p_id in usage_data:
                            p_stats = box[side]['players'][p_id_str]['stats']['pitching']
                            usage_data[p_id]['pitches'] += p_stats.get('pitchesThrown', 0)
                            usage_data[p_id]['appearances'] += 1
        except: continue
    for pid, data in usage_data.items():
        if data['appearances'] >= 3 or data['pitches'] >= 50:
            fatigued_names.append(key_arms[pid].split(' (')[0])
    return f"⚠️ {team_name} Bullpen Fatigue: ({', '.join(fatigued_names)})" if fatigued_names else ""

# --- BVP LOGIC WITH SEASON OBP FALLBACK ---
def load_bvp_cache():
    if os.path.exists(BVP_CACHE_FILE):
        try:
            with open(BVP_CACHE_FILE, 'r') as f: return json.load(f)
        except: return {}
    return {}

def save_bvp_cache(cache_data):
    with open(BVP_CACHE_FILE, 'w') as f: json.dump(cache_data, f, indent=4)

def get_smoothed_bvp(pitcher_id, lineup_ids, p_hand, name_map):
    cache = load_bvp_cache()
    details = []
    total_ob_events, total_pas, total_abs = 0, 0, 0
    cache_updated = False

    for b_id in lineup_ids:
        b_name = name_map.get(b_id) or f"ID:{b_id}"
        cache_key = f"{pitcher_id}_{b_id}_v5"
        
        if cache_key in cache:
            s = cache[cache_key]
            h, bb, hbp, pa, ab = s['h'], s['bb'], s['hbp'], s['pa'], s.get('ab', 0)
        else:
            time.sleep(0.05) # Rate limit protection
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
            total_ob_events += (h + bb + hbp)
            total_pas += pa
            total_abs += ab
            player_obp = (h + bb + hbp) / pa
            details.append(f"    - {b_name}: {h+bb+hbp}/{pa} OBP: {player_obp:.3f} (AB: {ab})")
        else:
            # NO HISTORY FALLBACK
            fallback_obp = get_season_obp(b_id, p_hand)
            # Use 10 theoretical PAs for smoothing weight
            total_ob_events += (fallback_obp * 10)
            total_pas += 10
            details.append(f"    - {b_name}: NO BvP HISTORY (Defaulting to 2026 Season OBP: {fallback_obp:.3f})")

    if cache_updated: save_bvp_cache(cache)
    smoothed = total_ob_events / total_pas if total_pas > 0 else 0.315
    return smoothed, total_pas, details, total_abs

# --- ODDS TRACKING ---
def get_mlb_odds():
    now_mst = get_mst_now()
    current_month = now_mst.strftime("%Y-%m")
    if not os.path.exists(USAGE_FILE):
        with open(USAGE_FILE, 'w') as f: f.write("Month,Calls\n" + f"{current_month},0\n")
    usage_df = pd.read_csv(USAGE_FILE)
    if current_month not in usage_df['Month'].values:
        usage_df = pd.concat([usage_df, pd.DataFrame([{'Month': current_month, 'Calls': 0}])], ignore_index=True)
    
    local_calls = int(usage_df.loc[usage_df['Month'] == current_month, 'Calls'].values[0])
    if not ODDS_API_KEY or local_calls >= ODDS_CALL_LIMIT: 
        return {}, "N/A", "N/A", True, local_calls
    
    try:
        url = "https://api.the-odds-api.com/v4/sports/baseball_mlb/odds/"
        params = {'apiKey': ODDS_API_KEY, 'bookmakers': 'fanduel', 'markets': 'h2h', 'oddsFormat': 'american'}
        resp = requests.get(url, params=params)
        if resp.status_code == 200:
            usage_df.loc[usage_df['Month'] == current_month, 'Calls'] += 1
            usage_df.to_csv(USAGE_FILE, index=False)
            data = resp.json()
            used = resp.headers.get('x-requests-used', '0')
            rem = resp.headers.get('x-requests-remaining', '0')
            odds_dict = {f"{g['home_team']}_{o['name']}": o['price'] for g in data if g.get('bookmakers') for o in g['bookmakers'][0]['markets'][0]['outcomes']}
            return odds_dict, used, rem, False, local_calls + 1
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
            for g in dates[0].get('games', []):
                h_name = g.get('teams', {}).get('home', {}).get('team', {}).get('name', '')
                if h_name in row['Matchup'] and int(g.get('gameNumber', 1)) == int(row.get('Game_Num', 1)):
                    if g['status']['abstractGameState'] == 'Final':
                        winning_team = g['teams']['home']['team']['name'] if g['teams']['home'].get('isWinner') else g['teams']['away']['team']['name']
                        win = 'WIN' if row['Predicted_Winner'] == winning_team else 'LOSS'
                        try:
                            o = float(row['Odds'])
                            prof = (UNIT_SIZE * (o/100) if o > 0 else UNIT_SIZE/(abs(o)/100)) if win == 'WIN' else -UNIT_SIZE
                            df.at[idx, 'Result'], df.at[idx, 'Profit'] = win, prof
                            updated = True
                        except: pass
    if updated: df.to_csv(CSV_FILE, index=False)
    def get_stat_line(date_str, label):
        sub = df[df['Date'] == date_str]
        fin = sub[sub['Result'].isin(['WIN', 'LOSS'])]
        if fin.empty: return f"📊 {label}: 0/0 (0.0%) | $0.00"
        w, p = (fin['Result'] == 'WIN').sum(), fin['Profit'].sum()
        return f"📊 {label}: {w}/{len(fin)} ({w/len(fin)*100:.1f}%) | {'+$' if p>=0 else '-$'}{abs(p):,.2f}"
    total_fin = df[df['Result'].isin(['WIN', 'LOSS'])]
    l_w, l_p, l_count = (total_fin['Result'] == 'WIN').sum(), total_fin['Profit'].sum(), len(total_fin)
    return get_stat_line(today_str, "TODAY"), get_stat_line(yesterday_str, "YESTERDAY"), f"{l_w}/{l_count} ({l_w/l_count*100 if l_count>0 else 0:.1f}%) | {'+$' if l_p>=0 else '-$'}{abs(l_p):,.2f}"

# --- RUN ANALYSIS ---
def run_analysis():
    now_mst = get_mst_now()
    today_date_str = now_mst.strftime("%m/%d/%Y")
    games_raw = call_stats_api('schedule', {'sportId': 1, 'date': today_date_str, 'hydrate': 'probablePitcher,lineups'})
    games = [g for d in games_raw.get('dates', []) for g in d.get('games', [])]
    live_odds, odds_used, odds_rem, _, local_tracker = get_mlb_odds()
    new_preds, display_list, eval_log_lines = [], [], [f"DETAILED EVALUATION LOG - {today_date_str}\n" + "="*50 + "\n"]
    history_df = pd.read_csv(CSV_FILE) if os.path.exists(CSV_FILE) else pd.DataFrame()

    for game in games:
        game_id = game['gamePk']
        game_num = int(game.get('gameNumber', 1))
        # DOUBLEHEADER LABELING
        dh_label = f" (Game {game_num})" if game_num > 1 or game.get('doubleHeader') == 'Y' else ""
        
        h_p_id = game.get('teams',{}).get('home',{}).get('probablePitcher',{}).get('id')
        a_p_id = game.get('teams',{}).get('away',{}).get('probablePitcher',{}).get('id')
        
        mst_dt = datetime.strptime(game.get('gameDate'), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=pytz.UTC).astimezone(pytz.timezone('America/Denver'))
        mst_time = mst_dt.strftime("%I:%M %p")
        
        status, detailed_status = game.get('status',{}).get('abstractGameState','Pre-Game'), game.get('status',{}).get('detailedState','')
        away_name, home_name = game['teams']['away']['team']['name'], game['teams']['home']['team']['name']
        
        saved_game = history_df[(history_df['Date'] == today_date_str) & (history_df['Matchup'].str.contains(home_name)) & (history_df['Game_Num'].astype(int) == game_num)] if not history_df.empty else pd.DataFrame()
        is_started = status in ['Live', 'In Progress', 'Final']
        
        if is_started and not saved_game.empty:
            matchup_txt, w_odds = saved_game.iloc[0]['Matchup'], saved_game.iloc[0]['Odds']
        else:
            ao, ho = format_odds(live_odds.get(f"{home_name}_{away_name}", "N/A")), format_odds(live_odds.get(f"{home_name}_{home_name}", "N/A"))
            matchup_txt = f"{away_name} ({ao}) @ {home_name} ({ho}){dh_label}"
            w_odds = None
            if not saved_game.empty: history_df.loc[saved_game.index, 'Matchup'] = matchup_txt

        game_info = {'matchup': matchup_txt, 'time': mst_time, 'raw_time': mst_dt, 'is_active': False, 'status': detailed_status, 'score': f"🔥 LIVE: {game['teams']['away'].get('score',0)} - {game['teams']['home'].get('score',0)}" if is_started else "", 'away_team': away_name, 'home_team': home_name, 'game_num': game_num}
        
        if h_p_id and a_p_id and detailed_status != 'Postponed':
            try:
                box = statsapi.boxscore_data(game_id)
                name_map = {int(pid.replace('ID','')): p['person']['fullName'] for side in ['home','away'] for pid, p in box.get(side,{}).get('players',{}).items()}
                h_l, a_l = box.get('home',{}).get('battingOrder',[]), box.get('away',{}).get('battingOrder',[])
                src = "Official Boxscore" if h_l and a_l else "Starters of Last Game"
                if not h_l: h_l = statsapi.boxscore_data(statsapi.last_game(game['teams']['home']['team']['id'])).get('home' if statsapi.boxscore_data(statsapi.last_game(game['teams']['home']['team']['id']))['home']['team']['id'] == game['teams']['home']['team']['id'] else 'away',{}).get('battingOrder',[])
                if not a_l: a_l = statsapi.boxscore_data(statsapi.last_game(game['teams']['away']['team']['id'])).get('home' if statsapi.boxscore_data(statsapi.last_game(game['teams']['away']['team']['id']))['home']['team']['id'] == game['teams']['away']['team']['id'] else 'away',{}).get('battingOrder',[])

                if h_l and a_l:
                    h_e, _, h_det, h_ab = get_smoothed_bvp(a_p_id, h_l, 'R', name_map)
                    a_e, _, a_det, a_ab = get_smoothed_bvp(h_p_id, a_l, 'R', name_map)
                    winner, conf = (home_name, round(abs(h_e-a_e)*100, 2)) if h_e > a_e else (away_name, round(abs(h_e-a_e)*100, 2))
                    w_odds = w_odds if w_odds else live_odds.get(f"{home_name}_{winner}", -110)
                    
                    eval_log_lines.append(f"GAME: {away_name} @ {home_name}{dh_label}\n  Lineup Source: {src}\n  Home OBP: {h_e:.3f} (AB: {h_ab})\n  Away OBP: {a_e:.3f} (AB: {a_ab})\n  PROJECTION: {winner}\n" + "".join(h_det) + "".join(a_det) + "\n" + "-"*30 + "\n")
                    if status == 'Pre-Game' and saved_game.empty: new_preds.append({'Date': today_date_str, 'Matchup': matchup_txt, 'Predicted_Winner': winner, 'Odds': w_odds, 'Confidence': conf, 'Result': 'PENDING', 'Profit': 0.0, 'Game_Num': game_num})
                    game_info.update({'is_active': True, 'winner': winner, 'conf': conf, 'odds': format_odds(w_odds), 'src': src})
            except: pass
        display_list.append(game_info)

    if not history_df.empty: history_df.to_csv(CSV_FILE, index=False)
    if new_preds: pd.DataFrame(new_preds).to_csv(CSV_FILE, mode='a', index=False, header=not os.path.exists(CSV_FILE))
    with open(EVAL_LOG, 'w') as f: f.writelines(eval_log_lines)
    t, y, l = audit_and_stats()
    report = f"⚾ *MLB REPORT: {today_date_str}*\n\n{t}\n{y}\n📈 *LIFETIME:* {l}\n\n"
    for g in sorted(display_list, key=lambda x: (x['raw_time'] or datetime.max)):
        report += f"• [{g['time']}] {g['matchup']}\n"
        if g.get('is_active'): report += f"  👉 *{g['winner']}* ({g['odds']}) | {g['conf']}% Edge ({g['src']})\n\n"
    requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", data={'chat_id': TELEGRAM_CHAT_ID, 'text': report, 'parse_mode': 'Markdown'})

if __name__ == "__main__": run_analysis()
