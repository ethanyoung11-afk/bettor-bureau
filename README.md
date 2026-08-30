# Bettor Bureau

A local sportsbook market-intelligence product for comparing prices, finding opportunities, and
planning manual execution. It launches with a complete fictional demo market, so no API key is
required to use or evaluate the product.

## Open the product

```powershell
cd C:\Users\Admin\Documents\Betting
.\.venv\Scripts\streamlit.exe run app.py
```

Then open [http://localhost:8501](http://localhost:8501). Demo mode is selected automatically.

### Open it like a Windows app

Run `install_windows_shortcut.ps1` once. It creates a **Bettor Bureau** shortcut
in the Start menu and the current user's pinned-taskbar folder. The shortcut starts the local
server in the background (if necessary) and opens the terminal in your default browser without
showing a PowerShell window.

## User-facing workflows

- **Overview:** feed health, opportunity counts, leading signals, and the upcoming board.
- **Smart refresh plan:** schedules manual checks around useful pre-game windows.
- **Opportunities:** pure arbitrage with exact stakes, spread/total middles, consensus +EV, and
  best-line shopping.
- **Games:** one sportsbook matrix per game for moneylines, spreads, and totals.
- **Line Movement:** stored price and line histories across books and selections.
- **Bets & Watchlist:** event watchlist plus a local manual bet tracker and settlement workflow.
- **Settings:** persisted bankroll, freshness, ROI, edge, and data-source configuration.

Global filters cover league, market, sportsbook, quote age, minimum arb ROI, and odds format.
The intentionally stale demo book verifies that stale prices are excluded from opportunities.

## Live data

For the BC-first workflow, configure **The Odds API** with `ODDS_API_KEY` and use
`ODDS_API_REGIONS=ca,us,uk,eu` for broad consensus coverage. The Canadian region includes
PlayNow; UK coverage supplies Betway when that feed is available. Open **Owner controls** and
press **Refresh Odds**. Page loads, visitors, filters, sorting, and navigation never call the
provider. The initial refresh scope supports NFL, NCAAF, CFL, NBA, and NHL, including available
moneylines, spreads, totals, and supported player props. The owner panel records the provider's
reported credit usage, remaining allowance, and last-request cost.

The 500-credit free plan is suitable for a manual coverage trial, not continuous broad-region
refreshes. Request cost grows with the number of selected regions and markets. Start with manual
refreshes and confirm PlayNow, Betway, and comparison-book coverage before enabling a schedule.

Every managed refresh rechecks affected +EV recommendations. Price moves or removed markets
deactivate the recommendation without deleting its history. Failed requests preserve the last
good snapshot and allow freshness rules to mark old recommendations as stale. The owner-only
Odds Data panel reports API usage and the latest refresh diagnostics.

## Share a read-only hosted board

The app supports an owner/viewer split for hosting. Set these values in the host's encrypted
secrets panel (never commit the real values):

```toml
SHARED_APP = "true"
ADMIN_PASSWORD_HASH = "sha256-digest-of-the-owner-password"
ODDS_API_KEY = "your-server-side-feed-key"
ODDS_API_REGIONS = "ca,us,uk,eu"
DATABASE_URL = "your-postgres-connection-string"
```

Visitors get odds, filters, comparison tools, movement charts, and sportsbook links. Refresh,
feed credentials, saved settings, watchlists, and bet tracking remain hidden until the owner
unlocks the app. Without `DATABASE_URL`, the local app continues to use SQLite; hosted sharing
should use Postgres so every visitor sees the same durable snapshot.

The browser checks the shared database and redraws only when the owner has stored newer odds.
These viewer checks do not call the odds provider.

### Free beta deployment

1. Create a Neon Postgres database and copy its pooled connection string.
2. Push this repository to a private GitHub repository.
3. Add `DATABASE_URL` and `ODDS_API_KEY` as GitHub Actions repository secrets before enabling a
   scheduled The Odds API worker.
4. Deploy `app.py` on Streamlit Community Cloud from that repository.
5. Add `SHARED_APP`, `ADMIN_PASSWORD_HASH`, `ODDS_API_KEY`, `ODDS_API_REGIONS`, and
   `DATABASE_URL` in Streamlit's encrypted secrets panel.
6. Share the hosted app at `bettor-bureau.streamlit.app`.

The existing scheduled updater still targets OddsPapi. Keep it disabled during The Odds API free
trial; use owner-triggered refreshes until actual per-refresh credit cost and sportsbook coverage
have been verified. Then update the worker and schedule to fit the chosen paid allowance.

### Official paper strategy

Every successful manual or scheduled live-odds refresh evaluates the same fixed official strategy
and publishes every new qualifying paper bet. The offered sportsbook is excluded from its own
consensus. Bets need at least 2% EV, a 30% break-even probability, American odds from -200 to +300,
and at least three independent reference books. Quarter-Kelly sizing is capped at 1% per bet, then
scaled across the portfolio to cap exposure at 2% per event, 8% per league, and 20% per refresh.
The user-facing sportsbook selection only controls which recommendations that browser can see; it
never changes the global strategy or its paper-bet tracking.

The paper bankroll starts at 100 units, equivalent to $10,000 at $100 per unit. Stakes use
quarter-Kelly sizing, rounded to 0.01 units, with a 0.01-unit minimum and 1-unit per-bet maximum
before portfolio scaling. A pick is recorded once at its original sportsbook and price; later price
movement never rewrites history.
Run `python -m odds_scanner.strategy_review` to generate the weekly performance and strategy report.

When `ODDS_API_KEY` is present, the hosted app selects **The Odds API** automatically. The rest of
the product uses the same normalized domain objects and opportunity engines in either mode.

## Architecture

- `domain.py`: provider-neutral typed economic identities and user records.
- `normalization.py`: event, participant, market, line, and outcome normalization.
- `providers/`: provider protocol, deterministic demo feed, OddsPapi adapter, and The Odds API
  adapter.
- `opportunities.py`: freshness, deduplication, best prices, arbitrage, ROI, and stake sizing.
- `analytics.py`: middle detection and no-vig consensus value estimates.
- `storage/`: normalized SQLite/Postgres odds history, recommendation lifecycle, refresh locks,
  API usage, settings, watchlist, and manual bet persistence.
- `refresh.py`: shared manual/scheduled refresh orchestration, revalidation, freshness,
  adaptive-priority policy, and budget guardrails.
- `live_refresh.py`: host-independent central refresh worker used by GitHub Actions.
- `service.py`: lightweight analysis orchestration retained for non-refresh callers.
- `ui.py`: Streamlit presentation and user interactions only.

Spread contracts use the home participant's handicap as their canonical line. Home -3 and away
+3 therefore match; away +3.5 belongs to a different contract. Totals and props retain their
exact lines.

## Development checks

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy src
```

The terminal is for analysis and manual execution assistance. It does not access sportsbook
accounts or place bets. Demo prices are fictional.
