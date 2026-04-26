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

def get_mst_now():
    return datetime.utcnow() - timedelta(hours=7)

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
    if not ODDS_API_KEY or local_calls >= 450:
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
                matchup_str = f"{g['away_name']} @ {g['home_name']}"
                if matchup_str == row['Matchup'] and g['status'] == 'Final':
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
        profit_str = f"{'+$' if profit >= 0 else '-$'}{abs(profit):,.2f}"
        return f"📊 *{label}:* {wins}/{total} ({acc:.1f}%) | {profit_str}"
    finalized = df[df['Result'].isin(['WIN', 'LOSS'])]
    today_results = finalized[finalized['Date'] == today_str]
    yesterday_results = finalized[finalized['Date'] == yesterday_str]
    t_msg = get_line_stats(today_results, "TODAY")
    y_msg = get_line_stats(yesterday_results, "YESTERDAY")
    if finalized.empty: l_msg = "📈 *LIFETIME:* N/A"
    else:
        df['Date_DT'] = pd.to_datetime(df['Date'], format='%m/%d/%Y')
        start_date = df['Date_DT'].min().strftime("%m/%d/%Y")
        l_acc = ((finalized['Result'] == 'WIN').sum() / len(finalized)) * 100
        l_total_profit = finalized['Profit'].sum()
        l_profit_str = f"{'+$' if l_total_profit >= 0 else '-$'}{abs(l_total_profit):,.2f}"
        l_msg = f"📈 *LIFETIME (Since {start_date}):* {l_acc:.1f}% Accuracy | *{l_profit_str}*"
    return t_msg, y_msg, l_msg

def get_player_info(player_id):
    """Fetches throwing hand and other info for a player."""
    try:
        p = statsapi.get('person', {'personId': player_id})
        hand = p['people'][0].get('pitchHand', {}).get('code', 'R')
        return hand
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
        
        # Fallback OBP based on league-wide Platoon splits
        # Same-side (L vs L / R vs R) is generally harder for the batter
        # We assume hitters generally face Righties, so L vs L is the biggest penalty
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
        mst_time = utc_time - timedelta(hours=7)
        return mst_time, mst_time.strftime("%I:%M %p")
    except: return None, "TBD"

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, data={'chat_id': TELEGRAM_CHAT_ID, 'text': message, 'parse_mode': 'Markdown'})

def run_analysis():
    now_mst = get_mst_now()
    today_str = now_mst.strftime("%m/%d/%Y")
    games = statsapi.schedule(date=today_str)
    live_odds, api_used, api_remaining, hard_stop, local_calls = get_mlb_odds()
    sorted_games = sorted(games, key=lambda x: x.get('game_datetime', ''))
    new_predictions, display_list = [], []
    history_df = pd.read_csv(CSV_FILE) if os.path.exists(CSV_FILE) else pd.DataFrame()

    for game in sorted_games:
        status = game.get('status', 'Scheduled').upper()
        matchup = f"{game['away_name']} @ {game['home_name']}"
        mst_dt, mst_time_str = format_mst_time(game.get('game_datetime'))
        game_info = {'matchup': matchup, 'time': mst_time_str, 'status': status, 'is_active': False}
        if any(x in status for x in ['POSTPONED', 'CANCELLED', 'DELAYED']):
            game_info['status'] = f"🛑 {status}"; display_list.append(game_info); continue
        if not history_df.empty and not history_df[(history_df['Date'] == today_str) & (history_df['Matchup'] == matchup)].empty:
            existing = history_df[(history_df['Date'] == today_str) & (history_df['Matchup'] == matchup)].iloc[0]
            game_info.update({'is_active': True, 'winner': existing['Predicted_Winner'], 'odds': existing['Odds'], 'conf': existing['Confidence']})
            display_list.append(game_info); continue
        try:
            box = statsapi.boxscore_data(game['game_id'])
            h_l, a_l = box.get('home', {}).get('battingOrder', []), box.get('away', {}).get('battingOrder', [])
            if not h_l or not a_l:
                time_diff = mst_dt - now_mst if mst_dt else timedelta(hours=5)
                game_info['status'] = '📅 SCHEDULED' if time_diff.total_seconds() > 14400 else '⏳ LINEUPS PENDING'
                display_list.append(game_info); continue
            h_p_id = game.get('home_probable_pitcher_id')
            a_p_id = game.get('away_probable_pitcher_id')
            if not h_p_id or not a_p_id:
                game_info['status'] = '🧢 PITCHERS PENDING'; display_list.append(game_info); continue

            # Get Pitcher Hands
            h_p_hand = get_player_info(h_p_id)
            a_p_hand = get_player_info(a_p_id)

            h_e = get_smoothed_bvp(a_p_id, h_l, a_p_hand)
            a_e = get_smoothed_bvp(h_p_id, a_l, h_p_hand)
            winner = game['home_name'] if h_e > a_e else game['away_name']
            conf = round(abs(h_e - a_e) * 100, 1)
            f_odds = format_odds(live_odds.get(f"{game['home_name']}_{winner}", -110))
            new_predictions.append({'Date': today_str, 'Matchup': matchup, 'Predicted_Winner': winner, 'Odds': f_odds, 'Confidence': conf, 'Result': 'PENDING', 'Profit': 0.0})
            game_info.update({'is_active': True, 'winner': winner, 'odds': f_odds, 'conf': conf})
            display_list.append(game_info)
        except: continue
    if new_predictions: pd.DataFrame(new_predictions).to_csv(CSV_FILE, mode='a', index=False, header=not os.path.exists(CSV_FILE), quoting=csv.QUOTE_NONNUMERIC)
    t_msg, y_msg, l_msg = audit_and_stats()
    usage_msg = f"💳 *API USAGE*\n• Local Ticker: {local_calls}\n• API Reported: {api_used} Used | {api_remaining} Left"
    msg = f"⚾ *MLB QUANT REPORT: {today_str}*\n\n{t_msg}\n{y_msg}\n{l_msg}\n\n{usage_msg}\n\n"
    active_preds = [g for g in display_list if g.get('is_active')]
    if active_preds:
        best = max(active_preds, key=lambda x: x['conf'])
        msg += f"🔥 *BEST BET:* {best['matchup']}\n👉 {best['winner']} ({best['odds']}) — {best['conf']}% Edge\n\n"
    msg += "*DAILY SCHEDULE (MST):*\n"
    for g in display_list:
        if g.get('is_active'):
            star = " 🌟" if active_preds and g['matchup'] == best['matchup'] else ""
            msg += f"• [{g['time']}] {g['matchup']}{star}\n  👉 Pick: {g['winner']} ({g['odds']}) — {g['conf']}% Edge\n\n"
        else: msg += f"• [{g['time']}] {g['matchup']}\n  {g['status']}\n\n"
    send_telegram(msg)

if __name__ == "__main__":
    run_analysis()
