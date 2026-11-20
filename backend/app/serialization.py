import csv, io, json, zipfile
from dataclasses import asdict

PLATFORM_FIELDS = ["transaction_id","transaction_date","transaction_type","amount","currency","payment_status","customer_id","original_transaction_id"]
SETTLEMENT_FIELDS = ["settlement_id","transaction_id","settlement_date","transaction_type","amount","currency","settlement_status","batch_id"]

def csv_bytes(rows, fields):
    out = io.StringIO(); writer = csv.DictWriter(out, fieldnames=fields); writer.writeheader()
    for row in rows: writer.writerow(asdict(row))
    return out.getvalue().encode()

def build_files(platform, settlements, truth, config):
    gt = {"random_seed": config.seed, "generator_configuration": config.model_dump(mode="json"), "total_platform_records": len(platform), "total_bank_records": len(settlements), "injected_discrepancies": truth}
    return {"platform_transactions.csv": csv_bytes(platform, PLATFORM_FIELDS), "bank_settlements.csv": csv_bytes(settlements, SETTLEMENT_FIELDS), "ground_truth.json": json.dumps(gt, indent=2, default=str).encode()}

def zip_bytes(files):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items(): z.writestr(name, data)
    return out.getvalue()
