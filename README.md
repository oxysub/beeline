# Beeline → Airtable sync

A minimal **FastAPI** service and **Python** sync job that read a **Beeline Excel** export, apply business filters, and **create** or **update** records in a linked **Airtable** table. A static **web UI** under `/` supports drag-and-drop upload with a streaming progress bar.

## What it does

1. **Reads** a Beeline export (`.xlsx` / `.xls`) with a `Request-ID` column.
2. **Keeps** only rows where **MSP Owner** contains the whole word **Eric** (case-insensitive).
3. **Excludes** rows whose **Duration** converts to a **number of months** *below* a configurable floor (default **8** months). Durations that **cannot** be parsed to months are **not** dropped by that rule.
4. **Converts** common Beeline **Duration** text (e.g. `56 W, 4 D`) into a **Duration Mth** value (months) for Airtable, using a configurable days-per-month (default is a calendar month length)
5. **Inserts** new `Request-ID`s and **patches** existing ones: sets **Last Seen in Excel** to today, and **Duration Mth** on updates when it can be computed.
6. **Streams** progress from `POST /api/sync` as **NDJSON** and returns a full JSON result at the end.

## Requirements

- **Python 3.10+** (3.12 recommended) with `pip`
- An **Airtable personal access token** with access to the target base
- The Airtable **base** and **table** IDs are set in `bl_upload.py` (`AIRTABLE_BASE_ID`, `AIRTABLE_TABLE_ID`); change them there for another table.

## Setup

```bash
cd beeline
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Configuration

Create `beeline/.env` (same folder as `app.py` and `bl_upload.py`):

| Variable | Required | Description |
|----------|----------|-------------|
| `AIRTABLE_TOKEN` | **Yes** | Airtable API token: `Authorization: Bearer …` |
| `AIRTABLE_TABLE_WEB_URL` | No | “View in Airtable” link in the UI; defaults to a URL built from the base and table in code. |
| `MIN_DURATION_MONTHS_SYNC` | No | Minimum **months** when duration parses (default `8`). |
| `DURATION_DAYS_PER_MONTH` | No | Days in one “month” for W/D → months math (default `365.25/12`); use `30` for a 30-day month. |

`python-dotenv` loads `.env` from the `beeline` directory automatically.

## Run the web app

From the **`beeline`** directory:

```bash
make            # list targets (default)
make dev
# or: PORT=3000 make dev
```

Open **http://127.0.0.1:8000/** (or your `PORT`) and upload an Excel file. **GET /health** returns `{"status":"ok"}` for probes.

### Public HTTPS (recommended: own subdomain)

Serving the app at its **own hostname** (for example **https://beeline.oxydata.my/**) is simpler than putting it under a path on the main site (`/beeline` on `oxydata.my`). You get one DNS name, proxy `/` to Uvicorn, and no path rewriting. The upload page resolves `POST` to `/api/sync` from the current origin, so it works the same on localhost and on the subdomain.

1. **DNS** — add an `A` or `CNAME` so **beeline.oxydata.my** points to your server.
2. **TLS** — use Let’s Encrypt (Certbot) or your host’s managed certificate for that hostname.
3. **Reverse proxy** — Nginx (sketch):

   ```nginx
   server {
     server_name beeline.oxydata.my;
     # ssl_certificate / ssl_certificate_key (Certbot or similar)

     location / {
       proxy_pass http://127.0.0.1:8000;
       proxy_http_version 1.1;
       proxy_set_header Host $host;
       proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
       proxy_set_header X-Forwarded-Proto $scheme;
     }
   }
   ```

4. **Process** — run Uvicorn on `127.0.0.1:8000` (or another port) under **systemd** or **supervisor**, with `AIRTABLE_TOKEN` (and the rest) loaded from `beeline/.env` or the environment.

Replace **beeline.oxydata.my** with whatever hostname you use.

## Git and Render

**First time in this folder:** there must be a Git repo and a remote.

```bash
git init
git remote add origin https://github.com/oxysub/beeline.git   # your URL
git add .
git commit -m "Initial"
```

(If you already ran the above, you can use `make push` after any new commits.)

| Command | What it does |
|--------|----------------|
| `make push` | `git push -u` to `origin` and branch `main` (override with `REMOTE=…` `BRANCH=…`) |
| `make render` | `POST` to a Render **deploy hook** (URL in env `RENDER_DEPLOY_HOOK` or in `.env`, gitignored) |
| `make release` | `make push` then `make render` — use when you want both a push and a manual deploy trigger |

- **Render service:** In the [Render](https://render.com) dashboard, create a **Web Service** from this Git repo, or add **`render.yaml`** as a **Blueprint**. Set **`AIRTABLE_TOKEN`** and any optional env keys in the service **Environment** tab.
- **Auto-deploy:** If the service is connected to the repo with auto-deploy on push, **`make push`** is enough to start a new deploy; **`make render`** is for the optional **Deploy hook** (Render → your service → **Settings** → **Build & deploy** → **Deploy hook**). Add that URL to `beeline/.env` as `RENDER_DEPLOY_HOOK=…` for `make render` / `make release`.

## Run the sync from the command line

Default input file: `Request_Submitted_to_Supplier_Drill_Down.xlsx` in the **current working directory** (change `EXCEL_FILE` in `bl_upload.py` or import and call `sync_excel` with a path).

```bash
cd beeline
python3 -m bl_upload
```

The script prints insert/update counts, skip counts (not Eric, short duration), and Airtable `Request-ID`s that are not in the eligible export.

## API

| Method / path | Description |
|---------------|-------------|
| `GET /` | Static `static/index.html` (upload UI). |
| `POST /api/sync` | `multipart/form-data` with a single file field `file` (`.xlsx` or `.xls`). Response: `application/x-ndjson` stream (`start` → `progress` → `complete` or `error`). |
| `GET /health` | Liveness: `{"status":"ok"}`. |

If `AIRTABLE_TOKEN` is missing, the server responds with **500** and a message to configure `.env`.

## Project layout

| File | Role |
|------|------|
| `app.py` | FastAPI app, file upload, NDJSON `StreamingResponse`. |
| `bl_upload.py` | Excel read, filters, Airtable list/create/update, batching, `iter_sync_ndjson`, `sync_excel`, CLI. |
| `static/index.html` | User-facing upload and progress. |
| `Makefile` | `make dev`, `make install`, `make push`, `make render`, `make release`. |
| `render.yaml` | Render Blueprint (web service: `uvicorn app:app`). |
| `requirements.txt` | fastapi, uvicorn, pandas, openpyxl, requests, python-dotenv, python-multipart. |

## Product note

The **PRD** (`PRD.md`) describes business rules, field behavior, and non-goals in more detail. If the README and the code disagree, trust the code or update the docs.

## License

If the parent monorepo defines a license, it applies here. Otherwise follow your organization’s default.
