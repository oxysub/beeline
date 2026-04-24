# Product requirements: Beeline → Airtable sync

## 1. Document control

| Field | Value |
|--------|--------|
| Product | Beeline request export to Airtable |
| Code location | `beeline/` (FastAPI + sync engine) |
| Status | As implemented in repository |

## 2. Summary

**Problem.** Recruiting and MSP teams export request data from **Beeline** (Excel) and need the same data reflected in a shared **Airtable** view without manual copy-paste, while enforcing business rules about which rows qualify.

**Solution.** A small Python service that (1) reads a Beeline Excel export, (2) filters and normalizes rows, (3) **creates** new Airtable records or **updates** existing ones (primarily *last seen* and duration math), and (4) surfaces progress and a structured result via a **web upload UI** and a **command-line** entry point.

## 3. Goals

1. **Reduce manual work** for approved requests: one upload path from Excel to Airtable.
2. **Enforce business rules** automatically: only certain MSP owners, minimum engagement length when duration can be measured.
3. **Keep Airtable accurate over time** for rows that reappear: update “last seen” and refresh computed duration in months when applicable.
4. **Be observable**: users and operators see how many rows were included, skipped, inserted, updated, or errored.

## 4. Non-goals

- Full bidirectional sync (Airtable is not the source of truth for creating Beeline records).
- Editing Beeline or supplier workflows inside this app.
- Multi-tenant Airtable configuration via UI (base/table IDs are code-level configuration in this build).
- Deleting Airtable rows that disappear from a future export (stale records may remain; they are *reported* as “in Airtable but not in this file”).

## 5. Personas

| Persona | Need |
|--------|------|
| **Recruiter / coordinator** | Upload an `.xlsx` from Beeline, see clear success or errors, open Airtable to work the queue. |
| **Operations / engineering** | Configure token and URLs, run the app locally or behind a reverse proxy, troubleshoot API errors. |

## 6. User stories

1. As a coordinator, I upload the Beeline export so that **new** requests (by `Request-ID`) **appear in Airtable** with the right fields.
2. As a coordinator, I re-upload a later export so that **existing** `Request-ID`s get **Last Seen in Excel** updated (and **Duration Mth** refreshed when it can be computed).
3. As a business owner, I want only rows **owned by Eric** (word match) and with **sufficient duration** (when we can convert duration to months) to sync, so the table does not get cluttered with ineligible work.
4. As an operator, I want a **health** endpoint and a **CLI** path for scripts or debugging without the browser.

## 7. Functional requirements

### 7.1 Input: Excel

- **Format:** `.xlsx` or `.xls` (web); pandas/openpyxl read path.
- **Required columns (after header trim):** `Request-ID`, `MSP Owner`.
- **Rows:** Empty or invalid `Request-ID` rows are dropped after load.
- **Source file name (CLI default):** `Request_Submitted_to_Supplier_Drill_Down.xlsx` in the working directory when running the module (unless another path is supplied).

### 7.2 Filtering and eligibility

| Rule | Behavior |
|------|----------|
| **MSP Owner: Eric** | Keep rows where `MSP Owner` contains the **whole word** `Eric`, case-insensitive (regex word boundary). Others are counted as skipped (not Eric). |
| **Minimum duration (months)** | For each Eric row, if **Duration** parses to a **number of months**, exclude rows where that value is **strictly below** a configurable floor (default **8** months). Skipped count is reported separately. |
| **Unparseable duration** | If duration **does not** parse to months, the row is **not** excluded by the minimum-duration rule (it remains eligible if Eric is satisfied). |

### 7.3 Duration and “Duration Mth”

- **Beeline-style strings** such as `56 W, 4 D` (weeks and days) convert to **total days**, then to **months** by dividing by a configurable **days-per-month** (default calendar month length from 365.25/12, overridable for a 30-day “month” policy).
- **Plain numeric** duration values in Excel are treated as **already in months** (one decimal in output).
- **Date-like** strings and non-duration text are not forced into a month number (parser returns none for those cases for the numeric field as appropriate).
- On **create:** map computed **Duration Mth** to Airtable’s number field when available.  
- On **update:** set **Last Seen in Excel** to today; add **Duration Mth** only when it can be computed from the current row.

### 7.4 Airtable operations

- **Key:** `Request-ID` (string) identifies a logical request across Excel and Airtable.
- **If `Request-ID` not in Airtable:** create a new record (batched, see §8).  
- **If `Request-ID` already exists:** patch that record (last seen, optional duration month).
- **Ordering:** Process rows in **Excel row order**; batch consecutive operations of the **same** type (insert vs update) up to a fixed batch size (≤ Airtable API limits, implemented as 5 per request for reliability). If a batch fails, fall back to **per-row** calls for that segment to isolate failures.
- **Reporting:** Return counts for inserted, updated, errors, rows in sheet, rows skipped (not Eric), rows skipped (short duration), and **request IDs in Airtable** that are **missing from** the current **eligible** export (informational, not a delete action).

### 7.5 Web application

- **GET `/`** serves the static upload UI.
- **POST `/api/sync`** accepts a multipart file upload, streams **NDJSON** (newline-delimited JSON) for `start` → `progress` (per row) → `complete` (full result) or `error` lines.
- **GET `/health`** returns a simple JSON OK (for load balancers and probes).
- **Configuration:** If `AIRTABLE_TOKEN` is missing, the API returns a clear error (not a generic failure).

### 7.6 Command line

- `python -m bl_upload` (or `python bl_upload.py`) runs a full sync using the default or provided Excel file and prints a human-readable summary, including Airtable-only request IDs and skip statistics.

## 8. Non-functional requirements

- **Reliability:** Batch with fallback to single-record operations on batch failure.  
- **API limits:** Respect Airtable’s per-request record count (batches of 5 in implementation).  
- **Config:** Secrets via environment; no secrets committed.  
- **Progress:** Web flow streams events so the UI can show a progress bar without waiting for the entire sync to finish.

## 9. Data mapping (Beeline / Excel → Airtable)

On **create**, non-empty source fields (after cleaning) are sent where applicable, including: Request-ID, Request, Request Title, Qty, Desired Start Date, Duration, Duration Mth, Comments for Suppliers, MSP Owner, Date Released, and **Last Seen in Excel** (set to the sync’s “today”).

On **update**, fields sent are at minimum **Last Seen in Excel**; **Duration Mth** is included when it can be derived from the row.

## 10. API contract (result object, high level)

The completion payload (also embedded in CLI-oriented dicts) includes, among other keys:

- `inserted`, `updated_last_seen`  
- `errors` (list of per-row error objects)  
- `ok` (no errors)  
- `rows_in_sheet`, `rows_matched_eric` (rows that passed all filters and were processed), `rows_skipped_not_eric`, `rows_skipped_short_duration`  
- `missing_from_excel_count` / `missing_from_excel_ids` (Airtable request IDs not in this eligible export)  
- `airtable_url` (link to the table or view, when configured)

## 11. Configuration (environment)

| Variable | Role |
|----------|------|
| `AIRTABLE_TOKEN` | Bearer token for the Airtable API (required). |
| `AIRTABLE_TABLE_WEB_URL` | Optional. Link shown in UI/results (defaults to a constructed URL from base and table in code). |
| `MIN_DURATION_MONTHS_SYNC` | Optional. Minimum months when duration parses (default `8`). |
| `DURATION_DAYS_PER_MONTH` | Optional. Days in one “month” for W/D-style conversion (default calendar month length). |
| `PORT` | Optional. Uvicorn port when using `if __name__` in `app.py` (default 8000). |

Base and table IDs for the Airtable API are **compile-time** constants in `bl_upload.py` in this project; change them there (or future refactor) for a different Airtable base.

## 12. Success criteria

- A valid export can be uploaded end-to-end with **no manual field mapping** for supported columns.
- **Eric-only** and **minimum duration** rules are **transparent** in both streamed events and the final result.
- **Re-uploads** update last-seen (and duration month when possible) for existing `Request-ID`s.
- **Failures** are **row-level** in the `errors` list without silently dropping the whole run.

## 13. Open questions / future work

- Whether **unparseable** durations should be **included** or **excluded** (currently: included if Eric passes).  
- Whether to **deprecate** Airtable records that are no longer in eligible exports (policy decision; not implemented).  
- **Multi-table** or **multi-MSP** support via configuration instead of hardcoded base/table.  

---

*This PRD describes behavior as implemented in the repository. If code and this document disagree, the source code is authoritative until the document is updated.*
