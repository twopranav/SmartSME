"""Deleting your own account.

Who you are decides what goes with it: an employee, or one of several owners,
takes only their sign-in; the last owner takes the business. The shared demo
accounts and platform accounts are refused.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from tests.conftest import _create_platform_user, _new_client, _signed_in

PASSWORD = "owner-password-1234"


def _owner(business_name: str = "Closing Down Stores"):
    """A throwaway business with its owner signed in: (client, email)."""
    client = _new_client()
    client.__enter__()
    email = f"closing-{uuid.uuid4().hex[:10]}@smoketest.dev"
    r = client.post(
        "/api/auth/sign-up",
        json={"businessName": business_name, "name": "Owner", "email": email, "password": PASSWORD},
    )
    assert r.status_code == 201, r.text
    return client, email


def _member(owner_client, role: str):
    """Someone who joined `owner_client`'s business by invitation: (client, email)."""
    email = f"{role}-{uuid.uuid4().hex[:10]}@smoketest.dev"
    invite = owner_client.post("/api/users/invites", json={"email": email, "role": role})
    assert invite.status_code == 201, invite.text
    client = _new_client()
    client.__enter__()
    accepted = client.post(
        "/api/auth/accept-invite",
        json={"token": invite.json()["token"], "name": role.title(), "password": PASSWORD},
    )
    assert accepted.status_code == 201, accepted.text
    return client, email


def _can_sign_in(email: str) -> bool:
    with _new_client() as c:
        return (
            c.post("/api/auth/sign-in", json={"email": email, "password": PASSWORD}).status_code
            == 200
        )


def _business_exists(name: str) -> bool:
    from app.core.db import SessionLocal
    from app.models import Business

    with SessionLocal() as db:
        return db.scalar(select(Business.id).where(Business.name == name)) is not None


def test_it_needs_a_session(anon):
    with _new_client() as c:
        assert c.get("/api/auth/account-deletion").status_code == 401
        assert c.post("/api/auth/delete-account", json={"password": "x"}).status_code == 401


def test_the_last_owner_is_told_the_business_goes_too(anon):
    name = f"Preview Stores {uuid.uuid4().hex[:6]}"
    owner, _ = _owner(name)
    _member(owner, "employee")
    product = owner.post(
        "/api/products", json={"name": "Soap", "sellingPrice": 30, "purchasePrice": 20, "stock": 9}
    )
    assert product.status_code == 201, product.text
    sale = owner.post(
        "/api/sales",
        json={
            "items": [
                {
                    "productId": product.json()["id"],
                    "description": "Soap",
                    "quantity": 2,
                    "unitPrice": 30,
                }
            ]
        },
    )
    assert sale.status_code == 201, sale.text

    plan = owner.get("/api/auth/account-deletion").json()
    assert plan["allowed"] is True
    assert plan["deletesBusiness"] is True
    assert plan["businessName"] == name
    assert plan["otherMembers"] == 1
    assert plan["records"]["sales"] == 1
    assert plan["records"]["products"] == 1
    # Looking is not deleting.
    assert _business_exists(name)


def test_a_wrong_password_deletes_nothing(anon):
    name = f"Wrong Password Stores {uuid.uuid4().hex[:6]}"
    owner, email = _owner(name)
    r = owner.post("/api/auth/delete-account", json={"password": "not-the-password"})
    assert r.status_code == 403
    assert "password" in r.json()["detail"].lower()
    assert _business_exists(name)
    assert _can_sign_in(email)
    # And the session that tried is still a session.
    assert owner.get("/api/auth/me").status_code == 200


def test_the_last_owner_takes_the_business_and_its_members(anon, client):
    name = f"Last Owner Stores {uuid.uuid4().hex[:6]}"
    owner, owner_email = _owner(name)
    employee, employee_email = _member(owner, "employee")
    owner.post("/api/products", json={"name": "Soap", "sellingPrice": 30, "stock": 5})

    r = owner.post("/api/auth/delete-account", json={"password": PASSWORD})
    assert r.status_code == 200, r.text
    assert r.json()["deletesBusiness"] is True

    assert not _business_exists(name)
    assert not _can_sign_in(owner_email)
    assert not _can_sign_in(employee_email)
    # Both cookies now point at nobody.
    assert owner.get("/api/auth/me").status_code == 401
    assert employee.get("/api/auth/me").status_code == 401
    # Somebody else's business is exactly where it was.
    assert client.get("/api/dashboard").status_code == 200
    assert _business_exists("Smoke Test Traders")


def test_an_employee_takes_only_their_sign_in(anon):
    name = f"Staff Leaves Stores {uuid.uuid4().hex[:6]}"
    owner, owner_email = _owner(name)
    employee, employee_email = _member(owner, "employee")
    # Something the employee recorded, which has to outlive them.
    expense = employee.post(
        "/api/expenses", json={"category": "Tea", "description": "Tea for staff", "amount": 40}
    )
    assert expense.status_code == 201, expense.text

    plan = employee.get("/api/auth/account-deletion").json()
    assert plan == {
        "allowed": True,
        "reason": None,
        "deletesBusiness": False,
        "businessName": name,
        "otherMembers": 0,
        "records": {},
    }
    r = employee.post("/api/auth/delete-account", json={"password": PASSWORD})
    assert r.status_code == 200, r.text

    assert not _can_sign_in(employee_email)
    assert _can_sign_in(owner_email)
    assert _business_exists(name)
    rows = owner.get("/api/expenses").json()["rows"]
    assert [e["description"] for e in rows] == ["Tea for staff"]
    # The event the employee caused is still in the log, now without an author.
    events = owner.get("/api/events").json()
    assert any(e["type"] == "EXPENSE_ADDED" for e in events["rows"])
    members = owner.get("/api/users").json()["members"]
    assert [m["email"] for m in members] == [owner_email]


def test_one_of_two_owners_leaves_the_business_standing(anon):
    name = f"Two Owner Stores {uuid.uuid4().hex[:6]}"
    first, first_email = _owner(name)
    second, _ = _member(first, "owner")

    plan = first.get("/api/auth/account-deletion").json()
    assert plan["allowed"] is True
    assert plan["deletesBusiness"] is False

    assert first.post("/api/auth/delete-account", json={"password": PASSWORD}).status_code == 200
    assert _business_exists(name)
    assert not _can_sign_in(first_email)
    assert second.get("/api/dashboard").status_code == 200
    # Now the second owner is the last one, and is told so.
    assert second.get("/api/auth/account-deletion").json()["deletesBusiness"] is True


def test_a_platform_account_is_refused(anon):
    credentials = _create_platform_user("admin")
    admin = _signed_in(credentials)
    plan = admin.get("/api/auth/account-deletion").json()
    assert plan["allowed"] is False
    assert "platform" in plan["reason"].lower()
    r = admin.post("/api/auth/delete-account", json={"password": credentials["password"]})
    assert r.status_code == 403
    assert admin.get("/api/auth/me").status_code == 200


def test_the_shared_demo_accounts_are_refused(anon):
    """Their password is printed on the sign-in page. Without this, any
    visitor could delete what the next one came to see."""
    from app.accounts import PROTECTED_EMAILS, delete_account, plan_deletion
    from app.core.db import SessionLocal
    from app.core.security import hash_password
    from app.models import Business, User
    from app.seed import DEMO_EMAIL, DEMO_SHOP_ACCOUNTS

    assert DEMO_EMAIL in PROTECTED_EMAILS
    assert {a["email"] for a in DEMO_SHOP_ACCOUNTS.values()} <= PROTECTED_EMAILS

    with SessionLocal() as db:
        # The suite does not seed the demo business, so stand one up.
        existing = db.scalar(select(User).where(User.email == DEMO_EMAIL))
        business = None
        if existing is None:
            business = Business(name=f"Demo Stand-in {uuid.uuid4().hex[:6]}")
            db.add(business)
            db.flush()
            existing = User(
                business_id=business.id,
                email=DEMO_EMAIL,
                name="Demo Owner",
                password_hash=hash_password("demo1234"),
                role="owner",
            )
            db.add(existing)
            db.commit()
        try:
            plan = plan_deletion(db, existing)
            assert plan.allowed is False
            assert "demo" in plan.reason.lower()
            try:
                delete_account(db, existing)
            except ValueError:
                pass
            else:
                raise AssertionError("the demo account was deleted")
            assert db.scalar(select(User.id).where(User.email == DEMO_EMAIL)) is not None

            with _new_client() as c:
                signed_in = c.post(
                    "/api/auth/sign-in", json={"email": DEMO_EMAIL, "password": "demo1234"}
                )
                if signed_in.status_code == 200:
                    r = c.post("/api/auth/delete-account", json={"password": "demo1234"})
                    assert r.status_code == 403
        finally:
            if business is not None:
                from app.accounts import delete_business

                delete_business(db, business.id)
