# Advantage Betting Terminal

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

Run `install_windows_shortcut.ps1` once. It creates an **Advantage Betting Terminal** shortcut
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

## Optional live data

For the BC-first free workflow, select **OddsPapi Free**, open **Odds Data**, choose the sports
and markets you want, and press **Refresh Odds**. Refreshing is manual: page loads, visitors,
filters, sorting, and navigation never call the provider. The initial refresh scope supports
NFL, NCAAF, NBA, and NHL core markets; CFL and on-demand player props remain available.
OddsPapi uses one request per enabled sportsbook on each refresh. The first refresh also caches
the provider's league and market catalog locally, so later refreshes use fewer requests.

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
- `refresh.py`: shared manual/future-scheduler refresh orchestration, revalidation, freshness,
  adaptive-priority policy, and budget guardrails. Automated scheduling is not enabled.
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
