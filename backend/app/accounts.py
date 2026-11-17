"""Closing an account.

Anyone can delete their own account. What goes with it depends on who they
are: an employee, or one of several owners, takes only their sign-in with
them, and the records they entered stay with the business. The last owner
takes the business too, because a business nobody can administer is only
data nobody can reach.

The shared demo accounts are refused. Their password is printed on the sign-in
page, so anyone could otherwise delete what the next visitor came to see.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .core.roles import OWNER
from .models import Business, Expense, Party, Product, Purchase, Sale, User
from .seed import DEMO_EMAIL, DEMO_SHOP_ACCOUNTS

PROTECTED_EMAILS = frozenset({DEMO_EMAIL, *(a["email"] for a in DEMO_SHOP_ACCOUNTS.values())})


@dataclass(slots=True)
class DeletionPlan:
    """What deleting one account would do, worked out before anything is."""

    allowed: bool
    #: Why not, when it is not allowed.
    reason: str | None = None
    deletesBusiness: bool = False
    businessName: str | None = None
    #: Other people who would lose their sign-in with the business.
    otherMembers: int = 0
    #: How much would go with the business: label -> count.
    records: dict[str, int] = field(default_factory=dict)


def _count(db: Session, model, business_id: uuid.UUID) -> int:
    return db.scalar(select(func.count(model.id)).where(model.business_id == business_id)) or 0


def plan_deletion(db: Session, user: User) -> DeletionPlan:
    if user.email.lower() in PROTECTED_EMAILS:
        return DeletionPlan(
            allowed=False,
            reason="This is a shared demo account, so it cannot be deleted. "
            "Create your own account to try this.",
        )
    if user.business_id is None:
        return DeletionPlan(
            allowed=False,
            reason="A platform account does not belong to a business and cannot be deleted here.",
        )

    business = db.scalar(select(Business).where(Business.id == user.business_id))
    name = business.name if business else None

    other_owner = db.scalar(
        select(User.id)
        .where(User.business_id == user.business_id, User.role == OWNER, User.id != user.id)
        .limit(1)
    )
    if user.role != OWNER or other_owner is not None:
        return DeletionPlan(allowed=True, businessName=name)

    others = (
        db.scalar(
            select(func.count(User.id)).where(
                User.business_id == user.business_id, User.id != user.id
            )
        )
        or 0
    )
    return DeletionPlan(
        allowed=True,
        deletesBusiness=True,
        businessName=name,
        otherMembers=others,
        records={
            "sales": _count(db, Sale, user.business_id),
            "purchases": _count(db, Purchase, user.business_id),
            "expenses": _count(db, Expense, user.business_id),
            "products": _count(db, Product, user.business_id),
            "parties": _count(db, Party, user.business_id),
        },
    )


def delete_business(db: Session, business_id: uuid.UUID) -> None:
    """Remove a business and everything that hangs off it.

    Every tenant-owned table points at its business with ON DELETE CASCADE, the
    members included, so one delete is the whole job and the database does it
    in one transaction.
    """
    db.execute(delete(Business).where(Business.id == business_id))
    db.commit()


def delete_account(db: Session, user: User) -> DeletionPlan:
    """Carry out `plan_deletion`. Raises ValueError when it is not allowed."""
    plan = plan_deletion(db, user)
    if not plan.allowed:
        raise ValueError(plan.reason or "This account cannot be deleted.")
    if plan.deletesBusiness and user.business_id is not None:
        delete_business(db, user.business_id)
    else:
        # Events and inbox rows this person authored keep their place in the
        # log; their author column is ON DELETE SET NULL.
        db.execute(delete(User).where(User.id == user.id))
        db.commit()
    return plan
