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

def get_mst_now():
    tz = pytz.timezone('America/Denver')
    return datetime.now(tz)

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
    total_ob_events = 0
    total_pas = 0
    cache_updated = False
    default_obp = 0.310 if p_hand == 'L' else 0.320

    for b_id in lineup_ids:
        b_name = name_map.get(b_id) or f"ID:{b_id}"
        cache_key = f"{pitcher_id}_{b_id}"
        
        # Only use cache if it has actual data (greater than 0 PAs)
        # This prevents the "All Zeros" bug from earlier today from sticking around
        if cache_key in cache and cache[cache_key].get('pa', 0) > 0:
            s = cache[cache_key]
            h, bb, hbp, pa = s['h'], s['bb'], s['hbp'], s['pa']
        else:
            time.sleep(0.15) 
            try:
                params = {
                    'personIds': int(b_id),
                    'hydrate': f'stats(group=[hitting],type=[vsPlayer],opposingPlayerId={pitcher_id})'
                }
                data = statsapi.get('people', params)
                
                h, bb, hbp, pa = 0, 0, 0, 0
                if 'people' in data and data['people']:
                    player_stats = data['people'][0].get('stats', [])
                    for stat_group in player_stats:
                        # Target the Career Total block we identified in the debug log
                        if stat_group.get('type', {}).get('displayName') == 'vsPlayerTotal':
                            for split in stat_group.get('splits', []):
                                st = split.get('stat', {})
                                h = int(st.get('hits', 0))
                                bb = int(st.get('baseOnBalls', 0))
                                hbp = int(st.get('hitByPitch', 0))
                                pa = int(st.get('plateAppearances', 0))
                                break

                cache[cache_key] = {'h': h, 'bb': bb, 'hbp': hbp, 'pa': pa}
                cache_updated = True
            except:
                h, bb, hbp, pa = 0, 0, 0, 0

        ob_events = h + bb + hbp
        if pa > 0:
            total_ob_events += ob_events
            total_pas += pa
            details.append(f"    - {b_name}: {ob_events}/{pa} ({(ob_events/pa):.3f})")
        else:
            details.append(f"    - {b_name}: NO HISTORY (Defaulting {default_obp})")

    if cache_updated:
        save_bvp_cache(cache)

    smoothed = (total_ob_events + (default_obp * 10)) / (total_pas + 10)
    return smoothed, total_pas, details

def get_mlb_odds():
    now_mst = get_mst_now()
    current_month = now_mst.strftime("%Y-%m")
    if not os.path.exists(USAGE_FILE):
        with open(USAGE_FILE, 'w') as f: f.write("Month,Calls\n" + f"{current_month},0\n")
    usage_df = pd.read_csv(USAGE_FILE)
    if current_month not in usage_df['Month'].values:
        usage_df = pd.concat([usage_df, pd.DataFrame([{'Month': current_month, 'Calls': 0}])], ignore_index=True)
    
    local_calls = int(usage_df.loc[usage_df['Month'] == current_month, 'Calls'].values[0])
    if not ODDS_API_KEY or local_calls >= ODDS_CALL_LIMIT: return {}, "N/A", "N/A", True, local_calls
    
    try:
        url = "https://api.the-odds-api.com/v4/sports/baseball_mlb/odds/"
        params = {'apiKey': ODDS_API_KEY, 'bookmakers': 'fanduel', 'markets': 'h2h', 'oddsFormat': 'american'}
        resp = requests.get(url, params=params)
        if resp.status_code == 200:
            usage_df.loc[usage_df['Month'] == current_month, 'Calls'] += 1
            usage_df.to_csv(USAGE_FILE, index=False)
            data = resp.json()
            odds_dict = {f"{g['home_team']}_{o['name']}": o['price'] for g in data if g.get('bookmakers') for o in g['bookmakers'][0]['markets'][0]['outcomes']}
            return odds_dict, resp.headers.get('x-requests-used', '0'), resp.headers.get('x-requests-remaining', '0'), False, local_calls + 1
    except: pass
    return {}, "0", "0", False, local_calls

def format_odds(odds_val):
    try:
        if odds_val == "N/A" or odds_val is None: return "N/A"
        val = int(float(odds_val))
        return f"+{val}" if val > 0 else str(val)
    except: return str(odds_val)

def audit_and_stats():
    if not os.path.exists(CSV_FILE): return "N/A", "N/A", "0/0 | $0.00"
    df = pd.read_csv(CSV_FILE)
    now_mst = get_mst_now()
    today_str, yesterday_str = now_mst.strftime("%m/%d/%Y"), (now_mst - timedelta(days=1)).strftime("%m/%d/%Y")
    
    updated = False
    for idx, row in df.iterrows():
        if str(row.get('Result')).upper() == 'PENDING':
            games = statsapi.schedule(date=row['Date'])
            for g in games:
                if g['home_name'] in row['Matchup'] and g['status'] == 'Final':
                    win = 'WIN' if row['Predicted_Winner'] == g['winning_team'] else 'LOSS'
                    try:
                        o = float(row['Odds'])
                        prof = (UNIT_SIZE * (o/100) if o > 0 else UNIT_SIZE/(abs(o)/100)) if win == 'WIN' else -UNIT_SIZE
                        df.at[idx, 'Result'], df.at[idx, 'Profit'] = win, prof
                        updated = True
                    except: pass
    if updated: df.to_csv(CSV_FILE, index=False)
    
    def line(d, label):
        sub = df[df['Date'] == d]
        fin = sub[sub['Result'].isin(['WIN', 'LOSS'])]
        if fin.empty: return f"📊 *{label}:* N/A"
        w = (fin['Result'] == 'WIN').sum()
        p = fin['Profit'].sum()
        return f"📊 *{label}:* {w}/{len(fin)} ({w/len(fin)*100:.1f}%) | {'+$' if p>=0 else '-$'}{abs(p):,.2f}"

    total_fin = df[df['Result'].isin(['WIN', 'LOSS'])]
    l_w = (total_fin['Result'] == 'WIN').sum()
    l_p = total_fin['Profit'].sum()
    lifetime = f"{l_w}/{len(total_fin)} ({l_w/len(total_fin)*100 if len(total_fin)>0 else 0:.1f}%) | {'+$' if l_p>=0 else '-$'}{abs(l_p):,.2f}"
    return line(today_str, "TODAY"), line(yesterday_str, "YESTERDAY"), lifetime

def get_player_info(pid):
    try:
        p = statsapi.get('person', {'personId': pid})
        return p['people'][0].get('pitchHand', {}).get('code', 'R'), p['people'][0].get('fullName', f"ID:{pid}")
    except: return 'R', f"ID:{pid}"

def get_pro_lineup(tid):
    try:
        lg = statsapi.last_game(tid)
        if lg:
            box = statsapi.boxscore_data(lg)
            key = 'home' if box['home']['team']['id'] == tid else 'away'
            return box[key].get('battingOrder', [])
    except: pass
    return []

def format_mst_time(utc):
    try:
        dt = datetime.strptime(utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=pytz.UTC).astimezone(pytz.timezone('America/Denver'))
        return dt, dt.strftime("%I:%M %p")
    except: return None, "TBD"

def send_telegram(msg):
    requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", data={'chat_id': TELEGRAM_CHAT_ID, 'text': msg, 'parse_mode': 'Markdown'})

def run_analysis():
    now_mst = get_mst_now()
    today_str = now_mst.strftime("%m/%d/%Y")
    games = statsapi.schedule(date=today_str)
    live_odds, _, _, _, _ = get_mlb_odds()
    new_preds, display_list = [], []
    eval_log_lines = [f"DETAILED EVALUATION LOG - {today_str}\n" + "="*50 + "\n"]
    history_df = pd.read_csv(CSV_FILE) if os.path.exists(CSV_FILE) else pd.DataFrame()

    raw_sched = statsapi.get('schedule', {'sportId': 1, 'date': today_str, 'hydrate': 'probablePitcher'})
    raw_map = {rg['gamePk']: rg for d in raw_sched.get('dates', []) for rg in d.get('games', [])}

    for game in games:
        name_map = {}
        rg = raw_map.get(game['game_id'], {})
        h_p_id = rg.get('teams', {}).get('home', {}).get('probablePitcher', {}).get('id')
        a_p_id = rg.get('teams', {}).get('away', {}).get('probablePitcher', {}).get('id')
        game_num = int(game.get('game_num', 1))
        
        mst_dt, mst_time = format_mst_time(game.get('game_datetime'))
        away_o = format_odds(live_odds.get(f"{game['home_name']}_{game['away_name']}", "N/A"))
        home_o = format_odds(live_odds.get(f"{game['home_name']}_{game['home_name']}", "N/A"))
        matchup_txt = f"{game['away_name']} ({away_o}) @ {game['home_name']} ({home_o})"
        game_info = {'matchup': matchup_txt, 'pitchers': f"({game.get('away_probable_pitcher','TBD')} vs {game.get('home_probable_pitcher','TBD')})", 'time': mst_time, 'raw_time': mst_dt, 'is_active': False, 'status': game['status']}

        if h_p_id and a_p_id:
            try:
                box = statsapi.boxscore_data(game['game_id'])
                for t in ['home', 'away']:
                    for pid, p in box.get(t, {}).get('players', {}).items():
                        name_map[int(pid.replace('ID',''))] = p['person']['fullName']
                
                h_l, a_l = box.get('home',{}).get('battingOrder',[]), box.get('away',{}).get('battingOrder',[])
                src = "OFFICIAL BOXSCORE" if (h_l and a_l) else "ESTIMATED LAST GAME"
                if not h_l: h_l = get_pro_lineup(game['home_id'])
                if not a_l: a_l = get_pro_lineup(game['away_id'])

                if h_l and a_l:
                    h_h, h_n = get_player_info(h_p_id)
                    a_h, a_n = get_player_info(a_p_id)
                    
                    h_e, _, h_det = get_smoothed_bvp(a_p_id, h_l, a_h, name_map)
                    a_e, _, a_det = get_smoothed_bvp(h_p_id, a_l, h_h, name_map)
                    
                    winner = game['home_name'] if h_e > a_e else game['away_name']
                    conf = round(abs(h_e - a_e) * 100, 2)
                    
                    eval_log_lines.append(f"GAME: {game['away_name']} @ {game['home_name']} (G{game_num})\n")
                    eval_log_lines.append(f"  Source: {src}\n")
                    eval_log_lines.append(f"  [OFFENSE: {game['home_name']} vs {a_n}]\n")
                    eval_log_lines.extend([d + "\n" for d in h_det])
                    eval_log_lines.append(f"  >> Aggregated Home OBP: {h_e:.3f}\n\n")

                    eval_log_lines.append(f"  [OFFENSE: {game['away_name']} vs {h_n}]\n")
                    eval_log_lines.extend([d + "\n" for d in a_det])
                    eval_log_lines.append(f"  >> Aggregated Away OBP: {a_e:.3f}\n")
                    eval_log_lines.append("-" * 50 + "\n")

                    exists = not history_df.empty and not history_df[(history_df['Date'] == today_str) & (history_df['Matchup'].str.contains(game['home_name'])) & (history_df['Game_Num'].astype(int) == game_num)].empty
                    if not exists:
                        w_odds = live_odds.get(f"{game['home_name']}_{winner}", -110)
                        new_preds.append({'Date': today_str, 'Matchup': matchup_txt, 'Predicted_Winner': winner, 'Odds': w_odds, 'Confidence': conf, 'Result': 'PENDING', 'Profit': 0.0, 'Game_Num': game_num})
                    
                    game_info.update({'is_active': True, 'winner': winner, 'conf': conf, 'odds': format_odds(live_odds.get(f"{game['home_name']}_{winner}", "N/A"))})
            except: pass
        display_list.append(game_info)

    if new_preds: pd.DataFrame(new_preds).to_csv(CSV_FILE, mode='a', index=False, header=not os.path.exists(CSV_FILE))
    with open(EVAL_LOG, 'w') as f: f.writelines(eval_log_lines)
    
    t_msg, y_msg, life = audit_and_stats()
    report = f"⚾ *MLB REPORT: {today_str}*\n\n{t_msg}\n{y_msg}\n📈 *LIFETIME:* {life}\n\n"
    for g in sorted(display_list, key=lambda x: x['raw_time'] or datetime.max):
        if g['is_active']:
            report += f"• [{g['time']}] {g['matchup']}\n  👉 {g['winner']} | {g['conf']}% Edge\n\n"
        else:
            report += f"• [{g['time']}] {g['matchup']}\n  ⏳ {g['status']}\n\n"
    send_telegram(report)

if __name__ == "__main__": run_analysis()
