from fastapi import FastAPI, File, Form, HTTPException, Path, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from .config import GeneratorConfig, DEFAULTS
from .generator import generate_dataset, _serialize
from .serialization import build_files, zip_bytes
from .reconciliation import reconcile_csvs
from .gemini_analysis import analyze_discrepancies
from pydantic import BaseModel
import io
from datetime import date
from decimal import Decimal, InvalidOperation
import os


class DiscrepancyAnalysisRequest(BaseModel):
    discrepancies: list[dict] = []
    transactions: list[dict] = []


app = FastAPI(title="Payment Reconciliation Dataset Generator", version="1.0.0")

# Vite serves the frontend from one of these local origins during development.
# Keep this explicit instead of allowing every origin.
allowed_origins = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:4173",
    "http://127.0.0.1:4173",
]
frontend_origin = os.getenv("FRONTEND_ORIGIN")
if frontend_origin:
    allowed_origins.append(frontend_origin.rstrip("/"))

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)

@app.get("/api/health")
def health(): return {"status": "ok"}

@app.get("/api/generator/defaults")
def defaults(): return DEFAULTS.model_dump(mode="json")

def run(config):
    try: return generate_dataset(config)
    except ValueError as exc: raise HTTPException(422, str(exc))

@app.post("/api/generator/preview")
def preview(config: GeneratorConfig):
    platform, settlements, truth = run(config)
    return {"summary":{"platform_records":len(platform),"bank_records":len(settlements),"payment_amount":format(sum((r.amount for r in platform if r.transaction_type == 'PAYMENT'), start=0), '.2f'),"settlement_amount":format(sum((r.amount for r in settlements), start=0), '.2f'),"expected_discrepancies":len(truth)},"platform_preview":[_serialize(r) for r in platform[:10]],"settlement_preview":[_serialize(r) for r in settlements[:10]],"ground_truth":truth}

@app.post("/api/generator/download")
def download(config: GeneratorConfig):
    platform, settlements, truth = run(config); data = zip_bytes(build_files(platform, settlements, truth, config))
    return StreamingResponse(io.BytesIO(data), media_type="application/zip", headers={"Content-Disposition":"attachment; filename=payment_reconciliation_dataset.zip"})

@app.post("/api/generator/download/{filename}")
def download_file(config: GeneratorConfig, filename: str = Path(...)):
    allowed = {"platform_transactions.csv", "bank_settlements.csv", "ground_truth.json"}
    if filename not in allowed:
        raise HTTPException(404, "Unknown export file")
    platform, settlements, truth = run(config)
    files = build_files(platform, settlements, truth, config)
    media = "application/json" if filename.endswith(".json") else "text/csv"
    return StreamingResponse(io.BytesIO(files[filename]), media_type=media, headers={"Content-Disposition": f"attachment; filename={filename}"})

@app.post("/api/reconcile")
async def reconcile(
    platform_file: UploadFile = File(...),
    bank_file: UploadFile = File(...),
    as_of_date: str | None = Form(None),
    settlement_delay_days: int = Form(2),
    rounding_threshold: str = Form("0.01"),
):
    max_size = 10 * 1024 * 1024
    for upload in (platform_file, bank_file):
        if upload.filename and not upload.filename.lower().endswith(".csv"):
            raise HTTPException(415, f"{upload.filename} must be a CSV file")
    platform_content = await platform_file.read()
    bank_content = await bank_file.read()
    if len(platform_content) > max_size or len(bank_content) > max_size:
        raise HTTPException(413, "CSV files must be 10 MB or smaller")
    parsed_as_of = None
    if as_of_date:
        try: parsed_as_of = date.fromisoformat(as_of_date)
        except ValueError: raise HTTPException(422, "as_of_date must use YYYY-MM-DD format")
    try: threshold = Decimal(rounding_threshold)
    except InvalidOperation: raise HTTPException(422, "rounding_threshold must be a decimal amount")
    try:
        return reconcile_csvs(platform_content, bank_content, as_of_date=parsed_as_of, settlement_delay_days=settlement_delay_days, rounding_threshold=threshold)
    except ValueError as exc:
        raise HTTPException(422, str(exc))


@app.post("/api/reconcile/analyze")
def analyze_reconciliation(request: DiscrepancyAnalysisRequest):
    if not request.discrepancies:
        return {"model": None, "analyses": [], "record_count": 0}
    try:
        return analyze_discrepancies(request.discrepancies, request.transactions)
    except RuntimeError as exc:
        message = str(exc)
        status = 503 if "not configured" in message else 502
        raise HTTPException(status, message)

