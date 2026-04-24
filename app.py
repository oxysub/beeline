import os
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from bl_upload import AIRTABLE_TOKEN, iter_sync_ndjson

_DIR = Path(__file__).resolve().parent
_STATIC = _DIR / "static"
_INDEX = _STATIC / "index.html"

app = FastAPI(
    title="Beeline Airtable sync",
    description="Upload a Beeline Excel export to sync with Airtable.",
)


@app.get("/")
def index():
    if not _INDEX.is_file():
        raise HTTPException(status_code=404, detail="static/index.html missing")
    return FileResponse(_INDEX, media_type="text/html; charset=utf-8")


@app.post("/api/sync")
async def api_sync(file: UploadFile = File(...)):
    if not AIRTABLE_TOKEN:
        raise HTTPException(
            status_code=500,
            detail="Server is not configured: add AIRTABLE_TOKEN to beeline/.env (or the environment).",
        )
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file was uploaded.")
    lower = file.filename.lower()
    if not lower.endswith((".xlsx", ".xls")):
        raise HTTPException(
            status_code=400,
            detail="Please upload an Excel file (.xlsx or .xls).",
        )
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    return StreamingResponse(
        iter_sync_ndjson(data),
        media_type="application/x-ndjson; charset=utf-8",
    )


# Optional: for reverse proxies
@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=True)
