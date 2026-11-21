"""Committed bills I enter manually."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Bill
from app.timeutil import add_months

RECURRENCES = ("none", "weekly", "biweekly", "monthly", "yearly")


@dataclass
class BillOccurrence:
    bill: Bill
    due: date

    @property
    def amount_cents(self) -> int:
        return self.bill.amount_cents


def occurrences(bill: Bill, start: date, end: date) -> list[date]:
    """Due dates of `bill` within [start, end]."""
    out: list[date] = []
    if bill.recurrence == "none":
        return [bill.due_date] if start <= bill.due_date <= end else []
    d, i = bill.due_date, 0
    while d <= end and i < 2000:
        if d >= start:
            out.append(d)
        i += 1
        if bill.recurrence == "weekly":
            d = bill.due_date + timedelta(weeks=i)
        elif bill.recurrence == "biweekly":
            d = bill.due_date + timedelta(weeks=2 * i)
        elif bill.recurrence == "monthly":
            d = add_months(bill.due_date, i)
        elif bill.recurrence == "yearly":
            d = add_months(bill.due_date, 12 * i)
        else:
            break
    return out


def due_within(session: Session, start: date, days: int) -> list[BillOccurrence]:
    end = start + timedelta(days=days)
    out = []
    for b in session.scalars(select(Bill).where(Bill.active.is_(True)).order_by(Bill.due_date)):
        out += [BillOccurrence(b, d) for d in occurrences(b, start, end)]
    return sorted(out, key=lambda o: (o.due, o.bill.id))
