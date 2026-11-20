import io, zipfile
from app.config import GeneratorConfig
from app.generator import generate_dataset
from app.serialization import build_files, zip_bytes

def test_clean_integrity_and_seed():
    c = GeneratorConfig(payment_count=40, seed=7)
    a = generate_dataset(c); b = generate_dataset(c)
    assert [x.__dict__ for x in a[0]] == [x.__dict__ for x in b[0]]
    platform, settlements, _ = a
    assert all(s.transaction_id in {p.transaction_id for p in platform} for s in settlements)
    assert not any(p.payment_status == "FAILED" and p.transaction_id in {s.transaction_id for s in settlements} for p in platform)

def test_discrepancies_and_zip():
    c = GeneratorConfig(payment_count=300, start_date="2024-01-01", end_date="2024-02-29", inject_discrepancies=True, cross_month_count=1, rounding_count=2, duplicate_count=1, orphan_refund_count=1)
    platform, settlements, truth = generate_dataset(c)
    assert {x["type"] for x in truth} == {"cross_month_settlement","aggregate_rounding_discrepancy","duplicate_settlement_entry","orphan_refund"}
    assert any(p.original_transaction_id and p.original_transaction_id.startswith("TXN999") for p in platform)
    files = build_files(platform, settlements, truth, c); names = zipfile.ZipFile(io.BytesIO(zip_bytes(files))).namelist()
    assert set(names) == set(files)

def test_schema_and_validation():
    try: GeneratorConfig(min_amount=10, max_amount=1)
    except ValueError: pass
    else: assert False
