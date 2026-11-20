from datetime import date
from decimal import Decimal
from pydantic import BaseModel, Field, model_validator


class GeneratorConfig(BaseModel):
    payment_count: int = Field(100, ge=1, le=100_000)
    start_date: date = date(2024, 1, 1)
    end_date: date = date(2024, 3, 31)
    failed_payment_percentage: float = Field(5, ge=0, le=100)
    refund_percentage: float = Field(10, ge=0, le=100)
    min_amount: Decimal = Field(Decimal("100.00"), ge=Decimal("0.01"))
    max_amount: Decimal = Field(Decimal("10000.00"), ge=Decimal("0.01"))
    min_settlement_delay: int = Field(1, ge=1, le=30)
    max_settlement_delay: int = Field(2, ge=1, le=30)
    seed: int = 42
    inject_discrepancies: bool = False
    cross_month_count: int = Field(0, ge=0)
    rounding_count: int = Field(0, ge=0)
    duplicate_count: int = Field(0, ge=0)
    orphan_refund_count: int = Field(0, ge=0)

    @model_validator(mode="after")
    def validate_ranges(self):
        if self.start_date > self.end_date:
            raise ValueError("Start date must be on or before end date")
        if self.min_amount > self.max_amount:
            raise ValueError("Minimum amount must not exceed maximum amount")
        if self.min_settlement_delay > self.max_settlement_delay:
            raise ValueError("Minimum settlement delay must not exceed maximum delay")
        if not self.inject_discrepancies and any((self.cross_month_count, self.rounding_count, self.duplicate_count, self.orphan_refund_count)):
            raise ValueError("Discrepancy counts require discrepancy injection to be enabled")
        return self

    @property
    def total_discrepancies(self):
        return self.cross_month_count + self.rounding_count + self.duplicate_count + self.orphan_refund_count if self.inject_discrepancies else 0


DEFAULTS = GeneratorConfig()
