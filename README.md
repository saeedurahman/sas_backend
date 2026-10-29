# POS SaaS Backend (FastAPI + PostgreSQL)

See [ARCHITECTURE.md](ARCHITECTURE.md) for the module layout and [schema/SCHEMA.md](schema/SCHEMA.md) for the database.

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
cp .env.example .env                                # then fill in DATABASE_URL and the two JWT secrets
alembic upgrade head
uvicorn main:app --reload --port 8000
```

`.env` is git-ignored and must never be committed. Generate secrets with
`python -c "import secrets; print(secrets.token_urlsafe(48))"`.

## Environment variables

`.env.example` lists every key; the table below explains the security-relevant ones.

| Variable | Default | Purpose |
|---|---|---|
| `ENV` | `development` | Set to `production` to enable startup safety checks (below) |
| `DEBUG` | `false` | Must be `false` in production |
| `DATABASE_URL` | required | Postgres URL (`postgresql+asyncpg://...`) |
| `TEST_DATABASE_URL` | required for tests | Separate Postgres database used only by the test suite. Tests refuse to run if it is unset or points at `DATABASE_URL`'s database |
| `JWT_SECRET_KEY`, `JWT_REFRESH_SECRET_KEY` | required | Distinct secrets, 32+ chars in production |
| `ALLOWED_ORIGINS` | localhost dev origins | Comma-separated CORS origins. Required in production, `*` rejected |
| `LOGIN_REQUIRE_BUSINESS_SLUG` | `false` | `true` makes password login require `business_slug` |
| `MAX_LOGIN_ATTEMPTS` / `LOCKOUT_MINUTES` | `5` / `15` | Account lockout |
| `RATE_LIMIT_ENABLED` | `true` | Must stay `true` in production |
| `RATE_LIMIT_LOGIN_IP` | `10/minute` | Per client IP on `/auth/login` |
| `RATE_LIMIT_LOGIN_ACCOUNT` | `10/minute` | Per business+phone on `/auth/login` |
| `RATE_LIMIT_PIN_IP` / `RATE_LIMIT_PIN_USER` | `30/minute` / `5/minute` | `/auth/login/pin` |
| `RATE_LIMIT_REFRESH_IP` | `30/minute` | `/auth/refresh` |
| `RATE_LIMIT_REGISTER_IP` | `3/hour` | `/auth/register` |
| `RATE_LIMIT_PIN_CHANGE_USER` / `RATE_LIMIT_PASSWORD_CHANGE_USER` | `5/minute` | Self-service PIN/password changes and admin resets |
| `RATE_LIMIT_CHECK_ALERTS_USER` | `6/minute` | `/notifications/check-alerts` |
| `TRUST_FORWARDED_FOR` | `false` | Use the first `X-Forwarded-For` address as the client IP. Enable only behind a proxy you control that overwrites the header |
| `MAX_REQUEST_BODY_BYTES` | `2097152` | Larger bodies get `413` |
| `LOG_LEVEL` | `INFO` | JSON logs to stdout; bodies, headers, query strings and secrets are never logged |
| `REGISTRATION_CAPTCHA_ENABLED` | `false` | Require `captcha_token` on `/auth/register` |
| `CAPTCHA_VERIFY_URL` / `CAPTCHA_SECRET` | Turnstile URL / empty | Any Turnstile, hCaptcha or reCAPTCHA `siteverify` endpoint |
| `TRIAL_DAYS` / `GRACE_DAYS` | `14` / `3` | Trial length for new businesses; read-only days after a paid period ends |
| `PLATFORM_JWT_SECRET` | empty (API disabled) | Signs platform admin tokens; 32+ chars and different from the tenant secrets in production |
| `PLATFORM_TOKEN_EXPIRE_MINUTES` | `60` | Admin token lifetime |
| `PLATFORM_MAX_LOGIN_ATTEMPTS` / `PLATFORM_LOCKOUT_MINUTES` | `5` / `30` | Persisted admin lockout |
| `PLATFORM_ADMIN_RATE_LIMITS` | defaults below | `login_ip=10/minute,login_account=5/minute,api=300/minute` overrides |
| `LICENSE_SIGNING_PRIVATE_KEY` / `LICENSE_PUBLIC_KEY` | empty | Ed25519 key pair (`scripts/generate_license_keypair.py`); set both or neither |

Rate limits use the format `<count>/<second|minute|hour|day>`. Exceeding one returns `429` with a
`Retry-After` header. The limiter is **in-process**: with several workers or instances each keeps its own
counters, so also enforce limits at the reverse proxy / load balancer.

### Production startup checks

With `ENV=production` the app refuses to start when any of these hold:
a JWT secret is shorter than 32 characters or looks like a placeholder, the two JWT secrets are equal,
`ALLOWED_ORIGINS` is empty or contains `*`, `DEBUG=true`, `RATE_LIMIT_ENABLED=false`, or captcha is enabled
without `CAPTCHA_SECRET`. `/docs`, `/redoc` and `/openapi.json` are disabled and HSTS is sent.

## Authentication notes

- **Password login** `POST /auth/login` accepts an optional `business_slug`. Send it: it scopes the phone lookup
  to one business. Without it, a phone used by more than one business gets `409`.
- **Refresh tokens rotate.** `POST /auth/refresh` returns a new `refresh_token`; the one you sent is dead.
  Replaying a used token revokes the whole session (token family). Password or PIN changes and logout
  revoke refresh tokens (access tokens remain valid until they expire, at most `ACCESS_TOKEN_EXPIRE_HOURS`).
- **PINs**: new PINs are 6-8 digits. Existing 4-6 digit PINs keep working until changed. Changing your own PIN
  requires `current_password` or `current_pin`; an owner can set a staff PIN without it.
- **Passwords**: `POST /auth/change-password` (current password required).

### Forgot / reset password (current design)

There is no self-service "forgot password" (no email/SMS provider is integrated). Staff who forget their
password ask an owner or manager (`users.update`) to call `PUT /users/{id}/password`. That sets a new password,
clears any lockout and signs the user out everywhere; only an owner may reset an owner. An owner who is locked
out needs operator-level recovery (database or a support runbook) until an OTP channel (SMS/WhatsApp) is added.
That flow should issue a short-lived single-use reset token, rate limit it with the helpers in
`app/core/rate_limit.py`, and revoke sessions on use, mirroring `reset_user_password`.

## Tests

The suite registers throw-away tenants, so it runs against its own database and never against
`DATABASE_URL`. Set `TEST_DATABASE_URL` (environment or `.env`) to a different database, then create
and migrate it once (re-run after adding migrations):

```bash
python scripts/create_test_db.py           # creates the database if missing, runs alembic upgrade head
python scripts/ci_check.py                 # static checks + unit tests, no database needed
python scripts/ci_check.py --integration   # also boots isolated servers against TEST_DATABASE_URL
pytest tests/security                      # the security suite directly
```

pytest exits immediately when `TEST_DATABASE_URL` is unset or is the same database as `DATABASE_URL`.
The suite starts its own uvicorn processes (random ports, custom limits) via `tests/helpers/server.py`,
pointed at the test database, so it does not need or disturb a server you already have running. If you
set `API_BASE` to a server you started yourself, run that server with `DATABASE_URL` set to the test
database; the suite cannot check that.

To remove leftover test tenants from a development database (names starting with `POSTest`, or
`VoidRace` / `CancelRace` / `FIFO` followed by a number):

```bash
python scripts/cleanup_test_data.py            # dry run: per-table row counts, deletes nothing
python scripts/cleanup_test_data.py --apply    # delete them and all their rows in one transaction
```

## Subscription, platform admin and licenses (Phase 3)

Every business gets every feature with no limits. A **plan** is only a price tier stored as
data (`plans`); nothing in the app is gated by plan. Only time and status gate access.

### Subscription states

| State | When | API behaviour |
|---|---|---|
| `trial` | `status=trial` and `trial_ends_at` in the future (`TRIAL_DAYS`, default 14) | full access |
| `active` | paid period running (`subscription_ends_at` in the future) | full access |
| `grace` | up to `GRACE_DAYS` (default 3) after `subscription_ends_at` | GET only; POST/PUT/PATCH/DELETE -> 402 `subscription_grace_readonly` |
| `expired` | trial over, or grace over | every call -> 402 `subscription_expired` |
| `suspended` / `cancelled` | set by an admin | every call -> 402 `subscription_suspended` / `subscription_cancelled` |

The check lives in `get_current_user` and reuses the business row it already loaded (no extra
query). Data is never deleted. Routes that must keep working are whitelisted with the
`subscription_exempt` dependency: `/auth/me`, `/auth/logout`, `/subscription/*` (login, PIN
login and refresh do not pass through the gate). Closing a shift stays possible in grace
(`allow_in_grace`).

402 body (`Content-Type: application/json`):

```json
{"code":"subscription_expired","message":"Your subscription has expired. Please renew to continue.",
 "detail":"Your subscription has expired. Please renew to continue.",
 "expired_at":"2026-10-20T10:00:00+00:00",
 "contact":{"phone":"0300-1112223","whatsapp":"0300-4445556","email":"help@pakpos.pk"}}
```

`contact` comes from the editable `platform_settings` table (admin panel -> Settings).
`GET /api/v1/subscription/status` (any signed-in user) returns plan, state, dates, `days_left`,
`in_grace`, `contact` and the payment instructions text. Login, PIN login, register and refresh
responses carry `subscription: {status, days_left}`.

### Platform admin API (`/platform/*`)

For the separate admin web panel. Not under `/api/v1`. Admin JWT only: separate secret
(`PLATFORM_JWT_SECRET`), `aud=pakpos-platform`, short expiry (`PLATFORM_TOKEN_EXPIRE_MINUTES`,
default 60; `POST /platform/auth/refresh` swaps a live token for a fresh one). Sign-in is email +
password + 6-digit TOTP; a code works once; failures lock the account (persisted in the database)
after `PLATFORM_MAX_LOGIN_ATTEMPTS`; per-IP, per-account and per-admin rate limits come from
`PLATFORM_ADMIN_RATE_LIMITS`. Every mutation writes `platform_audit_log` with admin, before/after
and IP. Disabling an admin or resetting 2FA ends their sessions immediately.

Create the first admin (prints the TOTP provisioning URI once):

```bash
PLATFORM_ADMIN_PASSWORD='a-long-passphrase' python scripts/create_platform_admin.py --email owner@example.pk --name "Owner"
```

### Licenses (one-time / offline edition)

`/platform/licenses` issues, lists, revokes, transfers and extends licenses. A key is one
pasteable line, `PPOS-<base64url(canonical JSON payload)>.<base64url(Ed25519 signature)>`; the
payload is `{v, license_id, edition, customer, machine_id, issued_at, updates_until}`. Sign with
`LICENSE_SIGNING_PRIVATE_KEY` (server only), verify with `LICENSE_PUBLIC_KEY`
(`app/licensing/keys.py` is pure and shared with the offline app). `GET /licenses/revocations`
is a public, signed list of revoked license ids.

```bash
python scripts/generate_license_keypair.py   # prints both env lines
```

### Branch isolation

Owners and business-wide staff see every branch. A user with an assigned branch, or with roles
scoped through `user_roles.branch_id`, only reaches those branches across sales, returns, shifts,
registers, stock, adjustments, waste, transfers, expenses, purchases and reports; a foreign
`branch_id` (query, body or record) is 403. `user_roles.branch_id` is honoured. A user scoped to
several branches must send `branch_id` on list calls (400 otherwise).
`/auth/me` returns `permissions_version`, a fingerprint of roles + permissions + branch scope that
changes exactly when the user's access changes.

### CI guard

`python scripts/route_permission_report.py` fails when a non-public route has no permission or
ownership dependency (allow-lists with reasons are in the script). It runs in
`python scripts/ci_check.py`.

## Offline sync (Phase 4a)

POS devices keep a local copy of the catalog, work offline, and replay what they did when the
network returns. The server stays authoritative for FIFO costing, ledgers, stock and document
numbers; the device only ever supplies facts (what was sold, at what price, when).

Apply `alembic upgrade head` (revision `019_offline_sync`). Everything below is under `/api/v1`
unless stated; send `Authorization: Bearer ...` except for `/time`.

### Change feed: `change_seq` and cursors

Every synced table (`products`, `product_variations`, `barcodes`, `categories`, `brands`, `units`,
`tax_rates`, `price_lists`, `price_list_items`, `customers`, `discount_schemes`, `business_configs`,
`branches`, `cash_registers`, `users`, `roles`, and the derived `stock_levels`) has a per-business
`change_seq`. A deferred trigger stamps it **at COMMIT**, taking the business counter row lock last,
so sequence order is commit order: a pull can never skip a row that commits late. Deletes are soft
(`deleted_at`) and show up as tombstones; synced rows are never hard-deleted by the app (a unit test
enforces it). `stock_levels` is a per product+branch balance kept by a trigger on `stock_movements`
(stock itself remains a ledger). Cursors are signed, opaque and server-issued; a device clock is never used.
Because stamping happens at commit, two transactions that touch the same unlocked row at commit time can
deadlock; Postgres aborts one and the sync processor retries the op (up to 3 times).

Entities delivered: `settings` (business name/currency/timezone, a subset of the config, feature flags,
subscription status and contact), `branch`, `cash_register`, `role` (with `permission_keys`), `user`
(`id, name, roles, role_ids, branch_ids, status, is_locked, permissions_version` only), `category`,
`brand`, `unit`, `tax_rate`, `product`, `product_variation`, `barcode`, `price_list`, `price_list_item`,
`customer` (with `balance`), `discount_scheme`, `stock_level`. Entities are filtered by the caller's
permissions and branch scope. Numbers are exact decimal strings.

#### `GET /sync/pull?since=<cursor>&limit=500&branch_id=<uuid>&device_id=<id>`

`device_id` (or header `X-Device-ID`) is required. `limit` is 1..1000 (default 500). Omit `since` to start from the beginning.

```json
{
  "changes": [
    {"entity": "product", "id": "3b0f...", "op": "upsert", "seq": 118,
     "data": {"id": "3b0f...", "name": "Rice 5kg", "sku": "R5", "category_id": null, "base_unit_id": "9a1c...",
              "tax_rate_id": null, "product_type": "standard", "tracking_type": "none", "is_active": true,
              "min_stock_level": null, "created_at": "2026-10-29T08:15:00+00:00", "deleted_at": null}},
    {"entity": "stock_level", "id": "c2d4...", "op": "upsert", "seq": 119,
     "data": {"id": "c2d4...", "branch_id": "5e7a...", "product_id": "3b0f...", "variation_id": null,
              "qty": "97.0000", "last_movement_at": "2026-10-29T08:20:11+00:00"}},
    {"entity": "product", "id": "77aa...", "op": "delete", "seq": 120, "data": null}
  ],
  "next_cursor": "eyJiIjoi...Zjl9.k3mQ...",
  "has_more": false,
  "server_time": "2026-10-29T08:21:00.123456Z",
  "full_resync_required": false,
  "full_resync_reason": null,
  "permissions_version": "9f3c1a77d2b04e51",
  "entities": ["settings", "branch", "cash_register", "role", "user", "category", "..."],
  "restricted": false
}
```

* Apply the changes in order and store `next_cursor` after each page; if `has_more`, pull again at once.
* `full_resync_required: true` (with `next_cursor: null` and no changes) means: discard the local copy and run the
  snapshot. `full_resync_reason` is `cursor_expired` (older than `TOMBSTONE_RETENTION_DAYS`),
  `permissions_changed` (the user permissions_version differs from the one the cursor was issued under, so data
  they may now see or no longer see cannot be replayed), `user_changed` (cursor belongs to another user) or
  `cursor_ahead_of_server` (restored or foreign database).
* A malformed or forged cursor is `400 {"detail": {"code": "invalid_cursor", ...}}`.
* Subscription expired/suspended/cancelled: `restricted: true`, only `settings` (the expired-screen data) is
  served and the cursor does not advance, so everything missed is delivered after renewal. Grace pulls normally.

#### `GET /sync/snapshot?device_id=<id>&limit=500&page_token=<token>`

First install or full resync. Live rows only (no tombstones), keyset-paged per entity in the order above.

```json
{"changes": [{"entity": "settings", "id": "...", "op": "upsert", "seq": 3, "data": {"...": "..."}}],
 "next_page_token": "eyJj...", "has_more": true,
 "cursor": "eyJiIjoi...",
 "server_time": "2026-10-29T08:21:00Z", "permissions_version": "9f3c1a77d2b04e51",
 "entities": ["settings", "..."], "restricted": false}
```

Pass `next_page_token` back as `page_token` until `has_more` is false, then start pulling from `cursor`
(the same value on every page: the head at the moment the snapshot began; changes made while paging are
re-delivered by that first pull, and upserts are idempotent).

### `POST /sync/push`

```json
{
  "device_id": "till-front-1", "batch_id": "b-2026-10-29-0007",
  "device_name": "Front till", "app_version": "1.4.2",
  "ops": [
    {"op_id": "0d3a5c5e-...", "type": "customer.create",
     "client_created_at": "2026-10-29T07:58:00Z", "occurred_at": "2026-10-29T07:58:00Z",
     "payload": {"id": "c1c1...", "name": "Ali Traders", "phone": "0300...", "credit_limit": "5000"}},
    {"op_id": "6f2e9b0a-...", "type": "sale.create",
     "client_created_at": "2026-10-29T08:02:10Z", "occurred_at": "2026-10-29T08:02:10Z",
     "depends_on": ["0d3a5c5e-..."],
     "payload": {"id": "5a5a...", "client_receipt_no": "T1-000123", "branch_id": "5e7a...",
                 "register_shift_id": "e0e0...", "customer_id": "c1c1...", "sale_type": "pos",
                 "price_list_id": null, "discount_scheme_id": null, "notes": null,
                 "lines": [{"id": "l1l1...", "product_id": "3b0f...", "variation_id": null, "qty": "2",
                            "unit_price": "100.00", "discount_pct": "0", "discount_amount": "0",
                            "tax_rate": "5", "tax_amount": "10.00", "notes": null, "line_order": 0}],
                 "payments": [{"payment_method": "cash", "amount": "150.00", "tendered_amount": "200.00",
                               "change_given": "50.00", "reference_no": null, "paid_at": null},
                              {"payment_method": "credit", "amount": "60.00"}]}}
  ]
}
```

Response (always `200` unless the request itself is invalid; results are in request order):

```json
{"batch_id": "b-2026-10-29-0007", "server_time": "2026-10-29T08:30:00Z",
 "results": [
   {"op_id": "0d3a5c5e-...", "status": "applied", "server_id": "c1c1...", "official_number": null,
    "flags": [], "error": null, "original_status": null},
   {"op_id": "6f2e9b0a-...", "status": "applied", "server_id": "5a5a...", "official_number": "INV-20261029-0042",
    "flags": ["oversold", "cost_estimated"], "error": null, "original_status": null},
   {"op_id": "aaaa...", "status": "duplicate", "server_id": "5a5a...", "official_number": "INV-20261029-0042",
    "flags": ["oversold", "cost_estimated"], "error": null, "original_status": "applied"},
   {"op_id": "bbbb...", "status": "rejected", "server_id": null, "official_number": null, "flags": [],
    "error": {"code": "unknown_product", "message": "Unknown product(s): ...", "retryable": false},
    "original_status": null}
 ]}
```

Statuses: `applied`; `duplicate` (this `op_id` was processed before: the stored result is returned, with
`original_status`); `rejected`; `conflict` (valid but clashes with server state: id exists, sale already
voided, shift already closed, ...). **Retry rule:** only outcomes with `error.retryable: true` should be sent
again; they are not stored, so a re-send is evaluated afresh. Every other outcome is final and a re-send returns
the same stored answer. Reusing an `op_id` for different content is `rejected` / `idempotency_key_reused`.

Processing: ops run in order, each in its own transaction (effects, stored result and audit row commit
together, so a crash at any point leaves nothing half-applied and a retry is exact). An op whose `depends_on`
ids appear later in the same batch runs after them (results stay in request order); a dependency that was never
sent is `dependency_missing` (retryable), one that failed is `dependency_failed`. At most `SYNC_MAX_OPS` ops per
push (else `413 batch_too_large`); each payload at most `SYNC_MAX_PAYLOAD_BYTES`.

Op types. `occurred_at` is the business time (`sold_at` / `opened_at` / `closed_at`): at most `MAX_OFFLINE_DAYS`
in the past and 5 minutes in the future. Money fields are decimal strings.

| type | payload | needs | server_id | official_number |
|---|---|---|---|---|
| `customer.create` | `id`, `name`, `phone?`, `email?`, `tax_id?`, `address_line1?`, `city?`, `credit_limit?` | `customers.create` | customer id | |
| `shift.open` | `id`, `cash_register_id`, `opening_float?`, `notes?` | `shifts.open` | shift id | |
| `shift.close` | `shift_id`, `actual_cash`, `notes?` | `shifts.close` | shift id | |
| `sale.create` | see the example above | `sales.create` | sale id | `INV-YYYYMMDD-NNNN` |
| `sale.void` | `sale_id`, `reason?` (only a synced sale) | `sales.void` | sale id | sale number |
| `customer.payment` | `id?` (ledger id), `customer_id`, `amount`, `payment_method?`, `register_shift_id?`, `reference_no?`, `notes?` | `customers.update` | ledger entry id | |
| `expense.create` | as `POST /expenses` | `expenses.create` | expense id | `EXP-...` |
| `return.create` | as `POST /returns` (`sale_id`, `lines[{sale_line_id, qty}]`, `refund_payments`, ...) | `sales.returns.create` | return id | `RET-...` |

Held orders are not synced (`unsupported_op_type`). Clients generate the ids of customers, shifts, sales and sale
lines (`id` is the client UUID); inserts are duplicate-safe. `return.create` needs the user's open shift at sync
time (`no_open_shift`, retryable). `sale.void` follows the online rule: the sale's shift must still be open.

#### Offline sale acceptance (frozen totals)

The customer already has a receipt, so the server stores line prices, tax, discounts and payments exactly as sent
and never rejects a sale over a money difference. It recomputes on the server and records differences in
`sales.sync_flags` (also in the push result and in `GET /sync/flags`):

| flag | meaning |
|---|---|
| `price_mismatch` | a line unit price differs from the catalog price (price list) |
| `tax_mismatch` | a line tax rate or amount differs from the product configured rate |
| `oversold` | stock went negative (allowed for replayed sales while `ALLOW_NEGATIVE_STOCK_ON_SYNC=true`) |
| `credit_limit_exceeded` | the credit portion pushes the customer past their limit |
| `cost_estimated` | FIFO layers could not cover the quantity; the last known cost was used for the rest |
| `shift_mismatch` | the shift is unknown, belongs to another user or branch, or was not open at `occurred_at`; the sale is kept but no cash is posted into a drawer that is not the caller |
| `subscription_expired` | accepted while the subscription was expired, suspended or cancelled |

FIFO consumption runs normally. Every flagged sale gets an in-app notification (`sync_conflict`) and an audit
entry; every applied op gets an audit entry carrying the `device_id`. `GET /sync/flags` (owner/manager) lists
flagged sales (`?flag=oversold&branch_id=&date_from=&date_to=&skip=&limit=`). The server assigns the official
number when the sale is applied; the device number is kept as `client_receipt_no` (searchable through
`GET /sales?search=` and `/search`, and returned on sale, invoice and receipt data).

#### Error codes

| code | retryable | meaning |
|---|---|---|
| `malformed_op`, `malformed_payload` | no | the op or its payload does not validate |
| `payload_too_large` | no | over `SYNC_MAX_PAYLOAD_BYTES` |
| `unsupported_op_type` | no | unknown type, or a held order |
| `occurred_too_old`, `occurred_in_future` | no | outside the allowed window |
| `permission_denied` | no | the user lacks the op permission |
| `branch_forbidden` | no | branch outside the user scope |
| `unknown_product`, `unknown_branch` | no | referenced record does not exist |
| `unknown_register`, `unknown_customer`, `unknown_shift`, `sale_not_synced` | yes | sync the creating op first |
| `duplicate_receipt` | no | that `client_receipt_no` was synced with different content |
| `idempotency_key_reused` | no | `op_id` already used for another op |
| `id_conflict`, `id_exists`, `shift_conflict`, `shift_already_closed`, `sale_not_voidable`, `return_rejected` | no | status `conflict`: state clash |
| `insufficient_stock` | yes | only when `ALLOW_NEGATIVE_STOCK_ON_SYNC=false` |
| `dependency_missing` | yes | the `depends_on` op has not been synced |
| `dependency_failed` | as the dependency | a `depends_on` op was rejected |
| `dependency_cycle` | no | circular `depends_on` |
| `subscription_grace_readonly`, `subscription_expired`, `subscription_suspended`, `subscription_cancelled` | yes (after renewal) | see the table below |
| `no_open_shift` | yes | returns need the user open shift |
| `internal_error` | yes | unexpected failure; the rest of the batch still ran |

Whole-request errors: `403 device_revoked`, `413 batch_too_large`, `429` (per business+device limits
`RATE_LIMIT_SYNC_PUSH_DEVICE` / `RATE_LIMIT_SYNC_PULL_DEVICE`), `400 invalid_cursor`, `400 device_id_required`.
Their bodies look like `{"detail": {"code": "...", "message": "..."}}`.

#### Subscription during sync

| state | push | pull |
|---|---|---|
| trial / active | everything | everything |
| grace (read-only) | `sale.create`, `customer.payment`, `shift.close` still accepted (money already taken); other ops rejected `subscription_grace_readonly` | full |
| expired / suspended / cancelled | the same three op types accepted and flagged `subscription_expired`; others rejected with the subscription code | `settings` only (`restricted: true`) |

### Subscription lease

`GET /subscription/status`, the sign-in and refresh responses and `GET /auth/me` carry:

```json
"lease": {"v": 1, "business_id": "7d1c...", "valid_until": "2026-11-05T08:15:00Z",
          "server_time": "2026-10-29T08:15:00Z", "signature": "kq3v...base64url..."}
```

`valid_until` is `now + LEASE_DAYS`, capped by `subscription_ends_at + GRACE_DAYS` (trial: `trial_ends_at`); an
expired, suspended or cancelled business gets an already-lapsed lease. The signature is Ed25519 over the
canonical JSON (sorted keys, no whitespace) of the other four fields, made with `LEASE_SIGNING_PRIVATE_KEY` (or
the license key). Verify offline with `app.licensing.lease.verify_lease(lease, public_key, business_id=...)`
(copy the module next to `app/licensing/keys.py`): it rejects a bad signature, another business, an expired
lease, and a device clock earlier than `server_time` (rollback). `lease` is `null` when no signing key is configured.

### Offline login

There is deliberately no `/auth/offline-credentials` endpoint. Server-issued verifier material would have to be
derived from the password (which the server only sees at sign-in, and only hashed) or from the stored password
hash; handing either to a device turns a stolen phone into an offline-crackable copy of a server secret, and a
random server token the app can store is just a bearer credential with no password check. The safer design:
after a successful online sign-in the app derives its own verifier from the password the user just typed
(Argon2id, or PBKDF2-HMAC-SHA256 with at least 600k iterations, with a random per-user, per-device salt generated
on the device and the key kept in the platform secure storage) and uses it to unlock the local account. Bind it to
the lease: offline unlock is allowed only while the lease verifies. Pull supplies each user `permissions_version`
and each role `permission_keys`, so the device knows what that user may do. A password or PIN change is learned at
the next online sign-in; a revoked device is refused on its next contact.

### Devices and time

* Devices register on first pull or push (`device_id`, optional name and app version) and are listed by
  `GET /devices` (owner). `POST /devices/{device_id}/revoke` (owner) makes that device get `403 device_revoked`
  on every sync call.
* `GET /time` (also at the server root, next to `/health`; unauthenticated, rate limited per IP) returns
  `{"server_time": "2026-10-29T08:15:00.123456Z", "epoch_ms": 1793261700123}` for reachability pings and clock-skew detection.

### Sync tests

`tests/unit/test_lease.py`, `tests/unit/test_sync_units.py`, `tests/integration/sync/`,
`tests/concurrency/test_sync_concurrency.py` (all run against `TEST_DATABASE_URL`, like the rest of the suite).
