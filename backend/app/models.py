from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional


@dataclass
class PlatformTransaction:
    transaction_id: str
    transaction_date: datetime
    transaction_type: str
    amount: Decimal
    currency: str
    payment_status: str
    customer_id: str
    original_transaction_id: Optional[str] = None


@dataclass
class Settlement:
    settlement_id: str
    transaction_id: str
    settlement_date: datetime
    transaction_type: str
    amount: Decimal
    currency: str
    settlement_status: str
    batch_id: str
