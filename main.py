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
import re
from datetime import datetime, timedelta

# Force unbuffered output for GitHub logs
sys.stdout.reconfigure(line_buffering=True)

# --- CONFIGURATION ---
ODDS_CALL_LIMIT = 490        
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
    
    # League default acts as our ultimate safety net baseline
    league_default = 0.310 if p_hand == 'L' else 0.320
    
    # BAYESIAN PARAMETER: Number of imaginary PAs to blend into micro-samples
    # A weight of 10 perfectly balances early-series variance without drowning out historical dominance
    SMOOTHING_WEIGHT = 10.0

    for b_id in lineup_ids:
        b_name = name_map.get(b_id) or f"ID:{b_id}"
        cache_key = f"{pitcher_id}_{b_id}_v5"
        
        # 1. Fetch or load the Micro-Sample (BvP History)
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
            except: 
                h, bb, hbp, pa, ab = 0, 0, 0, 0, 0

        # 2. Establish the Macro Baseline (Player's Overall Season OBP)
        try:
            s_data = call_stats_api('person', {'personId': b_id, 'hydrate': 'stats(group=[hitting],type=[season],season=2026)'})
            season_obp = float(s_data['people'][0]['stats'][0]['splits'][0]['stat']['obp'])
            # If the player is a rookie who hasn't reached base yet (0.000), default to league average
            if season_obp <= 0.001:
                season_obp = league_default
                baseline_label = "Rookie (League Default)"
            else:
                baseline_label = "2026 Season OBP"
        except:
            season_obp = league_default
            baseline_label = "League Default"

        # 3. Apply Verbatim Bayesian Smoothing Formula
        actual_ob_events = h + bb + hbp
        
        # Calculate individual smoothed metric
        smoothed_player_obp = (actual_ob_events + (season_obp * SMOOTHING_WEIGHT)) / (pa + SMOOTHING_WEIGHT)
        
        # Accumulate toward team aggregates
        total_ob_events += smoothed_player_obp  # Summing individual smoothed expected value contributions
        total_pas += 1                          # Normalize per-hitter to keep team baseline scaled out of 1.000
        total_abs += ab

        # Append explicitly formatted line entries for your evaluation log file
        if pa > 0:
            details.append(f"    - {b_name}: {actual_ob_events}/{pa} BvP | Baseline ({baseline_label}): {season_obp:.3f} -> Smoothed OBP: {smoothed_player_obp:.3f} (AB: {ab})")
        else:
            details.append(f"    - {b_name}: NO HISTORY | Baseline ({baseline_label}): {season_obp:.3f} -> Smoothed OBP: {smoothed_player_obp:.3f}")

    if cache_updated: 
        save_bvp_cache(cache)
        
    # Calculate true normalized team aggregate capability
    smoothed_team_aggregate = total_ob_events / total_pas if total_pas > 0 else league_default
    return smoothed_team_aggregate, total_pas, details, total_abs

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
    
    # Calculate Best Picks Lifetime Record (Highest Confidence pick per completed historical date)
    past_completed_df = df[df['Result'].isin(['WIN', 'LOSS'])]
    bp_w, bp_l, bp_p = 0, 0, 0.0
    if not past_completed_df.empty:
        # Group by Date, and find the index of the maximum confidence pick for each day
        idx_best_picks = past_completed_df.groupby('Date')['Confidence'].idxmax()
        best_picks_df = past_completed_df.loc[idx_best_picks]
        bp_w = (best_picks_df['Result'] == 'WIN').sum()
        bp_l = (best_picks_df['Result'] == 'LOSS').sum()
        bp_p = best_picks_df['Profit'].sum()
    
    bp_total = bp_w + bp_l
    bp_pct = (bp_w / bp_total * 100) if bp_total > 0 else 0.0
    best_picks_line = f"{bp_w}/{bp_total} ({bp_pct:.1f}%) | {'+$' if bp_p>=0 else '-$'}{abs(bp_p):,.2f}"
    
    return get_stat_line(today_str, "TODAY"), get_stat_line(yesterday_str, "YESTERDAY"), lifetime, best_picks_line

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

def parse_existing_eval_log():
    """Reads evaluation_log.txt and extracts previously built game text blocks."""
    saved_blocks = {}
    if not os.path.exists(EVAL_LOG):
        return saved_blocks
    try:
        with open(EVAL_LOG, 'r') as f:
            content = f.read()
        # Find matches from 'GAME: ' down to the hyphens separator
        matches = re.findall(r"(GAME: .*?\n-{50}\n)", content, re.DOTALL)
        for block in matches:
            # Extract home team and game number using regular expressions
            match_title = re.search(r"GAME: .*? @ (.*?) \(G(\d+)\)", block)
            if match_title:
                h_name = match_title.group(1).strip()
                g_num = int(match_title.group(2))
                saved_blocks[(h_name, g_num)] = block
    except:
        pass
    return saved_blocks

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
    raw_games_list = [g for d in games_raw.get('dates', []) for g in d.get('games', [])]
    
    # Sort chronologically to ensure both Telegram and evaluation log output share the identical sequence
    games = sorted(raw_games_list, key=lambda x: x.get('gameDate', ''))
    
    live_odds, odds_used, odds_rem, _, local_tracker = get_mlb_odds()
    new_preds, display_list = [], []

    # Parse and load historical text blocks from current log execution space
    existing_blocks = parse_existing_eval_log()
    eval_log_lines = []
    
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

        # LOCKING ENHANCEMENT WITH ACCUMULATIVE PERSISTENCE
        if is_live_or_final and not saved_game.empty:
            winner = saved_game.iloc[0]['Predicted_Winner']
            conf = float(saved_game.iloc[0]['Confidence'])
            w_odds = saved_game.iloc[0]['Odds']
            
            game_info.update({'is_active': True, 'winner': winner, 'conf': conf, 'odds': format_odds(w_odds), 'src': "Locked Pregame Edge"})
            
            # Check if this specific matchup was already cached in the log file text from a previous pregame run
            if (home_name, game_num) in existing_blocks:
                eval_log_lines.append(existing_blocks[(home_name, game_num)])
            else:
                # Emergency fallback string if the action workflow runner checks a live game for the absolute first time
                eval_log_lines.append(f"GAME: {away_name} @ {home_name} (G{game_num})\n  Lineup Source: Locked Pregame Edge\n  PROJECTION (FROZEN): {winner} | {conf}% Edge\n" + "-" * 50 + "\n")
            
        elif h_p_id and a_p_id and detailed_status != 'Postponed':
            game_lbl = f"GAME: {away_name} @ {home_name} (G{game_num})\n"
            try:
                box = statsapi.boxscore_data(game_id)
                for side in ['home', 'away']:
                    for pid, p in box.get(side, {}).get('players', {}).items():
                        name_map[int(pid.replace('ID',''))] = p['person']['fullName']
                
                # Try getting official boxscore batting orders first
                h_l = box.get('home', {}).get('battingOrder', [])
                a_l = box.get('away', {}).get('battingOrder', [])
                
                # Determine source and fall back to historical starters if official order isn't live yet
                if h_l and a_l:
                    lineup_src = "Official Boxscore (Locked)" if is_live_or_final else "Official Boxscore"
                else:
                    lineup_src = "Starters of Last Game"
                    if not h_l: h_l, _ = get_pro_lineup(game['teams']['home']['team']['id'])
                    if not a_l: a_l, _ = get_pro_lineup(game['teams']['away']['team']['id'])

                # Emergency roster extraction if historical lookup returned empty lists
                if not h_l:
                    h_l = [p['id'] for p in box.get('home', {}).get('players', {}).values() if 'battingOrder' in p][:9]
                if not a_l:
                    a_l = [p['id'] for p in box.get('away', {}).get('players', {}).values() if 'battingOrder' in p][:9]

                if h_l and a_l and (h_p_id or a_p_id):
                    h_e, h_pa, h_det, h_ab = get_smoothed_bvp(a_p_id or 0, h_l, a_hand, name_map)
                    a_e, a_pa, a_det, a_ab = get_smoothed_bvp(h_p_id or 0, a_l, h_hand, name_map)
                    
                    winner = home_name if h_e > a_e else away_name
                    conf = round(abs(h_e - a_e) * 100, 2)
                    
                    if w_odds is None:
                        w_odds = live_odds.get(f"{home_name}_{winner}", -110)

                    # Build explicit log layout string formatting
                    game_block_txt = (
                        f"GAME: {away_name} @ {home_name} (G{game_num})\n"
                        f"  Lineup Source: {lineup_src}\n"
                        f"  {home_name} Hitting (vs {a_name}):\n"
                        + "".join([line + "\n" for line in h_det]) +
                        f"  Aggregated Home OBP: {h_e:.3f} (Total AB: {h_ab})\n"
                        f"  {away_name} Hitting (vs {h_name}):\n"
                        + "".join([line + "\n" for line in a_det]) +
                        f"  Aggregated Away OBP: {a_e:.3f} (Total AB: {a_ab})\n"
                        f"  PROJECTION: {winner} | {conf}% Edge\n"
                        + "-" * 50 + "\n"
                    )
                    eval_log_lines.append(game_block_txt)

                    if saved_game.empty:
                        new_preds.append({'Date': today_date_str, 'Matchup': matchup_txt, 'Predicted_Winner': winner, 'Odds': w_odds, 'Confidence': conf, 'Result': 'PENDING', 'Profit': 0.0, 'Game_Num': game_num})
                    else:
                        if not is_live_or_final:
                            history_df.loc[saved_game.index, 'Matchup'] = matchup_txt
                            history_df.loc[saved_game.index, 'Odds'] = w_odds
                    
                    game_info.update({'is_active': True, 'winner': winner, 'conf': conf, 'odds': format_odds(w_odds), 'src': lineup_src})
            except Exception as e:
                pass

        display_list.append(game_info)

    # --- SAVE UPDATED HISTORY WITHOUT WIPING ---
    if new_preds:
        history_df = pd.concat([history_df, pd.DataFrame(new_preds)], ignore_index=True)
    history_df.to_csv(CSV_FILE, index=False)

    # Extract historical performance metric counters safely from database ledger
    t_msg, y_msg, life, bp_life = audit_and_stats()
    
    # --- ASSEMBLE VERBATIM EVALUATION LOG HEADER BLOCK ---
    log_header = []
    header_time_str = now_mst.strftime("%m/%d/%Y (%I:%M %p MDT)")
    log_header.append(f"DETAILED EVALUATION LOG - {header_time_str}\n")
    log_header.append("=" * 50 + "\n")
    log_header.append(f"{t_msg}\n")
    log_header.append(f"{y_msg}\n")
    
    clean_life = life.replace("📈 *LIFETIME:*", "").strip() if "LIFETIME:" in life else life
    log_header.append(f"LIFETIME: {clean_life}\n")
    log_header.append(f"BEST PICKS LIFETIME: {bp_life}\n")
    log_header.append(f"ODDS-API: {local_tracker} Calls (Used: {odds_used} | Rem: {odds_rem})\n")
    log_header.append(f"MLB-STATS-API: {stats_api_calls} Total Calls\n")
    log_header.append("=" * 50 + "\n")
    
    # Overwrite the log file with 'w' mode passing the new metadata headers ahead of match strings
    with open(EVAL_LOG, 'w') as f: 
        f.writelines(log_header + eval_log_lines)

    # Transmit execution output if explicitly requested by wrapper logic
    if args.send_report:
        t_msg, y_msg, life, bp_life = audit_and_stats()
        report = f"⚾ *MLB REPORT: {full_timestamp_str}*\n\n{t_msg}\n{y_msg}\n📈 *LIFETIME:* {life}\n"
        report += f"⭐ *BEST PICKS LIFETIME:* {bp_life}\n"
        report += f"🔑 *ODDS-API:* {local_tracker} Calls (Used: {odds_used} | Rem: {odds_rem})\n"
        report += f"📊 *MLB-STATS-API:* {stats_api_calls} Calls this run\n\n"
        
        active_games = [g for g in display_list if g.get('is_active')]
        if active_games:
            best = max(active_games, key=lambda x: x['conf'])
            report += f"⭐ *BEST PICK:* {best['away_team']} @ {best['home_team']}\n"
            report += f"👉 PROJECTION: {best['winner']} ({best['odds']}) | {best['conf']}% Edge\n\n"
            
        # Kept perfectly sorted for Telegram presentation
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
