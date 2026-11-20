import calendar
import random
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from .config import GeneratorConfig
from .models import PlatformTransaction, Settlement

CENT = Decimal("0.01")


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _timestamp(rng: random.Random, start, end):
    span = int((end - start).total_seconds())
    return (start + timedelta(seconds=rng.randint(0, span))).replace(tzinfo=timezone.utc)


def generate_platform_transactions(config: GeneratorConfig, rng: random.Random):
    rows: list[PlatformTransaction] = []
    successful: list[PlatformTransaction] = []
    for index in range(1, config.payment_count + 1):
        amount = money(Decimal(str(rng.uniform(float(config.min_amount), float(config.max_amount)))))
        status = "FAILED" if rng.random() < config.failed_payment_percentage / 100 else "SUCCESS"
        row = PlatformTransaction(f"TXN{index:06d}", _timestamp(rng, datetime.combine(config.start_date, time.min), datetime.combine(config.end_date, time.max)), "PAYMENT", amount, "INR", status, f"CUS{rng.randint(1, config.payment_count):06d}")
        rows.append(row)
        if status == "SUCCESS": successful.append(row)
    refund_target_count = round(len(successful) * config.refund_percentage / 100)
    if config.inject_discrepancies and config.orphan_refund_count > 0:
        refund_target_count = max(0, refund_target_count - config.orphan_refund_count)
    targets = rng.sample(successful, min(refund_target_count, len(successful)))
    for index, payment in enumerate(targets, 1):
        rows.append(PlatformTransaction(f"REF{index:06d}", payment.transaction_date + timedelta(minutes=rng.randint(5, 1440)), "REFUND", payment.amount, "INR", "SUCCESS", payment.customer_id, payment.transaction_id))
    if config.inject_discrepancies:
        for index in range(1, config.orphan_refund_count + 1):
            source = rng.choice(successful) if successful else rows[0]
            rows.append(PlatformTransaction(f"REF{len(targets)+index:06d}", source.transaction_date + timedelta(minutes=2 + index), "REFUND", source.amount, "INR", "SUCCESS", source.customer_id, f"TXN999{index:03d}"))
    return rows


def derive_settlements(rows, config: GeneratorConfig, rng: random.Random):
    eligible = [r for r in rows if r.payment_status == "SUCCESS"]
    settlements = []
    for index, row in enumerate(eligible, 1):
        delay = rng.randint(config.min_settlement_delay, config.max_settlement_delay)
        settlements.append(Settlement(f"SET{index:06d}", row.transaction_id, row.transaction_date + timedelta(days=delay), row.transaction_type, row.amount, row.currency, "SETTLED", ""))
    by_day = {}
    for settlement in settlements:
        day = settlement.settlement_date.date()
        by_day.setdefault(day, len(by_day) + 1)
        settlement.batch_id = f"BAT{by_day[day]:06d}"
    return settlements


def _serialize(row):
    return {k: (v.isoformat() if isinstance(v, datetime) else format(v, ".2f") if isinstance(v, Decimal) else v) for k, v in row.__dict__.items()}


def generate_dataset(config: GeneratorConfig):
    rng = random.Random(config.seed)
    platform = generate_platform_transactions(config, rng)
    settlements = derive_settlements(platform, config, rng)
    ground_truth: list[dict[str, Any]] = []
    if config.inject_discrepancies:
        inject_discrepancies(platform, settlements, config, ground_truth, rng)
    validate_dataset(platform, settlements, config, ground_truth)
    return platform, settlements, ground_truth


def inject_discrepancies(platform, settlements, config, truth, rng):
    parents = {r.transaction_id: r for r in platform}
    if config.cross_month_count:
        candidates = [s for s in settlements if s.transaction_type == "PAYMENT" and s.settlement_date.month == parents[s.transaction_id].transaction_date.month and s.settlement_date.day >= 25]
        if len(candidates) < config.cross_month_count: raise ValueError("Not enough end-of-month eligible payments for cross-month discrepancies")
        for s in rng.sample(candidates, config.cross_month_count):
            old = s.settlement_date
            next_month = old.month % 12 + 1; year = old.year + (old.month == 12)
            day = min(old.day, calendar.monthrange(year, next_month)[1])
            s.settlement_date = s.settlement_date.replace(year=year, month=next_month, day=day)
            truth.append({"type":"cross_month_settlement", "transaction_id":s.transaction_id, "settlement_id":s.settlement_id, "original_values":{"settlement_date":old.isoformat()}, "modified_values":{"settlement_date":s.settlement_date.isoformat()}, "expected_classification":"timing_difference"})
    if config.rounding_count:
        if len(settlements) < config.rounding_count: raise ValueError("Not enough settlements for rounding discrepancies")
        for s in rng.sample(settlements, config.rounding_count):
            old = s.amount; delta = CENT if rng.choice([-1, 1]) > 0 else -CENT; s.amount = money(old + delta)
            truth.append({"type":"aggregate_rounding_discrepancy", "transaction_id":s.transaction_id, "settlement_id":s.settlement_id, "original_values":{"amount":format(old, '.2f')}, "modified_values":{"amount":format(s.amount, '.2f')}, "expected_classification":"amount_difference", "expected_financial_difference":format(s.amount-old, '.2f')})
    if config.duplicate_count:
        if len(settlements) < config.duplicate_count: raise ValueError("Not enough settlements for duplicate discrepancies")
        for s in rng.sample(settlements, config.duplicate_count):
            duplicate = Settlement(**s.__dict__); duplicate.batch_id = s.batch_id; settlements.append(duplicate)
            truth.append({"type":"duplicate_settlement_entry", "transaction_id":s.transaction_id, "settlement_id":s.settlement_id, "original_values":_serialize(s), "modified_values":_serialize(duplicate), "expected_classification":"duplicate"})
    if config.orphan_refund_count:
        for row in platform:
            if row.original_transaction_id and row.original_transaction_id.startswith("TXN999"):
                truth.append({"type":"orphan_refund", "transaction_id":row.transaction_id, "original_transaction_id":row.original_transaction_id, "original_values":{}, "modified_values":{"original_transaction_id":row.original_transaction_id}, "expected_classification":"orphan_refund"})


def validate_dataset(platform, settlements, config, truth):
    ids = [r.transaction_id for r in platform]
    if len(ids) != len(set(ids)): raise ValueError("Platform transaction IDs are not unique")
    known = {r.transaction_id: r for r in platform}
    for s in settlements:
        if s.transaction_id not in known: raise ValueError("Settlement references an unknown transaction")
        parent = known[s.transaction_id]
        if s.transaction_type != parent.transaction_type or s.currency != "INR" or s.settlement_date <= parent.transaction_date: raise ValueError("Settlement integrity check failed")
    for r in platform:
        if r.transaction_type == "REFUND" and r.original_transaction_id and r.original_transaction_id.startswith("TXN") and r.original_transaction_id in known and known[r.original_transaction_id].payment_status != "SUCCESS": raise ValueError("Refund references failed payment")
