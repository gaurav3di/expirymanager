# ExpiryManager

A zero-configuration data platform for Indian expired F&O contracts, built on the Fyers broker
API. It downloads complete expiry and contract catalogs, backfills historical OHLCV plus open
interest for the expiries you choose, keeps everything on a schedule, charts it, and exports it to
CSV and Parquet.

It is the datasource for options and futures research, and it is built so that a Phase 2 options
backtesting engine can read the store directly without a rewrite.

---

## What it does

- **Complete expiry data** for NIFTY, BANKNIFTY, SENSEX and RELIANCE out of the box, and any other
  NSE or BSE underlying you add through the UI.
- **Pick and download.** Choose an underlying, tick the expiries you want, choose resolutions, and
  see exactly what the download will cost in Fyers requests, time, rows and disk before it starts.
  The number you saw is the number the commit is allowed to spend.
- **A real pipeline.** Every outbound request is a durable row, so a multi-day backfill survives a
  restart, a token expiry or a closed laptop and resumes at the exact request it stopped on.
- **Schedulers.** Twelve built-in schedules keep the catalog current, capture second-resolution
  data inside the only 30 day window in which it exists, and snapshot the Fyers symbol master
  daily so lot sizes and tick sizes are recorded before expired contracts disappear from it.
- **Maximum metadata.** Full request provenance per chunk, the verbatim response fragment per
  contract, a slowly changing dimension for lot and tick size, and every field that can be parsed
  out of the Fyers symbology.
- **Charts.** Every contract renders in openalgo-charts with open interest as a separate pane.
- **Exports.** DuckDB writes denormalised, self-describing Parquet and CSV, and the archive layout
  includes the catalog so an export doubles as a restorable backup.

---

## Prerequisites

| Requirement | Version | Note |
|---|---|---|
| Python | 3.14.6 | Declared minimum is 3.14. |
| uv | any recent | The backend is run through `uv`. See https://docs.astral.sh/uv/ |
| Node | 26.4.0 | Needed only to build the frontend. Vite 8 requires `^20.19.0 \|\| >=22.12.0`. |
| npm | 11.17.0 | |
| A Fyers account | any | With an app registered whose redirect URI is exactly `http://127.0.0.1:8000/fyers/callback`. |
| Disk | 10 to 20 GB | The four seed underlyings over 2022 to 2026 at one minute land around 6 to 9 GB. |
| Network | outbound https | To `api-t1.fyers.in` and `public.fyers.in`. |

The app runs on loopback only and is designed for a single local user on their own machine.

There is no `.env` file and nothing to configure before the first start. Every setting lives in the
database and is edited in the UI, and nothing outside the app data directory is ever written.

Two environment variables exist, and neither is needed on a normal machine:

- `EXPIRYMANAGER_HOME` moves the data directory. It is the escape hatch the cloud sync refusal
  below names, and it is read at startup only, never by application code.
- `EXPIRYMANAGER_DEV_HTTPS=1` makes the Vite dev server serve TLS. See the development note, which
  explains why you almost certainly do not want it.

---

## The first run, end to end

Ten minutes, of which nine are the Fyers login. Everything below was run against a temporary data
directory before it was written down.

### 1. Build the frontend

```
scripts/build.sh
```

The backend serves `frontend/dist` from its own origin, so this has to exist before there is a UI.
It installs `node_modules` on the first run. Rebuilding later does not need a restart: the server
reads the directory per request.

### 2. Start the app

```
scripts/run.sh
```

Which prints, before it binds:

```
ExpiryManager 1.0.0
Data directory: /Users/you/.expirymanager
TLS: none. This server speaks plain HTTP on loopback.
     The redirect URI registered with Fyers must therefore read
     http://127.0.0.1:8000/fyers/callback, with no s. Fyers matches it exactly, so a
     mismatch fails the login rather than warning about it. Pass --https to serve TLS.
Listening on http://127.0.0.1:8000
```

**Plain HTTP, deliberately.** The scheme is not a preference. Fyers matches the registered
redirect URI character for character, and the one registered for this app is
`http://127.0.0.1:8000/fyers/callback`. Loopback is the one place http is legitimate, since a
self-signed certificate on 127.0.0.1 protects nothing that matters. There is no certificate
warning to accept, because there is no certificate. If you re-register the URI as https, start
with `scripts/run.sh --https` and the app generates its own certificate; the session cookie then
carries `Secure` to match, and your browser will warn once.

Open `http://127.0.0.1:8000`. You land on the three step setup wizard at `/setup`.

To see what would happen without starting anything, use `--check`, which prepares the data
directory, prints the same banner and exits:

```
scripts/run.sh --check --data-dir /tmp/em-scratch
```

### 3. Step 1: choose a username and a passcode

Three fields: **Username**, **Passcode**, **Repeat the passcode**. Both are local to this machine
and are sent nowhere. The passcode is at least 12 characters and is hashed with Argon2id, so it
cannot be recovered if it is lost, only replaced. This is the lock on the app itself, not your
Fyers password.

Press **Set the passcode and continue**. Step 1 then closes for good: the passcode can be changed
afterwards, but the account cannot be created twice, and a second attempt is refused with
`409 already_provisioned`, "This instance already has an account. Sign in instead."

You are signed in immediately, so the wizard carries straight on. Later visits, and every visit
after a restart, land on **Sign in** at `/login` and ask for the same username and passcode. That
is the lock on the app. The Fyers login in wizard step 3 is a separate thing entirely.

### 4. Step 2: paste your Fyers app credentials

Before the form, the screen shows the one value that has to match:

```
http://127.0.0.1:8000/fyers/callback
```

with a copy button. Register exactly that string on the Fyers dashboard against your app. A value
that differs by one character does not fail when you save it here. It fails later, during the
login, with a message that reads like a wrong app id.

Then four fields:

| Field | What it is |
|---|---|
| **Fyers app id** | From your app registration. It usually ends in a dash and three digits. Pasting `appid:secret` here is caught and named. |
| **Fyers app secret** | From the same place. Encrypted with AES-256-GCM before it reaches the database, under a key held in a 0600 file outside the database, and never displayed again, not even masked. Keep your own copy. |
| **Label** | Your own name for this registration. Cosmetic. |
| **Fyers plan** | **Standard** or **Prime**. This sets the outbound request budget, so it is not cosmetic: Standard is 10 per second, 200 per minute and 100,000 per day, and the limiter below is derived from it. |

Press **Save and continue**. The response carries `app_secret_configured: true` and never the
secret, which is also true of every later read of the broker settings.

### 5. Step 3: connect to Fyers

Press **Connect**. A new tab opens on the Fyers login page, where you authenticate with your
account password and TOTP. Fyers then redirects that tab back to
`http://127.0.0.1:8000/fyers/callback`, which finishes the exchange and lands on
**Settings, Broker**. The setup tab you left open notices within a few seconds and shows
**Connected to Fyers**.

If the return does not land, paste the URL you were redirected to into the fallback box on the
same screen. It runs exactly the same verification, including the single-use state check.

This interactive login is the only manual step in the whole system. It has to be repeated once a
day, which is the next section.

### 6. Discover expiries

Go to **Expiries** and choose an underlying. Four are seeded and ready:

| Underlying | Symbol | Data from |
|---|---|---|
| Nifty 50 | `NSE:NIFTY50-INDEX` | 2022-01-03 |
| Nifty Bank | `NSE:NIFTYBANK-INDEX` | 2022-01-03 |
| Sensex | `BSE:SENSEX-INDEX` | 2023-08-07 |
| Reliance Industries | `NSE:RELIANCE-EQ` | 2022-01-03 |

Nothing has been downloaded yet, so the table is empty. Press **Discover expiries**, which opens a
date range prefilled from the underlying's data floor to today, then **Queue discovery**. That
queues one job costing one Fyers request per 366 day window, and the expiry dates appear as it
finishes.

An expiry date is not the same thing as its contracts. Discovering the dates costs one request;
discovering which strikes existed for one of those dates costs another request per expiry. That is
the **Run contract discovery** button beside it, which fires the Contract discovery schedule now
for every undiscovered expiry whose date has passed. The download sheet refuses to price a
selection whose contracts are still unknown, saying so:

```
No contracts have been discovered for the selected expiries yet.
Discovery has to run before a download can be priced.
```

### 7. Price a download

Tick the expiries you want and open the download sheet. Choose **Options**, **Futures** or
**Both**, the strikes (**Every strike in the chain**, an **At the money band** in strike steps, or
**Named strikes**), the resolutions, and whether to carry **Open interest**.

The sheet prices itself on every change and spends no Fyers requests doing it. What it shows is a
real plan against the coverage ledger: requests, estimated rows, estimated bytes, an ETA against
the per-minute limit, what today's budget looks like afterwards, and how much of the selection is
already held or sealed and will therefore be skipped.

### 8. Run it

Press **Start download**. The request carries the exact estimate you were shown as
`confirm_requests`, and the commit is refused if that number no longer matches the plan, so a
sheet priced at 4 requests cannot spend 107. If the day's budget is nearly gone, **Queue for
tomorrow** puts the job in with the same guarantee.

### 9. Watch it

You land on the job detail screen. It streams: tasks done against total, requests used, rows
written, throughput, and the per-task table with the window each request asked for. **Jobs** lists
everything. A job can be paused, resumed and cancelled while it is live; all three are refused
with `409 job_finished` once it has ended, because reporting success for work that was already
over is worse than refusing.

### 10. Chart a contract

**Contracts** lists what landed, with row counts per resolution. **Chart** renders any one of them
in openalgo-charts with open interest in its own pane, and **Option chain** shows a whole expiry
at one moment in time.

Charting an expired contract works because the feed clamps the widget's now-relative request
window onto the bars the contract actually has. A contract that expired in March 2025 holds zero
bars in the window the widget asks for, and it still renders.

### 11. Export

**Exports**, then **New export**. Choose **Parquet** or CSV, **One file** or a partitioned
directory, the compression, whether to carry the symbol columns on every row, and whether to
include the catalog tables. The scope narrows by underlying, expiry range, resolutions and
instrument kind; every field is optional.

The result is a row in the table with a row count, a byte size and a sha256, and a file in
`~/.expirymanager/exports/` beside a `schema.json` that declares the types the file actually
holds. Deleting the row deletes the file.

---

## The daily login, which is the surprising part

**A 2FA login is required once a day. There is no way around it.**

Refresh tokens were discontinued from 1 April 2026 under SEBI's retail algo trading rules. The
flow that existed before that required your Fyers PIN and issued no rotated refresh token anyway.
This app therefore has no PIN field and no refresh flow at all: the guaranteed path is the
interactive login, and everything else is built around it rather than around a retry.

So:

- **The app logs itself out at 03:00 IST, on purpose.** A Fyers access token is day-scoped. Rather
  than let it die mid-request at an unpredictable moment, the `Scheduled daily logout` schedule
  parks every running job first and destroys the token second. That order is the whole design.
- **Jobs park, they do not fail.** A parked job is `blocked_auth`, not `failed`. Its tasks stay
  `pending` with their original attempt counts and their leases released, so nothing is retried,
  nothing is re-planned and no attempt is burned.
- **Logging back in resumes at the exact request.** Not at the start of the job, and not at the
  start of the contract. At the task the pool stopped on. Contracts that finished before the park
  are never requested again.
- **A dead token during the day does the same thing.** Eight workers seeing eight rejections
  produce one park, one banner and one login prompt, not eight.
- **Nothing is spent while parked.** The governor stops the pipeline, and a parked task writes no
  coverage row, because a false coverage row would make the missing data permanently invisible to
  the planner.

What you see is a banner saying the Fyers session has to be renewed, with a one-click re-login.
Press it, do the TOTP, and the pipeline picks up by itself.

The one job that keeps working with a dead token is the daily symbol master snapshot, because it
reads a public file rather than an authenticated endpoint. That matters: see the symbol master
note below.

---

## Operational limits that actually bite

### One process may hold `market.duckdb`

DuckDB refuses a second opener while a writer holds the file, not even read-only. That means:

- Do not run `uvicorn` with more than one worker. The app hardcodes one, for this reason and
  because a second scheduler would double every scheduled job.
- Do not run the scheduler as a separate process. It runs inside the app.
- Do not leave a `duckdb` CLI session, a DBeaver connection or a notebook open against the file
  while the app is running.
- To run a second instance for testing, give it its own directory with `--data-dir`, and remember
  that the OAuth callback can only ever reach the instance holding port 8000.

Startup also takes an advisory lock and refuses to start twice, rather than letting DuckDB report
a second instance as an IO error that reads like corruption.

### The Fyers budget, and what exceeding it costs

The number is on the top bar at all times. The Fyers Standard plan allows 10 requests per second,
200 per minute and 100,000 per day, and **exceeding the per-minute limit more than three times in
one day blocks the account for the rest of the day.**

ExpiryManager therefore:

- runs its own limiter at 8 per second and 170 per minute, under the published caps, with at most
  6 requests in flight,
- keeps the daily counter and the strike counter in the database, so a restart cannot reset them
  and a crash cannot lose more than 25 requests of the count,
- stops the entire pipeline on the first rate-limit response and waits for you to resume, rather
  than retrying and spending a second strike,
- refuses to resume at all after the fourth violation, until the IST date rolls,
- reserves 30 percent of the daily budget for downloads you start by hand, so an unattended sweep
  can never consume the whole day,
- and never starts a download without first showing you the request cost.

A full NIFTY 2022 to 2026 backfill is roughly 62,000 requests, about two thirds of one day of
Standard quota. Plan for it to span an evening, and let the scheduler continue it the next day.
The binding constraint is 170 per minute, not 8 per second: about 10,200 requests an hour.

### MCX is not served

The expired F&O endpoints cover NSE and BSE only. Every MCX underlying form was measured against
the live service and every one returned HTTP 422: `MCX:CRUDEOIL-COM`, `MCX:GOLD-COM`,
`MCX:CRUDEOIL`, `MCX:CRUDEOILM`, `MCX:GOLD`, `MCX:SILVER-COM`, `MCX:NATURALGAS`. `BSE:SENSEX-INDEX`
answered 200 in the same run, so it is the symbol being rejected and not a broken token.

MCX is therefore not offered in Add Underlying, and the resolver says so by name if you look for
it.

### 5S data exists only for the last 30 trading days

Second resolution can never be backfilled. Once a day falls out of the 30 trading day window, that
day's 5S data is gone from the vendor forever. The `Seconds capture` schedule runs every weekday
at 16:15 IST and is the highest priority job in the system. If the machine is off for a month,
that month of 5S is permanently gone, and no amount of budget brings it back.

Minute data and coarser are not affected. They can be fetched any time.

### The other things the broker limits

- **Daily, weekly and monthly candles are not available for expired contracts.** Any daily series
  in this app is aggregated from one minute bars inside DuckDB.
- **Data starts on 03 January 2022 for NSE and 07 August 2023 for BSE.** Requests are clamped to
  those floors rather than sent and rejected.
- **A historical request may span at most 100 calendar days.** 101 is a hard HTTP 422, not a
  truncation, so the chunk arithmetic has to be right rather than approximately right. The expiry
  dates window is 366 days with the same hard boundary.
- **Expired contracts vanish from the Fyers symbol master.** Lot size and tick size for a contract
  can only ever be captured while it is live, which is why the daily symbol master snapshot runs
  from day one and is the one job that keeps working when the broker token is dead. A contract
  first seen after its expiry has null lot and tick size, permanently.
- **Session hours are not constants.** The NSE derivatives close moved from 15:30 to 15:40 on
  2026-08-03, and special sessions run on some Saturdays and Sundays. Nothing in this app
  hardcodes a market open, close or session length, and observed trading days outrank the weekday
  rule.

---

## Running it

| Command | What it does |
|---|---|
| `scripts/build.sh` | Build the frontend into `frontend/dist`. |
| `scripts/run.sh` | Start on `http://127.0.0.1:8000`. |
| `scripts/run.sh dev` | The same with reload, for backend work. |
| `scripts/run.sh web` | The Vite dev server on `http://127.0.0.1:5173`. |
| `scripts/run.sh test` | The backend suite, then the frontend typecheck and suite. |

Anything after the mode reaches the underlying command, so `scripts/run.sh --data-dir /tmp/em`
and `scripts/run.sh test -k governor` both work. Without the scripts, the same thing by hand:

```
cd backend && uv sync && uv run expirymanager
cd frontend && npm install && npm run build
```

Flags, all of them optional:

```
--reload                 reload on source changes, development only
--log-level LEVEL        critical, error, warning, info (default) or debug
--json-logs              JSON on the console as well as in the log file
--data-dir PATH          override the data directory, default ~/.expirymanager
--check                  prepare the directory, report what was found, then exit
--https                  serve TLS from a self-signed certificate it generates
--version
```

The host and the port are not among them. They are fixed by the registered redirect URI.

### Development

Two terminals:

```
# terminal 1
scripts/run.sh dev          # http://127.0.0.1:8000, reloads on change

# terminal 2
scripts/run.sh web          # http://127.0.0.1:5173
```

The dev server proxies `/api` to the backend, so the browser talks to exactly one origin in
development as it does in production, and cookies and CSRF behave identically in both.

**Use `http://127.0.0.1:5173` and not `localhost`.** Cookies ignore the port but not the host, and
the same host is what lets the session cookie set by the OAuth callback on port 8000 be seen by
the dev origin on port 5173.

The dev server speaks the same scheme as the backend, which is http. A `Secure` cookie is not sent
over http at all, so a mismatch here does not warn, it silently logs you out on every request. If
you start the backend with `--https`, start the dev server with `EXPIRYMANAGER_DEV_HTTPS=1` too,
and it will reuse the certificate the backend generated.

---

## What the first run creates

```
~/.expirymanager/           0700
  master.key                the encryption key, 32 bytes, 0600
  config.sqlite3            settings, credentials, jobs, tasks, schedules, holidays
  market.duckdb             the catalog and every candle
  exports/                  CSV and Parquet you create, with a schema.json beside each
  raw/                      archived raw responses, for auditing
  backups/                  what the Backup action writes
  logs/                     expirymanager.log
  tmp/                      DuckDB spill space for large sorts
  tls/                      empty unless you start with --https
```

The process umask is set to 0o077 before anything opens a file, so the WAL and shared-memory
sidecars SQLite and DuckDB create for themselves are private too, which a later chmod could not
achieve.

The directory is refused outright if it resolves under iCloud Drive, Dropbox, OneDrive, Google
Drive, Nextcloud, pCloud or Syncthing. A sync client copying a WAL out from under two open
databases corrupts both, and it uploads the key file while it is at it.

---

## Where the data lives

`market.duckdb` is the source of truth. CSV and Parquet are exports, never inputs.

The candles table is nine columns wide (`contract_id`, `res_id`, `ts`, four `DECIMAL(11,4)`
prices, `volume`, `oi`), physically sorted by `(contract_id, res_id, ts)`, with no primary key and
no index, because that layout measured 15.21 bytes per row and 0.1 ms per-contract queries. All
descriptive richness lives in a small catalog joined at query time. Timestamps are naive
`TIMESTAMP` holding IST wall clock, so no query depends on a session timezone setting.

Prices are `DECIMAL(11,4)` and not `DECIMAL(9,2)` because a user may register an underlying whose
prices need four decimal places, and because a float that reads back as 100.5 instead of 100.5000
has lost the information that says which it was.

You can open the file with any DuckDB client while the app is **not** running, and the shipped
macros give you the same vocabulary the app uses:

```sql
SELECT * FROM bars(42101, 2, TIMESTAMP '2025-03-01', TIMESTAMP '2025-03-28');
SELECT * FROM chain_at(1, DATE '2025-03-27', 2, TIMESTAMP '2025-03-27 14:30:00');
SELECT * FROM atm_strike(1, DATE '2025-03-27', 2, TIMESTAMP '2025-03-27 14:30:00');
SELECT * FROM spot_at(1, 2, TIMESTAMP '2025-03-27 14:30:00');
```

`res_id` 2 is one minute. `res_id` 1 is 5S. The `ref_resolution` table has all fourteen.

---

## Maintenance

Settings has three actions, under **Storage**.

- **Checkpoint now** folds the write ahead log into the main file. Cheap, safe, and what you want
  before copying anything by hand.
- **Backup** checkpoints first and then copies the DuckDB file, its WAL and the SQLite files
  together into `backups/`. Copying the `.duckdb` alone, or without checkpointing, restores a
  database missing the most recent writes.
- **Compact the store** rewrites the whole database sorted and swaps it in. It closes the file to
  do so, which is why it is a deliberate manual action and not a schedule.

The Storage panel shows the file size against the size the rows model, and suggests compaction
when the ratio drifts. Treat that suggestion as a prompt to look, not as an instruction:
compaction reclaims space on a file that has been heavily re-downloaded, and measurably **grows**
a file that was written once and never churned. Check the before and after rather than running it
on a schedule.

Do not interrupt a compaction. It swaps two files, and there is a window between the two renames
in which neither is in place.

---

## Documentation

| Document | Contents |
|---|---|
| `docs/ARCHITECTURE.md` | Components, process model, end-to-end flow, module layout, trust boundaries. |
| `docs/DATA-MODEL.md` | Every table with exact DDL, the metadata captured, and the Phase 2 queries the schema is built for. |
| `docs/PIPELINE.md` | Job and task decomposition, the rate limiter, retry policy, resume, idempotent writes, token expiry, the scheduler. |
| `docs/API.md` | Every endpoint with method, path, models, auth, rate limit and error cases. |
| `docs/SECURITY.md` | Encryption scheme, key location, OAuth, sessions, CSRF, headers, and the never-log and never-return lists. |
| `docs/API-PROBES.md` | Vendor behaviour measured against the live service. It overrides any inference. |
| `docs/BUILD-PLAN.md` | Ordered implementation phases and the parallelisable work items. |
| `docs/research/` | The verified research notes the design is built on. Every number in them was measured. |

---

## Writing rules for this repository

Inherited from openalgo-charts and applied project-wide:

- No emoji and no icons anywhere: code, comments, log messages, commit messages, docs, tests or
  terminal output. Plain text labels only.
- No em dashes and no en dashes. Use a comma, a colon, parentheses or a full stop.
- Comments explain why, not what.
- Conventional Commits.
- Never write a real credential into any file, fixture, log or error message. Tests use synthetic
  values, and application code never reads a credential file.
