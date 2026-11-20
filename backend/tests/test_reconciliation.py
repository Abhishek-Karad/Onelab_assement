import csv
import io
from decimal import Decimal

from app.reconciliation import reconcile_csvs


PLATFORM = ["transaction_id","transaction_date","transaction_type","amount","currency","payment_status","customer_id","original_transaction_id"]
BANK = ["settlement_id","transaction_id","settlement_date","transaction_type","amount","currency","settlement_status","batch_id"]


def csv_bytes(fields, rows):
    out = io.StringIO(); writer = csv.DictWriter(out, fieldnames=fields); writer.writeheader(); writer.writerows(rows); return out.getvalue().encode()


def pair(platform_rows, bank_rows, **kwargs):
    return reconcile_csvs(csv_bytes(PLATFORM, platform_rows), csv_bytes(BANK, bank_rows), **kwargs)


def p(tid="TXN001", date="2026-01-01T10:00:00+00:00", amount="100.00", kind="PAYMENT", status="SUCCESS", original=""):
    return dict(transaction_id=tid, transaction_date=date, transaction_type=kind, amount=amount, currency="INR", payment_status=status, customer_id="CUS001", original_transaction_id=original)


def b(sid="SET001", tid="TXN001", date="2026-01-03T10:00:00+00:00", amount="100.00", kind="PAYMENT"):
    return dict(settlement_id=sid, transaction_id=tid, settlement_date=date, transaction_type=kind, amount=amount, currency="INR", settlement_status="SETTLED", batch_id="BAT001")


def test_clean_match_and_signed_refund_totals():
    result = pair([p(), p("REF001", "2026-01-02T10:00:00+00:00", "25.00", "REFUND", original="TXN001")], [b(), b("SET002", "REF001", "2026-01-04T10:00:00+00:00", "25.00", "REFUND")])
    assert result["summary"]["total_discrepancies"] == 0
    assert result["financial_summary"]["net_expected_amount"] == "75.00"
    assert result["financial_summary"]["net_actual_amount"] == "75.00"


def test_missing_settlement_pending_and_overdue():
    pending = pair([p()], [], as_of_date=__import__('datetime').date(2026, 1, 2))
    overdue = pair([p()], [], as_of_date=__import__('datetime').date(2026, 1, 5))
    assert pending["matched_transactions"][0]["status"] == "PENDING"
    assert overdue["matched_transactions"][0]["status"] == "OVERDUE"


def test_unmatched_duplicate_cross_month_and_amount_mismatch():
    result = pair([p()], [b("SET001", date="2026-02-01T10:00:00+00:00", amount="99.99"), b("SET002", tid="UNKNOWN", amount="5.00")])
    assert "CROSS_MONTH_SETTLEMENT" in result["matched_transactions"][0]["findings"]
    assert "AMOUNT_MISMATCH" in result["matched_transactions"][0]["findings"]
    assert result["summary"]["unmatched_bank_record_count"] == 1


def test_validation_and_failed_payment_exclusion():
    result = pair([p(status="FAILED")], [])
    assert result["summary"]["total_discrepancies"] == 0
    bad = pair([p()], [b()])
    assert bad["validation_errors"] == []
    invalid = reconcile_csvs(b"transaction_id\n", csv_bytes(BANK, [b()]))
    assert invalid["validation_errors"]
