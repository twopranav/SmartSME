# HTTP API

Every endpoint, and the conventions they share.

[&larr; Back to the README](../README.md)

---

## API reference

Interactive docs at **<http://localhost:8000/docs>**. Summary:

| Area | Endpoints |
|---|---|
| Meta | `GET /api/health` (public) |
| Auth | `POST /api/auth/sign-in` · `sign-up` · `sign-out` · `GET /api/auth/me` · `GET /api/auth/demo-accounts` (public) · `GET /api/auth/account-deletion` · `POST /api/auth/delete-account` (`password`) |
| Dashboard | `GET /api/dashboard` |
| Sales | `GET/POST /api/sales` · `GET /api/sales/{id}` · `POST {id}/payment` (`amount`, `mode`, `reference`, `details`, optional `date`) · `{id}/cancel` · `PATCH {id}/date` · `PATCH {id}/due-date` |
| Purchases | same shape under `/api/purchases` |
| Products | `GET/POST /api/products` · `PUT/DELETE {id}` · `POST {id}/adjust` |
| Parties | `GET/POST /api/parties` · `PUT/DELETE {id}` · `POST {id}/settle` · `POST /settle-all/{kind}` |
| Expenses | `GET/POST /api/expenses` · `DELETE {id}` |
| Smart Input | `GET /api/input/status` · `POST /parse-text` · `/parse-image` · `/publish` |
| Reports | `GET /api/reports/overview` · `/revenue?days=&metric=sales|purchases|expenses` · `/preview` · `/download` |
| Workflow | `GET /api/workflow` · `POST /rules` · `PUT/DELETE /rules/{id}` · `POST /rules/{id}/toggle` |
| Events | `GET /api/events` · `POST /events/{id}/replay` · `POST /events/drain` |
| Notifications | `GET /api/notifications` · `/unread-count` · `POST {id}/read` · `/read-all` |
| Settings | `GET/PUT /api/settings` |

Requests use camelCase JSON to match the React code; the database stays
snake_case. Domain validation errors come back as `400 {"detail": "..."}`.

`GET /api/auth/account-deletion` says what deleting the signed-in account would
remove (`allowed`, `reason`, `deletesBusiness`, `businessName`, `otherMembers`,
`records`), and `POST /api/auth/delete-account` does it once the password is
sent again. A wrong password, a shared demo account and a platform account are
each a 403. See [Deleting an account](auth.md#deleting-an-account).

`GET /api/auth/demo-accounts` needs no session. It lists the shared demo
sign-ins that exist on this instance, which is how the sign-in page knows
whether to offer the two [demo shops](features.md#demo-shops-two-businesses-to-compare).

`GET /api/reports/overview` carries `totals.netProfit` (gross profit less
expenses, negative for a loss) beside `totals.grossProfit`.

`POST {id}/payment` takes an optional `date` (`yyyy-mm-dd`), the day the money
moved; left out, it is now. It is refused with a 400 when it is before the
sale or purchase, after today, or when the document itself is dated ahead of
today. The comparison is by calendar day, so the document's own day is always
allowed. See [Which day a payment may carry](features.md#which-day-a-payment-may-carry).

---

