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

For the BC-first free workflow, select **OddsPapi Free**, open **Odds Data**, choose the sports
and markets you want, and press **Refresh Odds**. Page loads, visitors, filters, sorting, and
navigation never call the provider. The initial refresh scope supports NFL, NCAAF, CFL, NBA,
and NHL, including available moneylines, spreads, totals, and player props. Starter accounts
require one odds request per sportsbook, so a full ten-book update normally costs about ten
requests. The first refresh also caches the provider's league and market catalogs, so it can use
a few additional requests.

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
ODDSPAPI_API_KEY = "your-server-side-feed-key"
DATABASE_URL = "your-postgres-connection-string"
```

Visitors get odds, filters, comparison tools, movement charts, and sportsbook links. Refresh,
feed credentials, saved settings, watchlists, and bet tracking remain hidden until the owner
unlocks the app. Without `DATABASE_URL`, the local app continues to use SQLite; hosted sharing
should use Postgres so every visitor sees the same durable snapshot.

The browser checks the shared database once a minute and redraws only when the central worker has
stored newer odds. These checks do not call OddsPapi. The included GitHub workflow runs the
central refresh four times per week and can also be started manually. It protects a configurable
monthly reserve before making provider calls.

### Free beta deployment

1. Create a Neon Postgres database and copy its pooled connection string.
2. Push this repository to a private GitHub repository.
3. Add `DATABASE_URL` and `ODDSPAPI_API_KEY` as GitHub Actions repository secrets.
4. Deploy `app.py` on Streamlit Community Cloud from that repository.
5. Add `SHARED_APP`, `ADMIN_PASSWORD_HASH`, `ODDSPAPI_API_KEY`, and `DATABASE_URL` in
   Streamlit's encrypted secrets panel.
6. Share the hosted app at `bettor-bureau.streamlit.app`.

The scheduled updater is in `.github/workflows/refresh-odds.yml`. Its free-plan defaults are four
full refreshes per week, a 250-call monthly limit, and a 25-call owner reserve. Manual owner
refreshes share the same allowance. Increase the schedule only after the provider plan is upgraded.

### Official paper strategy

Every successful manual or scheduled live-odds refresh evaluates the same fixed official strategy
and publishes up to three qualifying paper bets. The offered sportsbook is excluded from its own
consensus. Bets need at least 2% EV, a 30% break-even probability, American odds from -200 to +300,
and at least three independent reference books. The slate is ranked by confidence-adjusted expected
log bankroll growth and is limited to one bet per event.

The paper bankroll starts at 100 units, equivalent to $10,000 at $100 per unit. Stakes use
quarter-Kelly sizing, rounded to 0.05 units, with a 0.25-unit minimum and 1-unit maximum. A pick is
recorded once at its original sportsbook and price; later price movement never rewrites history.
Run `python -m odds_scanner.strategy_review` to generate the weekly performance and strategy report.

Select **The Odds API** in the sidebar, enter a key from
[The Odds API](https://the-odds-api.com/), and choose **Fetch live odds**. The rest of the product
uses the same normalized domain objects and opportunity engines in either mode.

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
