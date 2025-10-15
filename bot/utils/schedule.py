from __future__ import annotations

from datetime import date
from typing import List

from dateutil.relativedelta import relativedelta

from bot.models import Payment

BASE_MONTHLY = 10_000
SECOND_MONTH_EXTRA = 17_000
THIRD_MONTH_EXTRA = 25_000


def build_payment_schedule(start_date: date, total_amount: int) -> List[Payment]:
    payments: List[Payment] = []
    remaining = total_amount
    month = 0

    while remaining > 0:
        month += 1
        if month == 1:
            amount = min(remaining, BASE_MONTHLY)
        elif month == 2:
            amount = min(remaining, BASE_MONTHLY + SECOND_MONTH_EXTRA)
        elif month == 3:
            amount = min(remaining, BASE_MONTHLY + THIRD_MONTH_EXTRA)
        else:
            amount = min(remaining, BASE_MONTHLY)

        due_date = start_date + relativedelta(months=month - 1)
        payments.append(Payment(month_index=month, due_date=due_date, amount=amount))
        remaining -= amount

    return payments
