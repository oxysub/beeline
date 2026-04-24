import os
import io
import json
import re
from pathlib import Path
from typing import (
    Any,
    BinaryIO,
    Callable,
    Dict,
    Iterator,
    List,
    Optional,
    Tuple,
    Union,
)

import pandas as pd
import requests
import math
from datetime import date, datetime
from dotenv import load_dotenv

_BEELINE_ROOT = Path(__file__).resolve().parent
# Load next to this module so the token is found even when cwd is not beeline/ (e.g. uvicorn from repo root)
load_dotenv(_BEELINE_ROOT / ".env")

# Keep token in .env
AIRTABLE_TOKEN = os.getenv("AIRTABLE_TOKEN")

# Airtable IDs hardcoded as requested
AIRTABLE_BASE_ID = "appYDuaNdV87C964E"
AIRTABLE_TABLE_ID = "tblpOssJgjlMvnxS9"

# Web app link to this table (override in .env if you use a shared view URL)
AIRTABLE_TABLE_WEB_URL = os.getenv(
    "AIRTABLE_TABLE_WEB_URL",
    f"https://airtable.com/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_ID}",
)

EXCEL_FILE = "Request_Submitted_to_Supplier_Drill_Down.xlsx"

AIRTABLE_URL = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_ID}"
# Airtable allows at most 10 records per create or update request; we use 5 for smaller batches
AIRTABLE_BATCH_SIZE = 5

# Do not sync rows whose duration is under this many months (after W/D → months conversion)
MIN_DURATION_MONTHS_SYNC = float(os.getenv("MIN_DURATION_MONTHS_SYNC", "8"))

HEADERS = {
    "Authorization": f"Bearer {AIRTABLE_TOKEN}",
    "Content-Type": "application/json"
}

TODAY = date.today().isoformat()

# Only sync rows where MSP Owner contains the name Eric (whole word, case-insensitive)
_ERIC_WORD = re.compile(r"(?i)\bEric\b")


def msp_owner_has_eric(value) -> bool:
    if pd.isna(value):
        return False
    return _ERIC_WORD.search(str(value)) is not None


def clean_value(value):
    if pd.isna(value):
        return None
    return value


def _decimal_string_for_parse(s: str) -> str:
    """Handle European decimals like 4,2; strip thousands in 1,234.5 style."""
    t = s.replace("\u00a0", " ").strip().replace(" ", "")
    m = re.match(r"^([-+]?)(\d+),(\d+)$", t)
    if m and "." not in t and len(m.group(3)) <= 2:
        return f"{m.group(1)}{m.group(2)}.{m.group(3)}"
    return t.replace(",", "")


# Beeline "56 W, 4 D" -> total days / days-per-month. Default: calendar (365.25/12).
# Set DURATION_DAYS_PER_MONTH=30 in .env if your org uses 30-day "months"
DAYS_PER_CALENDAR_MONTH = float(
    os.getenv("DURATION_DAYS_PER_MONTH", str(365.25 / 12.0))
)


def _try_parse_weeks_days(s: str) -> Optional[Tuple[float, float]]:
    """
    Beeline style e.g. "56 W, 4 D" (weeks, days). Returns (weeks, days) or None.
    """
    t = s.strip()
    if not t:
        return None
    mW = re.search(
        r"(\d+(?:\.\d+)?)\s*(?:W|wks?|weeks?)\b",
        t,
        re.I,
    )
    mD = re.search(
        r"(\d+(?:\.\d+)?)\s*(?:D|days?)\b",
        t,
        re.I,
    )
    if mW is None and mD is None:
        return None
    w = float(mW.group(1)) if mW else 0.0
    d = float(mD.group(1)) if mD else 0.0
    return w, d


def value_for_duration_mth(raw) -> Optional[float]:
    """
    Airtable "Duration Mth" is a decimal (number) field in **months** (one decimal).

    Beeline "Duration" is often "56 W, 4 D" = 56 weeks and 4 days, converted
    to months. Plain numbers are treated as **already in months** (e.g. 4.2).
    """
    if raw is None:
        return None
    if isinstance(raw, (pd.Timestamp, datetime, date)):
        return None
    try:
        if pd.isna(raw):
            return None
    except TypeError:
        pass

    s = str(raw).strip()
    if s and s.lower() not in ("nan", "none", "n/a", "-", "—"):
        wd = _try_parse_weeks_days(s)
        if wd is not None:
            weeks, days = wd
            total_days = weeks * 7.0 + days
            if not math.isfinite(total_days) or total_days < 0 or abs(total_days) >= 1e15:
                return None
            months = total_days / DAYS_PER_CALENDAR_MONTH
            if not math.isfinite(months) or abs(months) >= 1e15:
                return None
            return round(float(months), 1)

    n = pd.to_numeric(raw, errors="coerce")
    if not pd.isna(n):
        x = float(n)
        if not math.isfinite(x) or abs(x) >= 1e15:
            return None
        return round(x, 1)

    s = s.strip() if s else ""
    if not s or s.lower() in ("nan", "none", "n/a", "-", "—"):
        return None
    if re.match(r"^\d{1,2}/\d{1,2}/\d{2,4}$", s) or re.match(
        r"^\d{4}-\d{1,2}-\d{1,2}$", s
    ):
        return None
    s_num = _decimal_string_for_parse(s)
    m = re.search(
        r"[-+]?(?:\d+)(?:\.\d+)?",
        s_num,
    )
    if m:
        try:
            x = float(m.group(0))
            if not math.isfinite(x) or abs(x) >= 1e15:
                return None
            return round(x, 1)
        except ValueError:
            return None
    return None


def _row_meets_min_duration_months(row: "pd.Series") -> bool:
    """False if duration parses to months and is below MIN_DURATION_MONTHS_SYNC."""
    m = value_for_duration_mth(row.get("Duration"))
    if m is None:
        return True
    return m >= MIN_DURATION_MONTHS_SYNC


FileSource = Union[str, bytes, bytearray, BinaryIO]


def read_excel(file: FileSource = EXCEL_FILE):
    """
    Load the workbook from a path (default) or from uploaded bytes / a file-like object.
    A web API can pass request body bytes or UploadFile after .read().
    """
    try:
        if isinstance(file, (str, os.PathLike)):
            df = pd.read_excel(file)
        else:
            if isinstance(file, (bytes, bytearray)):
                file = io.BytesIO(file)
            elif not isinstance(file, io.IOBase):
                raise TypeError("file must be a path, bytes, or binary file-like object")
            df = pd.read_excel(file)
    except Exception as e:  # noqa: BLE001
        # pandas/zip may raise for non-Excel bytes
        raise ValueError(
            f"Could not read this as an Excel file. Please upload the .xlsx (or .xls) you exported from Beeline. ({e!s})"
        ) from e
    df.columns = df.columns.str.strip()

    if "Request-ID" not in df.columns:
        raise ValueError("Excel must contain a column called 'Request-ID'")
    if "MSP Owner" not in df.columns:
        raise ValueError("Excel must contain a column called 'MSP Owner'")

    df["Request-ID"] = df["Request-ID"].astype(str).str.strip()

    # Remove blank Request-ID rows
    df = df[df["Request-ID"] != ""]
    df = df[df["Request-ID"].str.lower() != "nan"]

    return df


def get_airtable_records():
    records = {}
    offset = None

    while True:
        params = {}
        if offset:
            params["offset"] = offset

        response = requests.get(AIRTABLE_URL, headers=HEADERS, params=params)
        response.raise_for_status()

        data = response.json()

        for record in data.get("records", []):
            fields = record.get("fields", {})
            request_id = str(fields.get("Request-ID", "")).strip()

            if request_id:
                records[request_id] = {
                    "record_id": record["id"],
                    "fields": fields
                }

        offset = data.get("offset")
        if not offset:
            break

    return records


def prepare_eric_dataframe(
    file: FileSource,
) -> Tuple[pd.DataFrame, int, int, int]:
    """
    Read Excel, keep rows where MSP Owner is Eric and duration is at least
    MIN_DURATION_MONTHS_SYNC months (when duration can be parsed).

    Returns (filtered_dataframe, rows_in_sheet, rows_skipped_not_eric,
    rows_skipped_short_duration).
    """
    df = read_excel(file)
    rows_in_sheet = len(df)
    df_eric = df[df["MSP Owner"].apply(msp_owner_has_eric)]
    rows_skipped = rows_in_sheet - len(df_eric)
    n_eric = len(df_eric)
    df_ok = df_eric[df_eric.apply(_row_meets_min_duration_months, axis=1)]
    skipped_short = n_eric - len(df_ok)
    return df_ok, rows_in_sheet, rows_skipped, skipped_short


def _build_result(
    inserted: int,
    updated: int,
    errors: List[dict],
    excel_ids: set,
    airtable_ids: set,
    rows_in_sheet: int,
    rows_for_eric: int,
    rows_skipped_not_eric: int,
    rows_skipped_short_duration: int,
) -> Dict[str, Any]:
    missing_from_excel = sorted(airtable_ids - excel_ids)
    return {
        "inserted": inserted,
        "updated_last_seen": updated,
        "missing_from_excel_count": len(missing_from_excel),
        "missing_from_excel_ids": missing_from_excel,
        "errors": errors,
        "ok": len(errors) == 0,
        "rows_in_sheet": rows_in_sheet,
        "rows_matched_eric": rows_for_eric,
        "rows_skipped_not_eric": rows_skipped_not_eric,
        "rows_skipped_short_duration": rows_skipped_short_duration,
        "airtable_url": AIRTABLE_TABLE_WEB_URL,
    }


def _ndjson_line(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


def iter_sync_ndjson(file: FileSource) -> Iterator[bytes]:
    """
    Yields UTF-8 NDJSON lines: start, per-row progress, then complete (with full result) or error.
    """
    if not AIRTABLE_TOKEN:
        yield _ndjson_line(
            {
                "type": "error",
                "message": "AIRTABLE_TOKEN is missing from .env",
            }
        )
        return
    try:
        df, rows_in_sheet, rows_skipped, rows_skipped_short = (
            prepare_eric_dataframe(file)
        )
    except ValueError as e:
        yield _ndjson_line({"type": "error", "message": str(e)})
        return
    n = len(df)
    yield _ndjson_line(
        {
            "type": "start",
            "total": n,
            "rows_in_sheet": rows_in_sheet,
            "rows_skipped_not_eric": rows_skipped,
            "rows_skipped_short_duration": rows_skipped_short,
        }
    )
    if n == 0:
        result = _build_result(
            0, 0, [], set(), set(), rows_in_sheet, 0, rows_skipped, rows_skipped_short
        )
        yield _ndjson_line({"type": "complete", "result": result})
        return
    try:
        airtable_records = get_airtable_records()
    except requests.RequestException as e:
        yield _ndjson_line(
            {
                "type": "error",
                "message": f"Failed to load Airtable: {e!s}"[:2000],
            }
        )
        return

    excel_ids = set(df["Request-ID"])
    airtable_ids = set(airtable_records.keys())
    rows_list = list(df.iterrows())
    inserted = 0
    updated = 0
    errors: List[dict] = []
    pos = 0
    for op, segment in iter_request_segments(rows_list, airtable_records):
        if op == "update":
            upd_items = [
                (airtable_records[rid]["record_id"], r) for rid, r in segment
            ]
            if not _update_batch(upd_items):
                for rid, r in segment:
                    err = update_existing_row(airtable_records[rid]["record_id"], r)
                    if err:
                        errors.append(
                            {
                                "request_id": rid,
                                "op": "update_last_seen",
                                "detail": err[:2000],
                            }
                        )
                    else:
                        updated += 1
                        pos += 1
                        yield _ndjson_line(
                            {
                                "type": "progress",
                                "current": pos,
                                "total": n,
                                "request_id": rid,
                                "phase": "update",
                            }
                        )
                        print("Updated Last Seen + fields:", rid)
            else:
                for rid, r in segment:
                    updated += 1
                    pos += 1
                    yield _ndjson_line(
                        {
                            "type": "progress",
                            "current": pos,
                            "total": n,
                            "request_id": rid,
                            "phase": "update",
                        }
                    )
                    print("Updated batch, Last Seen:", rid)
        else:
            to_create = [r for _, r in segment]
            if not _create_batch(to_create):
                for rid, r in segment:
                    err = create_airtable_record(r)
                    if err:
                        errors.append(
                            {
                                "request_id": rid,
                                "op": "insert",
                                "detail": err[:2000],
                            }
                        )
                    else:
                        inserted += 1
                        pos += 1
                        yield _ndjson_line(
                            {
                                "type": "progress",
                                "current": pos,
                                "total": n,
                                "request_id": rid,
                                "phase": "insert",
                            }
                        )
            else:
                for rid, r in segment:
                    inserted += 1
                    pos += 1
                    yield _ndjson_line(
                        {
                            "type": "progress",
                            "current": pos,
                            "total": n,
                            "request_id": rid,
                            "phase": "insert",
                        }
                    )
                    print("Inserted batch:", rid)

    print("\nRecords in Airtable but missing from this Excel (Eric filter):")
    for rid in sorted(airtable_ids - excel_ids):
        print("-", rid)

    print("\nSync summary")
    print("Inserted:", inserted)
    print("Updated Last Seen:", updated)
    print("Rows skipped (not Eric):", rows_skipped)
    print("Rows skipped (duration <", MIN_DURATION_MONTHS_SYNC, "mths):", rows_skipped_short)
    print("Missing from Eric Excel export:", len(airtable_ids - excel_ids))

    result = _build_result(
        inserted, updated, errors, excel_ids, airtable_ids,
        rows_in_sheet, n, rows_skipped, rows_skipped_short,
    )
    yield _ndjson_line({"type": "complete", "result": result})


def build_create_fields(row) -> Dict[str, Any]:
    fields: Dict[str, Any] = {
        "Request-ID": str(row.get("Request-ID", "")).strip(),
        "Request": clean_value(row.get("Request")),
        "Request Title": clean_value(row.get("Request Title")),
        "Qty": clean_value(row.get("Qty")),
        "Desired Start Date": clean_value(row.get("Desired Start Date")),
        "Duration": clean_value(row.get("Duration")),
        "Duration Mth": value_for_duration_mth(row.get("Duration")),
        "Comments for Suppliers": clean_value(row.get("Comments for Suppliers")),
        "MSP Owner": clean_value(row.get("MSP Owner")),
        "Date Released": clean_value(row.get("Date Released")),
        "Last Seen in Excel": TODAY,
    }
    return {k: v for k, v in fields.items() if v is not None}


def build_update_fields(row) -> Dict[str, Any]:
    """Patch fields for an existing Airtable record (last seen + optional Duration Mth)."""
    fields: Dict[str, Any] = {"Last Seen in Excel": TODAY}
    duration_mth = value_for_duration_mth(row.get("Duration"))
    if duration_mth is not None:
        fields["Duration Mth"] = duration_mth
    return {k: v for k, v in fields.items() if v is not None}


def create_airtable_record(row) -> Optional[str]:
    """Single-row create. Prefer batching via API create batch when many rows."""
    fields = build_create_fields(row)
    if not fields.get("Request-ID"):
        return "Missing Request-ID"
    payload = {"fields": fields}
    response = requests.post(AIRTABLE_URL, headers=HEADERS, json=payload)
    rid = fields.get("Request-ID")
    if response.status_code not in [200, 201]:
        print("Insert error:", rid, response.text)
        return response.text
    print("Inserted:", rid)
    return None


def _create_batch(rows: List) -> bool:
    """True if batch succeeded. False → caller may retry one-by-one."""
    if not rows:
        return True
    body = {"records": [{"fields": build_create_fields(r)} for r in rows]}
    response = requests.post(AIRTABLE_URL, headers=HEADERS, json=body)
    if response.status_code in (200, 201):
        return True
    print("Batch insert error:", response.text)
    return False


def _update_batch(
    id_and_rows: List[Tuple[str, Any]],
) -> bool:
    """
    id_and_rows: (airtable record id, row series) per item.
    True if batch succeeded.
    """
    if not id_and_rows:
        return True
    body: Dict[str, List[Dict[str, Any]]] = {
        "records": [
            {"id": rid, "fields": build_update_fields(r)}
            for rid, r in id_and_rows
        ]
    }
    response = requests.patch(AIRTABLE_URL, headers=HEADERS, json=body)
    if response.status_code in (200, 201):
        return True
    print("Batch update error:", response.text)
    return False


def update_existing_row(record_id, row) -> Optional[str]:
    """Single-row patch (used as fallback if batch update fails)."""
    fields = build_update_fields(row)
    payload = {"fields": fields}
    url = f"{AIRTABLE_URL}/{record_id}"
    response = requests.patch(url, headers=HEADERS, json=payload)
    if response.status_code not in [200, 201]:
        print("Update error:", record_id, response.text)
        return response.text
    return None


def iter_request_segments(
    rows_list: List[Tuple[Any, Any]], airtable_records: dict
) -> Iterator[Tuple[str, List[Tuple[str, Any]]]]:
    """
    Yields (operation, segment) where op is "insert" or "update" and each segment
    is at most AIRTABLE_BATCH_SIZE consecutive rows in file order, all the same op.
    """
    n = len(rows_list)
    i = 0
    while i < n:
        _, row = rows_list[i]
        request_id = str(row["Request-ID"]).strip()
        is_update = request_id in airtable_records
        op = "update" if is_update else "insert"
        segment: List[Tuple[str, Any]] = []
        while i < n:
            _, row2 = rows_list[i]
            rid2 = str(row2["Request-ID"]).strip()
            is_u2 = rid2 in airtable_records
            o2 = "update" if is_u2 else "insert"
            if o2 != op:
                break
            segment.append((rid2, row2))
            i += 1
            if len(segment) == AIRTABLE_BATCH_SIZE:
                break
        yield op, segment


def run_batched_dataframe(
    df: "pd.DataFrame",
    airtable_records: dict,
    progress: Optional[Callable[[int, int, str, str], None]],
) -> Tuple[int, int, List[dict]]:
    """
    Process rows in file order, batching up to AIRTABLE_BATCH_SIZE consecutive inserts
    or the same for updates per Airtable request. Calls progress(current, total, request_id, phase)
    after each row succeeds, where phase is "insert" or "update".
    """
    rows_list: List[Tuple[Any, Any]] = list(df.iterrows())
    n = len(rows_list)
    if n == 0:
        return 0, 0, []

    inserted = 0
    updated = 0
    errors: List[dict] = []
    pos = 0
    for op, segment in iter_request_segments(rows_list, airtable_records):
        if op == "update":
            upd_items = [
                (airtable_records[rid]["record_id"], r) for rid, r in segment
            ]
            if not _update_batch(upd_items):
                for rid, r in segment:
                    err = update_existing_row(airtable_records[rid]["record_id"], r)
                    if err:
                        errors.append(
                            {
                                "request_id": rid,
                                "op": "update_last_seen",
                                "detail": err[:2000],
                            }
                        )
                    else:
                        updated += 1
                        pos += 1
                        if progress:
                            progress(
                                pos,
                                n,
                                rid,
                                "update",
                            )
                        print("Updated Last Seen + fields:", rid)
            else:
                for rid, r in segment:
                    updated += 1
                    pos += 1
                    if progress:
                        progress(
                            pos,
                            n,
                            rid,
                            "update",
                        )
                    print("Updated batch, Last Seen:", rid)
        else:
            to_create = [r for _, r in segment]
            if not _create_batch(to_create):
                for rid, r in segment:
                    err = create_airtable_record(r)
                    if err:
                        errors.append(
                            {
                                "request_id": rid,
                                "op": "insert",
                                "detail": err[:2000],
                            }
                        )
                    else:
                        inserted += 1
                        pos += 1
                        if progress:
                            progress(
                                pos,
                                n,
                                rid,
                                "insert",
                            )
            else:
                for rid, r in segment:
                    inserted += 1
                    pos += 1
                    if progress:
                        progress(
                            pos,
                            n,
                            rid,
                            "insert",
                        )
                    print("Inserted batch:", rid)

    return inserted, updated, errors


def sync_excel(excel_file: FileSource) -> Dict[str, Any]:
    """
    Run the full Airtable sync. Only rows where MSP Owner contains the word Eric
    are processed. Returns a dict suitable for JSON and CLI summary.
    """
    if not AIRTABLE_TOKEN:
        raise ValueError("AIRTABLE_TOKEN is missing from .env")

    (
        df,
        rows_in_sheet,
        rows_skipped,
        rows_skipped_short,
    ) = prepare_eric_dataframe(excel_file)
    n = len(df)
    if n == 0:
        return _build_result(
            0, 0, [], set(), set(), rows_in_sheet, 0, rows_skipped, rows_skipped_short
        )

    airtable_records = get_airtable_records()
    excel_ids = set(df["Request-ID"])
    airtable_ids = set(airtable_records.keys())
    inserted, updated, errors = run_batched_dataframe(
        df, airtable_records, progress=None
    )

    missing_from_excel = sorted(airtable_ids - excel_ids)
    missing = len(missing_from_excel)

    print("\nRecords in Airtable but missing from this Excel (Eric filter):")
    for rid in missing_from_excel:
        print("-", rid)

    print("\nSync summary")
    print("Inserted:", inserted)
    print("Existing skipped / Last Seen updated:", updated)
    print("Rows in sheet (with Request-ID):", rows_in_sheet)
    print("Rows skipped (MSP Owner is not Eric):", rows_skipped)
    print(
        "Rows skipped (duration <", MIN_DURATION_MONTHS_SYNC, "mths):", rows_skipped_short
    )
    print("Missing from Eric export:", missing)

    return _build_result(
        inserted, updated, errors, excel_ids, airtable_ids,
        rows_in_sheet, n, rows_skipped, rows_skipped_short,
    )


def main(excel_file: FileSource = EXCEL_FILE):
    result = sync_excel(excel_file)
    if not result["ok"]:
        print(
            "\nCompleted with Airtable errors on",
            len(result["errors"]),
            "row(s). Check the printed messages above.",
        )


if __name__ == "__main__":
    main()