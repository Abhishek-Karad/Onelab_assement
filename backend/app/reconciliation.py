"""Deterministic, rule-based reconciliation for the two Phase 1 CSV datasets."""

import csv
import io
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any


PLATFORM_FIELDS = ["transaction_id", "transaction_date", "transaction_type", "amount", "currency", "payment_status", "customer_id", "original_transaction_id"]
BANK_FIELDS = ["settlement_id", "transaction_id", "settlement_date", "transaction_type", "amount", "currency", "settlement_status", "batch_id"]
PAYMENT_TYPES = {"PAYMENT", "REFUND"}
PAYMENT_STATUSES = {"SUCCESS", "FAILED"}
SETTLEMENT_STATUSES = {"SETTLED", "FAILED"}
ZERO = Decimal("0.00")


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"))


def _fmt(value: Decimal) -> str:
    return format(_money(value), ".2f")


def _parse_date(value: str, row_number: int, field: str, errors: list[dict[str, Any]]) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        errors.append({"row": row_number, "field": field, "code": "INVALID_DATE", "message": f"{field} must be an ISO 8601 datetime"})
        return None


def _parse_amount(value: str, row_number: int, errors: list[dict[str, Any]]) -> Decimal | None:
    try:
        amount = Decimal(value.strip())
        if not amount.is_finite() or amount <= 0:
            raise InvalidOperation
        return _money(amount)
    except (AttributeError, InvalidOperation):
        errors.append({"row": row_number, "field": "amount", "code": "INVALID_AMOUNT", "message": "amount must be a positive decimal"})
        return None


def _read_csv(content: bytes, dataset: str, fields: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    errors: list[dict[str, Any]] = []
    if not content:
        return [], [{"row": 0, "code": "EMPTY_FILE", "message": f"{dataset} CSV is empty"}], True
    try:
        text = content.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
    except (UnicodeDecodeError, csv.Error) as exc:
        return [], [{"row": 0, "code": "UNREADABLE_CSV", "message": str(exc)}], True
    headers = reader.fieldnames or []
    missing = [field for field in fields if field not in headers]
    if missing:
        return [], [{"row": 1, "code": "MISSING_COLUMNS", "message": f"Missing required columns: {', '.join(missing)}"}], True
    rows: list[dict[str, Any]] = []
    for row_number, raw in enumerate(reader, 2):
        if None in raw:
            errors.append({"row": row_number, "code": "UNEXPECTED_FIELDS", "message": "Row contains more fields than the header"})
        missing_values = [field for field in fields if raw.get(field) is None]
        if missing_values:
            errors.append({"row": row_number, "code": "MISSING_ROW_FIELDS", "message": f"Row is missing values for: {', '.join(missing_values)}"})
        if not any((value or "").strip() for value in raw.values() if value is not None):
            errors.append({"row": row_number, "code": "EMPTY_ROW", "message": "Row is empty"})
            continue
        row = {field: (raw.get(field) or "").strip() for field in fields}
        row["_row"] = row_number
        id_field = "transaction_id" if dataset == "platform" else "settlement_id"
        if not row[id_field]:
            errors.append({"row": row_number, "field": id_field, "code": "EMPTY_IDENTIFIER", "message": f"{id_field} must not be empty"})
        if dataset == "bank" and not row["transaction_id"]:
            errors.append({"row": row_number, "field": "transaction_id", "code": "EMPTY_IDENTIFIER", "message": "transaction_id must not be empty"})
        parsed_date = _parse_date(row["transaction_date" if dataset == "platform" else "settlement_date"], row_number, "transaction_date" if dataset == "platform" else "settlement_date", errors)
        amount = _parse_amount(row["amount"], row_number, errors)
        row["_date"] = parsed_date
        row["_amount"] = amount
        if row["currency"] != "INR": errors.append({"row": row_number, "field": "currency", "code": "CURRENCY_MISMATCH", "message": "currency must be INR"})
        if row["transaction_type"] not in PAYMENT_TYPES: errors.append({"row": row_number, "field": "transaction_type", "code": "INVALID_TRANSACTION_TYPE", "message": "transaction_type must be PAYMENT or REFUND"})
        status_field = "payment_status" if dataset == "platform" else "settlement_status"
        allowed = PAYMENT_STATUSES if dataset == "platform" else SETTLEMENT_STATUSES
        if row[status_field] not in allowed: errors.append({"row": row_number, "field": status_field, "code": "INVALID_STATUS", "message": f"{status_field} has an invalid value"})
        if dataset == "platform" and row["transaction_type"] == "REFUND" and not row["original_transaction_id"]:
            errors.append({"row": row_number, "field": "original_transaction_id", "code": "MISSING_REFUND_REFERENCE", "message": "Refunds require original_transaction_id"})
        rows.append(row)
    return rows, errors, False


def _public(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None: return None
    return {key: value for key, value in row.items() if not key.startswith("_")}


def _duplicate_findings(rows, identifier, dataset):
    groups = defaultdict(list)
    for row in rows: groups[row[identifier]].append(row)
    findings = []
    for key, group in groups.items():
        if key and len(group) > 1:
            findings.append({"code": "DUPLICATE_PLATFORM_RECORD" if dataset == "platform" else "DUPLICATE_BANK_RECORD", "dataset": dataset, "identifier": key, "occurrences": len(group), "rows": [r["_row"] for r in group], "amount": _fmt(sum((r["_amount"] or ZERO for r in group), ZERO))})
    return groups, findings


def _event_finding(code, explanation, amount_difference=ZERO, **extra):
    return {"code": code, "explanation": explanation, "amount_difference": _fmt(amount_difference), **extra}


def reconcile_csvs(platform_bytes: bytes, bank_bytes: bytes, *, as_of_date: date | None = None, settlement_delay_days: int = 2, rounding_threshold: Decimal = Decimal("0.01")) -> dict[str, Any]:
    if settlement_delay_days < 1 or settlement_delay_days > 30: raise ValueError("settlement_delay_days must be between 1 and 30")
    if rounding_threshold < 0: raise ValueError("rounding_threshold must be non-negative")
    platform, platform_errors, platform_fatal = _read_csv(platform_bytes, "platform", PLATFORM_FIELDS)
    bank, bank_errors, bank_fatal = _read_csv(bank_bytes, "bank", BANK_FIELDS)
    validation_errors = [{"dataset": "platform", **e} for e in platform_errors] + [{"dataset": "bank", **e} for e in bank_errors]
    if platform_fatal or bank_fatal:
        return _empty_report(validation_errors, settlement_delay_days, rounding_threshold, as_of_date)
    platform_by_id, platform_duplicates = _duplicate_findings(platform, "transaction_id", "platform")
    bank_by_settlement, bank_duplicates = _duplicate_findings(bank, "settlement_id", "bank")
    bank_by_transaction = defaultdict(list)
    for row in bank: bank_by_transaction[row["transaction_id"]].append(row)
    for transaction_id, group in bank_by_transaction.items():
        if transaction_id and len(group) > 1:
            bank_duplicates.append({"code": "DUPLICATE_BANK_TRANSACTION", "dataset": "bank", "identifier": transaction_id, "occurrences": len(group), "rows": [r["_row"] for r in group], "amount": _fmt(sum((r["_amount"] or ZERO for r in group), ZERO))})
    duplicate_findings = platform_duplicates + bank_duplicates
    max_date = max((r["_date"].date() for r in platform if r["_date"]), default=date.today())
    cutoff = as_of_date or max_date
    events: list[dict[str, Any]] = []
    discrepancies: list[dict[str, Any]] = []
    matched_count = 0
    pending_amount = ZERO
    overdue_amount = ZERO
    duplicate_amount = sum((Decimal(f["amount"]) for f in duplicate_findings if f.get("dataset") == "bank"), ZERO)
    for p in platform:
        tid = p["transaction_id"]
        matches = bank_by_transaction.get(tid, [])
        findings = []
        if len(platform_by_id.get(tid, [])) > 1:
            findings.append(_event_finding("DUPLICATE_PLATFORM_RECORD", "Multiple platform rows share this transaction identifier; matching is ambiguous."))
        parent_valid = p["_date"] is not None and p["_amount"] is not None and p["transaction_type"] in PAYMENT_TYPES and p["currency"] == "INR"
        if not parent_valid:
            findings.append(_event_finding("INVALID", "The platform row contains invalid data and cannot be safely reconciled."))
        original = None
        if p["transaction_type"] == "REFUND":
            original_group = platform_by_id.get(p["original_transaction_id"], [])
            original = original_group[0] if len(original_group) == 1 else None
            if original is None: findings.append(_event_finding("ORPHAN_REFUND", "The refund references an original payment that does not exist uniquely."))
            elif original["transaction_type"] != "PAYMENT" or original["payment_status"] != "SUCCESS": findings.append(_event_finding("INVALID_REFUND_REFERENCE", "The refund does not reference a successful payment."))
            elif p["_date"] and original["_date"] and p["_date"] < original["_date"]: findings.append(_event_finding("INVALID_REFUND_DATE", "The refund date precedes its original payment date."))
            elif p["_amount"] and original["_amount"] and p["_amount"] > original["_amount"]: findings.append(_event_finding("REFUND_EXCEEDS_PAYMENT", "The refund amount exceeds the original payment amount."))
        if p["transaction_type"] == "PAYMENT" and p["payment_status"] == "FAILED":
            if matches:
                findings.append(_event_finding("UNEXPECTED_SETTLEMENT_FOR_FAILED_PAYMENT", "A failed platform payment has a bank settlement."))
            else:
                status = "MATCHED"
        bank_row = matches[0] if len(matches) == 1 else None
        if len(matches) > 1:
            findings.append(_event_finding("DUPLICATE_BANK_TRANSACTION", "Multiple bank settlements reference this transaction; matching is ambiguous."))
            status = "REVIEW_REQUIRED"
        elif p["payment_status"] == "SUCCESS" and bank_row is None:
            deadline = (p["_date"] + timedelta(days=settlement_delay_days)).date() if p["_date"] else cutoff
            if cutoff < deadline:
                status = "PENDING"; pending_amount += p["_amount"] or ZERO; findings.append(_event_finding("MISSING_SETTLEMENT", "No settlement exists yet and the expected settlement deadline has not passed."))
            else:
                status = "OVERDUE"; overdue_amount += p["_amount"] or ZERO; findings.append(_event_finding("MISSING_SETTLEMENT", "No settlement exists and the expected settlement deadline has passed."))
        elif not (p["transaction_type"] == "PAYMENT" and p["payment_status"] == "FAILED"):
            status = "MATCHED" if bank_row else "REVIEW_REQUIRED"
        difference = ZERO
        if bank_row:
            matched_count += 1
            if bank_row["_date"] and p["_date"] and bank_row["_date"] < p["_date"]: findings.append(_event_finding("INVALID_SETTLEMENT_DATE", "The settlement timestamp precedes the transaction timestamp."))
            if bank_row["_date"] and p["_date"] and bank_row["_date"].date() > (p["_date"] + timedelta(days=settlement_delay_days)).date(): findings.append(_event_finding("LATE_SETTLEMENT", "The settlement occurs after the configured calendar-day settlement window."))
            if bank_row["_date"] and p["_date"] and bank_row["_date"].month != p["_date"].month: findings.append(_event_finding("CROSS_MONTH_SETTLEMENT", "The settlement occurred in a different calendar month from the transaction."))
            if bank_row["transaction_type"] != p["transaction_type"]: findings.append(_event_finding("TYPE_MISMATCH", "Platform and bank transaction types do not match."))
            if bank_row["currency"] != p["currency"]: findings.append(_event_finding("CURRENCY_MISMATCH", "Platform and bank currencies do not match."))
            if p["_amount"] is not None and bank_row["_amount"] is not None:
                difference = bank_row["_amount"] - p["_amount"]
                if difference != ZERO: findings.append(_event_finding("AMOUNT_MISMATCH", "Platform and bank amounts differ.", difference))
            if any(f["code"] in {"INVALID_SETTLEMENT_DATE", "TYPE_MISMATCH", "CURRENCY_MISMATCH", "DUPLICATE_BANK_TRANSACTION", "DUPLICATE_PLATFORM_RECORD", "UNEXPECTED_SETTLEMENT_FOR_FAILED_PAYMENT"} for f in findings): status = "REVIEW_REQUIRED"
            elif difference != ZERO: status = "MISMATCH"
            elif any(f["code"] == "LATE_SETTLEMENT" for f in findings): status = "REVIEW_REQUIRED"
            else: status = "MATCHED"
        if any(f["code"] == "INVALID" for f in findings): status = "INVALID"
        if any(f["code"] == "DUPLICATE_PLATFORM_RECORD" for f in findings): status = "REVIEW_REQUIRED"
        if any(f["code"] == "ORPHAN_REFUND" for f in findings): status = "REVIEW_REQUIRED"
        if findings:
            for finding in findings:
                if finding["code"] not in {"MISSING_SETTLEMENT"} or status not in {"PENDING", "OVERDUE"}:
                    discrepancies.append({"transaction_id": tid, "transaction_type": p["transaction_type"], "status": status, **finding, "platform_record": _public(p), "bank_record": _public(bank_row)})
        event = {"transaction_id": tid, "transaction_type": p["transaction_type"], "status": status, "findings": [f["code"] for f in findings], "platform_amount": _fmt(p["_amount"] or ZERO), "bank_amount": _fmt(bank_row["_amount"] if bank_row and bank_row["_amount"] else ZERO), "difference": _fmt(difference), "transaction_date": p["transaction_date"], "settlement_date": bank_row["settlement_date"] if bank_row else None, "explanation": "; ".join(f["explanation"] for f in findings) or "Platform and bank records match.", "financially_reconciled": status == "MATCHED" and difference == ZERO, "platform_record": _public(p), "bank_records": [_public(r) for r in matches]}
        events.append(event)
    platform_ids = set(platform_by_id)
    for b in bank:
        if b["transaction_id"] not in platform_ids:
            finding = _event_finding("UNMATCHED_BANK_TRANSACTION", "The bank settlement references an unknown platform transaction.", b["_amount"] or ZERO)
            discrepancies.append({"transaction_id": b["transaction_id"], "transaction_type": b["transaction_type"], "status": "UNMATCHED_BANK", **finding, "platform_record": None, "bank_record": _public(b)})
            events.append({"transaction_id": b["transaction_id"], "transaction_type": b["transaction_type"], "status": "UNMATCHED_BANK", "findings": [finding["code"]], "platform_amount": "0.00", "bank_amount": _fmt(b["_amount"] or ZERO), "difference": _fmt(b["_amount"] or ZERO), "transaction_date": None, "settlement_date": b["settlement_date"], "explanation": finding["explanation"], "financially_reconciled": False, "platform_record": None, "bank_records": [_public(b)]})
    eligible_platform = [p for p in platform if p["payment_status"] == "SUCCESS" and p["_amount"] is not None]
    expected = sum(((p["_amount"] if p["transaction_type"] == "PAYMENT" else -p["_amount"]) for p in eligible_platform), ZERO)
    settled = [b for b in bank if b["settlement_status"] == "SETTLED" and b["_amount"] is not None]
    actual = sum(((b["_amount"] if b["transaction_type"] == "PAYMENT" else -b["_amount"]) for b in settled), ZERO)
    matched_differences = sum((Decimal(e["difference"]) * (1 if e["transaction_type"] == "PAYMENT" else -1) for e in events if e["status"] in {"MATCHED", "MISMATCH", "REVIEW_REQUIRED"} and e["bank_records"] and len(e["bank_records"]) == 1), ZERO)
    small_mismatches = [e for e in events if e["bank_records"] and len(e["bank_records"]) == 1 and e["difference"] != "0.00" and abs(Decimal(e["difference"])) <= rounding_threshold]
    aggregate_rounding = ZERO
    if len(small_mismatches) >= 2:
        aggregate_rounding = sum((Decimal(e["difference"]) * (1 if e["transaction_type"] == "PAYMENT" else -1) for e in small_mismatches), ZERO)
        discrepancies.append({"transaction_id": None, "transaction_type": "AGGREGATE", "status": "REVIEW_REQUIRED", "code": "AGGREGATE_ROUNDING_DIFFERENCE", "explanation": "Small matched-record amount differences accumulate across transactions; this is a rounding candidate, not proof of a rounding method.", "amount_difference": _fmt(aggregate_rounding), "platform_record": None, "bank_record": None})
    for finding in duplicate_findings: discrepancies.append({"transaction_id": finding.get("identifier"), "transaction_type": "DUPLICATE", "status": "REVIEW_REQUIRED", "code": finding["code"], "explanation": f"{finding['occurrences']} records share identifier {finding['identifier']}.", "amount_difference": finding["amount"], "platform_record": None, "bank_record": None, **finding})
    total_discrepancies = len(discrepancies)
    summary = {"platform_transaction_count": len(platform), "successful_payment_count": sum(p["transaction_type"] == "PAYMENT" and p["payment_status"] == "SUCCESS" for p in platform), "failed_payment_count": sum(p["transaction_type"] == "PAYMENT" and p["payment_status"] == "FAILED" for p in platform), "refund_count": sum(p["transaction_type"] == "REFUND" for p in platform), "bank_settlement_count": len(bank), "matched_event_count": matched_count, "pending_settlement_count": sum(e["status"] == "PENDING" for e in events), "overdue_settlement_count": sum(e["status"] == "OVERDUE" for e in events), "unmatched_bank_record_count": sum(e["status"] == "UNMATCHED_BANK" for e in events), "duplicate_record_count": len(duplicate_findings), "orphan_refund_count": sum("ORPHAN_REFUND" in e["findings"] for e in events), "cross_month_settlement_count": sum("CROSS_MONTH_SETTLEMENT" in e["findings"] for e in events), "amount_mismatch_count": sum("AMOUNT_MISMATCH" in e["findings"] for e in events), "total_discrepancies": total_discrepancies}
    financial = {"total_successful_platform_payment_amount": _fmt(sum((p["_amount"] for p in eligible_platform if p["transaction_type"] == "PAYMENT"), ZERO)), "total_successful_platform_refund_amount": _fmt(sum((p["_amount"] for p in eligible_platform if p["transaction_type"] == "REFUND"), ZERO)), "net_expected_amount": _fmt(expected), "total_settled_bank_payment_amount": _fmt(sum((b["_amount"] for b in settled if b["transaction_type"] == "PAYMENT"), ZERO)), "total_settled_bank_refund_amount": _fmt(sum((b["_amount"] for b in settled if b["transaction_type"] == "REFUND"), ZERO)), "net_actual_amount": _fmt(actual), "raw_net_difference": _fmt(actual - expected), "matched_record_amount_difference": _fmt(matched_differences), "pending_settlement_amount": _fmt(pending_amount), "overdue_settlement_amount": _fmt(overdue_amount), "duplicate_related_amount": _fmt(duplicate_amount), "unmatched_bank_amount": _fmt(sum((b["_amount"] for b in bank if b["transaction_id"] not in platform_ids and b["_amount"]), ZERO)), "aggregate_rounding_difference": _fmt(aggregate_rounding), "unresolved_financial_difference": _fmt(actual - expected - aggregate_rounding), "bridge": {"raw_difference": _fmt(actual - expected), "matched_record_difference": _fmt(matched_differences), "aggregate_rounding_candidate": _fmt(aggregate_rounding), "unresolved_exposure": _fmt(actual - expected - aggregate_rounding)}}
    return {"summary": summary, "financial_summary": financial, "discrepancies": discrepancies, "matched_transactions": events, "validation_errors": validation_errors, "reconciliation_metadata": {"as_of_date": cutoff.isoformat(), "settlement_delay_days": settlement_delay_days, "rounding_threshold": _fmt(rounding_threshold), "timezone": "UTC", "calendar_day_assumption": True, "status_precedence": ["INVALID", "UNMATCHED_BANK", "REVIEW_REQUIRED", "MISMATCH", "OVERDUE", "PENDING", "MATCHED"]}}


def _empty_report(errors, delay, threshold, as_of):
    return {"summary": {"platform_transaction_count": 0, "bank_settlement_count": 0, "total_discrepancies": 0}, "financial_summary": {}, "discrepancies": [], "matched_transactions": [], "validation_errors": errors, "reconciliation_metadata": {"as_of_date": as_of.isoformat() if as_of else None, "settlement_delay_days": delay, "rounding_threshold": _fmt(threshold)}}
