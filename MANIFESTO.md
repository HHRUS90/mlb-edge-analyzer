MLB Edge Analyzer: Project Manifesto
1. Core Data Integrity Protocols
Odds Source: All betting odds must be pulled exclusively from FanDuel.

BvP Data Scope: Batter vs. Pitcher calculations must include Regular Season, Postseason, and World Series statistics.

Predictive Model: Projections are based on smoothed On-Base Percentage (OBP) differentials between the current lineup and the probable pitcher.

2. Automation & Environment
Execution: The script is designed to run via GitHub Actions.

Notification: Real-time updates and summaries are delivered via Telegram.

Thresholds: Alerts for flight tracking (if integrated) or significant betting edges are set at a 30% threshold.

3. Telegram Message Structure (Immutable)
The reporting format is locked and must not be altered without explicit instruction. The hierarchy is as follows:

Header: Date and performance stats (Today, Yesterday, Lifetime).

API Diagnostics: Odds-API usage followed immediately by MLB-Stats-API call counts.

Best Pick: Highlights the matchup with the highest calculated edge for the day.

Game List:

Matchup: Away Team (Odds) @ Home Team (Odds)

Status/Score: Bolded (e.g., LIVE: 2 - 4 or FINAL: 5 - 3).

Projection: Predicted winner, odds, edge percentage, and lineup source (OFFICIAL/ESTIMATED).

4. Error Handling & Logic Gateways
Postponed Games: Games marked as "Postponed" in the MLB API must be bypassed in profit/win-loss calculations to ensure ROI accuracy.

API Resilience: Every call to the MLB Stats API must include sportId: 1 to prevent endpoint resolution errors.

Lineup Fallback: If official lineups are unavailable, the script must fall back to the most recent "Estimated" lineup from the previous game.
