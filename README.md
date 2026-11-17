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
| `PLATFORM_ENCRYPTION_KEY` | empty (derived from secret in dev) | Fernet encryption key for platform TOTP secrets at rest (`scripts/generate_platform_key.py`); required in production |
| `PLATFORM_TOKEN_EXPIRE_MINUTES` | `60` | Admin token lifetime |
| `PLATFORM_MAX_LOGIN_ATTEMPTS` / `PLATFORM_LOCKOUT_MINUTES` | `5` / `30` | Persisted admin lockout |
| `PLATFORM_ADMIN_RATE_LIMITS` | defaults below | `login_ip=10/minute,login_account=5/minute,api=300/minute` overrides |
| `LICENSE_SIGNING_PRIVATE_KEY` / `LICENSE_PUBLIC_KEY` | empty | Ed25519 key pair (`scripts/generate_license_keypair.py`); set both or neither |

| `DB_POOL_SIZE` | `10` | SQLAlchemy persistent connection pool size per process |
| `DB_MAX_OVERFLOW` | `20` | Maximum connections allowed beyond `DB_POOL_SIZE` during traffic bursts |
| `DB_POOL_TIMEOUT` | `10` | Pool checkout timeout in seconds |
| `ANALYTICS_MAX_RANGE_DAYS` | `400` | Maximum date range span allowed on analytics endpoints (returns 400 when breached) |

Rate limits use the format `<count>/<second|minute|hour|day>`. Exceeding one returns `429` with a
`Retry-After` header. The limiter is **in-process**: with several workers or instances each keeps its own
counters, so also enforce limits at the reverse proxy / load balancer.

### Connection Pool and Worker Sizing

The backend uses SQLAlchemy with an asyncpg connection pool (`app/database.py`).
- **Recommended Gunicorn/Uvicorn worker count:** `(2 x CPU_cores) + 1` (e.g., 4–8 workers on typical 2–4 core cloud instances).
- **Total Postgres connection capacity calculation:**
  $$\text{Total Connections} = \text{Workers} \times (\text{DB\_POOL\_SIZE} + \text{DB\_MAX\_OVERFLOW})$$
  With 4 workers, this represents up to $4 \times (10 + 20) = 120$ possible connections. Ensure PostgreSQL's `max_connections` (default 100 on stock Postgres) is sized accordingly, or adjust `DB_POOL_SIZE=5` and `DB_MAX_OVERFLOW=10` per worker if running high worker counts or external connection poolers (e.g. PgBouncer).
- **Per-Process In-Memory State & Rate Limiting:**
  - **Rate Limits:** In-process sliding window (`slowapi`/`limits`). Each worker tracks rate limits in its own memory. When multiple workers or instances are deployed, rate limits are per-worker; configure upstream rate limiting on your reverse proxy / API gateway (e.g. NGINX, Cloudflare) for unified edge enforcement.
  - **Dashboard Cache:** 30-second TTL cache for `/analytics/dashboard` and `/analytics/dashboard/enhanced` stored in worker memory, automatically invalidated on local sale creations, voids, returns, and sync pushes.
  - **Permission Cache:** In-memory user permission cache (`app/core/permission_cache.py`) invalidated on role/user permission mutations; avoids redundant database role lookups on hot API paths.

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
default 60; `POST /platform/auth/refresh` swaps a live token for a fresh one).

**Hardening and Safety Features:**
- **Encryption at rest:** TOTP secrets are encrypted at rest with Fernet cryptography using `PLATFORM_ENCRYPTION_KEY` (required in production startup check; generated via `python scripts/generate_platform_key.py`). Secrets are decrypted only in memory when verifying sign-in codes.
- **Sign-in & 2FA:** Sign-in is email + password + 6-digit TOTP (`POST /platform/auth/login`). TOTP codes cannot be replayed.
- **Recovery Codes:** 10 one-time backup recovery codes (`xxxx-xxxx` format) are generated at admin creation, 2FA reset, or on demand via `POST /platform/auth/recovery-codes`. Stored SHA-256 hashed in `recovery_codes_hashes`, usable instead of TOTP once each (`auth.recovery_code_used` audit entry).
- **Single-view provision:** The provisioning URI and recovery codes are returned exactly once upon admin creation or 2FA reset; they cannot be queried back later.
- **Account Lockout & Rate Limits:** Failed attempts lock the account after `PLATFORM_MAX_LOGIN_ATTEMPTS` for `PLATFORM_LOCKOUT_MINUTES`. Per-IP, per-account, and per-admin rate limits come from `PLATFORM_ADMIN_RATE_LIMITS`.
- **Change Password:** `POST /platform/auth/change-password` requires the current password, validates min 12 chars, hashes with bcrypt, logs `auth.password_change`, and immediately invalidates all other active sessions via token version increment while issuing a fresh token for the current session.
- **Real Server-side Logout:** `POST /platform/auth/logout` records the token's `jti` in `platform_revoked_tokens` until its expiry (`auth.logout` audit entry). Every request checks the revocation table, rejecting revoked tokens with `401 Unauthorized`.
- **Audit Logging:** Every platform authentication event (`auth.login`, `auth.login_failed`, `auth.password_change`, `auth.logout`, `auth.recovery_code_used`, `admin.reset_2fa`) and all resource mutations write to `platform_audit_log` with admin, entity, before/after, and client IP.
- **Tenant Export Auditing:** Tenant data exports (`GET /export/{dataset}`) record who exported which dataset, format, date range, branch, and row count into `audit_logs` (`action="export"`).

Create the first admin (prints the TOTP provisioning URI and backup recovery codes once):

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

Apply `alembic upgrade head` (latest sync revision `025_offline_sync_gaps`). Everything below is under `/api/v1`
unless stated; send `Authorization: Bearer ...` except for `/time`.

### Change feed: `change_seq` and cursors

Every synced table (`products`, `product_variations`, `barcodes`, `categories`, `brands`, `units`,
`tax_rates`, `price_lists`, `price_list_items`, `customers`, `discount_schemes`, `business_configs`,
`branches`, `cash_registers`, `users`, `roles`, derived `stock_levels`, `modifier_groups`, `modifiers`,
`product_modifier_groups`, `floor_plans`, `tables`, `register_shifts`, `sales`) has a per-business
`change_seq`. A deferred trigger stamps it **at COMMIT**, taking the business counter row lock last,
so sequence order is commit order: a pull can never skip a row that commits late. Deletes are soft
(`deleted_at`) and show up as tombstones; synced rows are never hard-deleted by the app (a unit test
enforces it). `stock_levels` is a per product+branch balance kept by a trigger on `stock_movements`
(stock itself remains a ledger). Cursors are signed, opaque and server-issued; a device clock is never used.
Because stamping happens at commit, two transactions that touch the same unlocked row at commit time can
deadlock; Postgres aborts one and the sync processor retries the op (up to 3 times).

Entities delivered: `settings` (business name, address, phone, NTN, receipt footer, tax labels, currency/timezone, feature flags,
subscription status and contact), `branch`, `cash_register`, `role` (with `permission_keys`), `user`
(`id, name, roles, role_ids, branch_ids, status, is_locked, permissions_version` only), `category`,
`brand`, `unit`, `tax_rate`, `product`, `product_variation`, `barcode`, `price_list`, `price_list_item`,
`customer` (with `balance` and `balance_owed`), `discount_scheme`, `stock_level`, `modifier_group`, `modifier`,
`product_modifier_group`, `floor_plan`, `table`, `register_shift` (own shifts, last `SYNC_HISTORY_DAYS` default 30 days),
`sale_summary` (last `SYNC_HISTORY_DAYS` default 30 days, header/totals/payments summary; full line detail stays online),
and `customer_balance` (`balance` and `balance_owed`). Entities are filtered by the caller's
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

#### Entity JSON Shapes (Phase 6c Offline Sync Gaps)

##### 1. Restaurant Catalog & Layout Reference Data (Read-Only)
Synced to offline devices with tombstones (`op: "delete"` on soft-delete). Modifiers are tenant-wide; floor plans and tables are branch-scoped (`branch_id`).
* `modifier_group`:
  ```json
  {"id": "uuid", "business_id": "uuid", "name": "Size", "selection_type": "single|multiple", "min_selections": 1, "max_selections": 1, "is_required": true, "sort_order": 1, "is_active": true, "created_at": "...", "deleted_at": null}
  ```
* `modifier`:
  ```json
  {"id": "uuid", "business_id": "uuid", "modifier_group_id": "uuid", "name": "Large", "price_delta": "2.50", "sort_order": 2, "is_active": true, "created_at": "...", "deleted_at": null}
  ```
* `product_modifier_group`:
  ```json
  {"id": "uuid", "business_id": "uuid", "product_id": "uuid", "modifier_group_id": "uuid", "created_at": "...", "deleted_at": null}
  ```
* `floor_plan`:
  ```json
  {"id": "uuid", "business_id": "uuid", "branch_id": "uuid", "name": "Main Floor", "is_active": true, "created_at": "...", "deleted_at": null}
  ```
* `table`:
  ```json
  {"id": "uuid", "business_id": "uuid", "branch_id": "uuid", "floor_plan_id": "uuid", "table_number": "T1", "capacity": 4, "pos_x": null, "pos_y": null, "status": "available", "created_at": "...", "deleted_at": null}
  ```

##### 2. Local History Screens (`SYNC_HISTORY_DAYS`, default 30)
* `register_shift`: Delivered only for caller's own shifts (`opened_by == user.id`) within the last `SYNC_HISTORY_DAYS`:
  ```json
  {"id": "uuid", "business_id": "uuid", "branch_id": "uuid", "cash_register_id": "uuid", "opened_by": "uuid", "closed_by": "uuid|null", "opened_by_name": "Cashier", "closed_by_name": "Manager", "status": "open|closed", "opening_float": "1000.00", "expected_cash": "5500.00", "actual_cash": "5450.00", "cash_difference": "-50.00", "opened_at": "...", "closed_at": "..."}
  ```
* `sale_summary`: Recent sales summaries within the last `SYNC_HISTORY_DAYS` (full line items stay online-only):
  ```json
  {"id": "uuid", "sale_number": "INV-20261116-0001", "number": "INV-20261116-0001", "client_receipt_no": "T1-000123", "status": "completed", "sold_at": "2026-11-16T10:00:00Z", "branch_id": "uuid", "customer_id": "uuid|null", "customer_name": "Ali Traders", "customer": {"id": "uuid", "name": "Ali Traders", "phone": "0300..."}, "totals": {"subtotal": "500.00", "discount": "0.00", "tax": "25.00", "total": "525.00"}, "total_amount": "525.00", "payments": [{"payment_method": "cash", "amount": "525.00"}], "created_at": "..."}
  ```

##### 3. Customer Ledger Balance Convention & `balance_owed`
Customer balance is computed by the server from `customer_ledger` entries (`sum(payments) - sum(credit_sales)`):
* `balance`: The raw server ledger balance.
  * `balance < 0` (e.g. `"-250.00"`): **Customer owes money to the shop**.
  * `balance > 0` (e.g. `"100.00"`): Customer has advance store credit / overpaid.
  * `balance == 0`: Account is settled.
* `balance_owed`: Explicit positive owed amount (`-balance`):
  * `balance_owed > 0` (e.g. `"250.00"`): **Customer owes the shop Rs. 250.00**.
  * This matches the Flutter credit limit check directly (`balance_owed + new_sale_credit <= credit_limit`).
  * `balance_owed <= 0`: No outstanding debt.
* Exposed in both `customer` and standalone `customer_balance` feed entities:
  ```json
  {"id": "uuid", "customer_id": "uuid", "balance": "-250.00", "balance_owed": "250.00"}
  ```

##### 4. Receipt Branding in `settings`
The `settings` entity change provides the business identity fields (the receipt footer is NOT here any more: it is
`footer.text_en` / `footer.text_ur` of the `receipt_layout` entity):
```json
{
  "business_name": "My Supermarket",
  "address_line1": "123 Main Road",
  "address": "123 Main Road",
  "phone": "03001234567",
  "tax_id": "1234567-8",
  "ntn": "1234567-8",
  "tax_labels": {"standard": "GST"},
  "currency": "PKR",
  "timezone": "Asia/Karachi",
  "receipt_prefix": "RCP",
  "invoice_prefix": "INV",
  "features": {"enable_restaurant": true, "enable_table_management": true, ...}
}
```

##### 5. `sale.create` Modifier Line Payload
Lines in `sale.create` op accept an optional `modifiers` array and `notes`:
```json
{
  "lines": [
    {
      "product_id": "uuid",
      "qty": "2",
      "unit_price": "100.00",
      "discount_amount": "0",
      "tax_amount": "0",
      "tax_rate": "0",
      "notes": "No onions",
      "modifiers": [
        {
          "modifier_id": "uuid|null",
          "name": "Extra Cheese",
          "price": "25.00",
          "qty": "1"
        }
      ]
    }
  ]
}
```
* Modifiers are persisted into `sale_lines.notes` under the standard JSON format: `restaurant:{"kitchen_notes": "...", "modifiers": [{"modifier_id": "...", "name": "...", "price_delta": "...", "price": "...", "qty": "..."}]}`.
* Client totals are frozen (what customer paid is recorded). Price differences are flagged as `modifier_price_mismatch`. Unknown modifier IDs are accepted, snapshot name and price preserved, and flagged as `unknown_modifier`.
* Dine-in / restaurant tab operations (`tab.*`, `dine_in.*`) are strictly online-only: if pushed, the op is rejected with stable error code `online_only_operation` (`retryable: false`).

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
                            "tax_rate": "5", "tax_amount": "10.00", "notes": "Extra spicy", "line_order": 0,
                            "modifiers": [{"modifier_id": "m1m1...", "name": "Extra Cheese", "price": "25.00", "qty": "1"}]}],
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
| `modifier_price_mismatch` | a line modifier price differs from the catalog modifier price delta |
| `unknown_modifier` | modifier id is unknown or not linked; accepted with snapshot name/price preserved |
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
| `online_only_operation` | no | tab or dine-in operation arrived in `/sync/push` (dine-in ops stay online-only) |
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

## Tax and price settings

Per-business settings (stored in `app_settings`, business-wide; read with `GET /settings`, written with
`PUT /settings` / `PUT /settings/bulk`, permission `settings.manage`). `GET /settings` always returns every key
below with its default filled in. No tax rate is hardcoded anywhere: rates come from the business's own
`/tax-rates`.

```json
{
  "tax.enabled": true,
  "tax.prices_include_tax": false,
  "tax.rounding_mode": "none",
  "tax.show_breakdown_on_receipt": true,
  "tax.label": "GST",
  "tax.default_rate_id": null,
  "ui.hide_zero_decimals": false
}
```

A row is `{"setting_key": "tax.rounding_mode", "setting_value": {"value": "nearest_5"}}`. Validation (422):
booleans must be real booleans, `tax.rounding_mode` is `none | nearest_1 | nearest_5 | nearest_10`,
`tax.label` is 1-30 characters, `tax.default_rate_id` must be one of the business's tax rates (or `null`).
Existing businesses get `enabled=true`, `prices_include_tax=false`, `rounding_mode=none`.
Changing `tax.prices_include_tax` or `tax.rounding_mode` writes an `audit_logs` row (`table_name='app_settings'`,
old value, `new_values={event:"tax_setting_changed", setting_key, value, changed_by, applies_to:"new_sales_only"}`).
A change only applies to **new** sales; every sale and return stores the mode it was made with.
All of these keys (and `ui.hide_zero_decimals`, which is display only) are in the sync snapshot
(`settings` entity, `app_settings`); any settings change bumps the business change sequence.

Registration (`POST /auth/register`) accepts an optional `prices_include_tax`. Default by business type:
`pharmacy` and `mart` (grocery/general store) = true, everything else (retail, restaurant, ...) = false.
`tax.enabled` starts true with no default rate.

### Which rate a line gets

1. tax disabled -> 0
2. product marked `is_tax_exempt` -> 0
3. product's own tax rate (`tax_rate_id`) if set
4. otherwise the business default POS rate (`tax.default_rate_id`), otherwise 0

(`sales.override_tax` still lets a permitted user override the rate on a sale.)

Product API (`POST /products`, `PUT /products/{id}` with `products.update`): `tax_rate_id` (nullable; must be an
active rate of the same business, `null` on update clears it) and `is_tax_exempt` (bool, default false). Product
reads return `{"tax_rate_id": "<uuid>|null", "tax_rate": "17.000|null", "is_tax_exempt": false}`. Changes are audited
(before/after of both fields). The sync `product` entity carries `tax_rate_id` and `is_tax_exempt`. CSV: the
`products` export and `POST /products/import` have `tax_rate_name` (matched by name within the business, unknown
or inactive name = row error, blank = unchanged) and `tax_exempt` (`true|false`, blank = unchanged).

Product create is atomic and returns the full product. `POST /products` accepts `barcodes` (product-level) and
`variations[].barcodes` (`{barcode, barcode_type?, is_primary?}`); the product, its variations and all barcodes are
saved in one transaction (a duplicate barcode/SKU, two primaries, or a barcode already used = nothing is saved: 409/400).
Responses (`POST|GET|PUT /products`, `/products/barcode/{code}`) embed `category` as a slim
`{id, name, parent_id}` (never its `children`), plus `brand`, `base_unit`, `variations[]` (each with `unit` and
`barcodes`) and `barcodes`. The category tree is served only by `/categories`.

#### Async loading rule (no `MissingGreenlet`)

The async session cannot lazy-load. Every relationship must be declared `lazy="selectin"` or `"noload"`, a response
schema may only read relationships its service loaded explicitly (`selectinload` in the query), and recursive tree
schemas (`CategoryResponse`, `ExpenseCategoryResponse`) are only ever an endpoint's own response and are loaded with
`selectinload(..., recursion_depth=-1)`; embed a brief schema (e.g. `CategoryBrief`) anywhere else. After an update,
reload with `execution_options(populate_existing=True)` rather than `db.refresh()` (which resets `noload`
relationships to empty). Guards: `tests/unit/test_async_loading_safety.py` (static), `tests/integration/smoke`
(API smoke for every nested resource; fails on any 5xx and on `MissingGreenlet` in the server log) and the managed
test server, which fails its fixture teardown if `MissingGreenlet` was ever logged.

### Formulas (`app/services/pricing_engine.py`, Decimal only, half-up to 2 dp)

Line, with `gross_in = qty x price`, `d` = discount (percent, fixed or cart share; never below 0 for the line):

| | exclusive (default) | inclusive |
|-|-|-|
| net | `gross_in - d` | `gross - tax` |
| tax | `round2(net x rate / 100)` | `round2(gross x rate / (100 + rate))` |
| line total (gross) | `net + tax` | `gross_in - d` |

`net + tax == gross` exactly in both modes. A cart-level fixed discount is spread proportionally over the lines
(each share rounded to the cent, the last line takes the remainder, no share negative or above its line) and then
priced like a line discount. Modifier prices are part of the unit price, so they follow the product's mode.
With rate 0 or tax disabled both modes give identical totals.

Sale: `exact_total = sum(net) + sum(tax)`; `total_amount = exact_total + rounding_amount`.

Rounding (`tax.rounding_mode`) touches **only** the invoice grand total, half-up to the step (1, 5 or 10);
`rounding_amount = total_amount - exact_total` is signed (negative when rounded down) and stored on the sale. Per-line
values are never rounded to whole rupees. Payments are compared with `total_amount`; credit, ledger, register, FIFO
and reports use the recorded amounts.

Examples (price 6, rate 17):

| mode | qty | net | tax | total |
|-|-|-|-|-|
| exclusive | 1 / 2 / 3 | 6.00 / 12.00 / 18.00 | 1.02 / 2.04 / 3.06 | 7.02 / 14.04 / 21.06 |
| inclusive | 1 | 5.13 | 0.87 | 6.00 |
| inclusive, qty 3, `nearest_5` | 3 | 15.38 | 2.62 | exact 18.00, rounding +2.00, total 20.00 |
| exclusive, qty 3, `nearest_5` | 3 | 18.00 | 3.06 | exact 21.06, rounding -1.06, total 20.00 |

### Returns and voids

A return uses the sale's own snapshot (`tax_inclusive`, `tax_rounding_mode`, `tax_label`) and the original line
net/tax, prorated by quantity (the return that clears a line takes the remainder). Rounding applies to the refund
total only; the return that clears the whole sale refunds exactly what is left of the original rounded total, so a
full return (or partials adding up to it) refunds exactly the original `total_amount`. A void reverses what was
recorded.

### Offline replay

`sale.create` ops may carry `tax_inclusive`, `tax_rounding_mode`, `tax_label`, `rounding_amount`, `total_amount`
(and an informational `settings_version`). The server recomputes with the engine and flags, never rejects:
`tax_mismatch`, `rounding_mismatch`, `total_mismatch`, `settings_mismatch` (device settings differ from the
current ones). Without a mode in the payload, the settings in effect at `occurred_at` are used (rebuilt from the
audit log).

### API shapes

Sale / `GET /sales/{id}` additions:

```json
{ "tax_inclusive": false, "tax_rounding_mode": "nearest_5", "tax_label": "GST",
  "subtotal": "18.00", "total_discount": "0", "net_amount": "18.00", "total_tax": "3.06",
  "exact_total": "21.06", "rounding_amount": "-1.06", "total_amount": "20.00",
  "lines": [{ "net_amount": "18.00", "tax_rate": "17.000", "tax_amount": "3.06" }] }
```

`POST /sales/price-preview` returns the same totals plus `tax_enabled`. Returns carry `tax_inclusive`,
`tax_rounding_mode`, `tax_label`, `rounding_amount`, line `net_amount`. `GET /invoice/{id}` (and the public
`/r/{token}` receipt, thermal and PDF) add to `summary`:

```json
{ "tax_inclusive": true, "tax_label": "GST", "tax_rounding_mode": "nearest_5", "show_tax_breakdown": true,
  "net_amount": "15.38", "total_tax": "2.62", "exact_total": "18.00", "rounding_amount": "2.00",
  "total_amount": "20.00",
  "tax_breakdown": [{ "tax_label": "GST", "tax_rate": "17.000", "net_amount": "15.38", "tax_amount": "2.62" }] }
```

`show_tax_breakdown` = tax enabled and `tax.show_breakdown_on_receipt` and tax > 0. The receipt prints
business name, NTN, address, phone, items, subtotal, discount, tax rows per rate ("GST 17%"), a Rounding line
when non-zero, the grand total, payments and change, and "Prices include tax" once in inclusive mode.

`GET /settings/tax-preview?price=6&qty=3&rate=17&discount=0` (permission `sales.create`) breaks one line
down with the current settings:

```json
{ "tax_enabled": true, "tax_inclusive": false, "tax_rounding_mode": "none", "tax_label": "GST",
  "tax_rate": "17", "qty": "3", "unit_price": "6", "subtotal": "18.00", "discount_amount": "0.00",
  "net_amount": "18.00", "tax_amount": "3.06", "exact_total": "21.06", "rounding_amount": "0.00",
  "total_amount": "21.06" }
```

### Reports and exports

Revenue is net sales everywhere (dashboard, sales summary, profit and loss, exports), so they agree. The sales
summary adds `total_gross` (net + tax), `total_rounding` and `total_collected`. The tax summary groups by
label and rate (`tax_by_rate[].tax_label`). Profit and loss = net sales minus COGS. Export datasets `sales`
(subtotal, discount, net, tax, rounding, total, paid, balance_due, tax_mode, tax_label) and `sale_lines`
(net, tax_rate, tax, tax_label, line_total) are available in CSV, XLSX and PDF.

### Tests

`tests/unit/test_pricing_engine_tax.py` (engine, property-style random carts) and `tests/integration/tax/`.

## Support endpoints (Phase 5)

### Receipts by link

* `POST /sales/{id}/receipt-link` (`sales.view`) → `201 {"url": "https://pos.example.com/r/<token>", "expires_at": "2026-12-13T08:15:00Z"}`.
  The origin is `PUBLIC_BASE_URL` (else the request origin); expiry is `RECEIPT_LINK_DAYS` (default 30) from issue.
* `POST /sales/receipt-link/lookup` (`sales.view`) with `{"sale_number": "..."}` or `{"client_receipt_no": "..."}` finds a
  sale (including offline-synced ones) and returns the same shape.
* `DELETE /sales/{id}/receipt-link` revokes every link of the sale → `{"revoked": 2}`.
* `GET /r/{token}` is public and rate limited per IP (`RATE_LIMIT_RECEIPT_IP`): a mobile-friendly HTML receipt
  (business name, items, totals, payments, receipt number, receipt footer from settings), or `?format=pdf` for an 80 mm PDF.
  Unknown, forged or revoked token → `404`; expired → `410`. The page never shows customer, cashier or ledger data.
  The token is `base64url(random id).base64url(HMAC-SHA256(JWT_SECRET, id)[:16])`; the signature is checked before the database.

### Exports

`GET /export` lists the datasets you may export. `GET /export/{dataset}?format=csv|xlsx|pdf&date_from=&date_to=&branch_id=`
needs `reports.export` (some datasets need one more permission, shown by the listing).

Datasets: `sales`, `sale_lines`, `inventory` (alias `stock_valuation`), `products`, `customers` (+`customers.view`),
`customer_ledger` (+`customers.ledger.view`), `suppliers` (+`suppliers.view`), `supplier_ledger` (+`suppliers.ledger.view`),
`expenses` (+`expenses.view`), `purchases` (+`inventory.purchase_orders.view`), `profit_loss` and `tax_summary`
(+`reports.financial`), `cashier_performance`, `top_products`.

* Dates default to the last 30 days (business timezone); `date_to` is inclusive. `branch_id` follows the user's branch scope.
* Dates are `YYYY-MM-DD`, times `YYYY-MM-DD HH:MM` in the business timezone (default Asia/Karachi); money is a plain
  number with two decimals (PKR, no symbol); CSV is UTF-8 with a BOM.
* Ledger balances are the sum of ledger amounts: negative means the party owes you (customers) or is owed (suppliers).
* Text starting with `= + - @`, tab, CR or LF is prefixed with `'` (formula-injection safe); plain numbers are untouched.
* More than `EXPORT_MAX_ROWS` rows → `413 {"detail": "Export too large: more than 50000 rows. Narrow the date range or pick a branch and try again."}`.
* For PDF (`format=pdf`), more than `EXPORT_PDF_MAX_ROWS` (default 5,000) rows → `413` with a message suggesting CSV or Excel format.
* PDF exports include a clean branded layout (business name, dataset title, date range, generated timestamp in Asia/Karachi, page numbers, repeating headers, landscape for wide tables, PKR number formatting, summary totals row, and Unicode Urdu RTL text support).
* The `products` columns (`name, sku, barcode, category, unit, cost, price, stock`) are what `POST /products/import`
  reads (`stock` is accepted as `opening_stock`).
* The older `/invoice/export/sales` and `/invoice/export/inventory` keep working.

### Price lists

* `GET/POST /prices/lists`, `GET/PUT/DELETE /prices/lists/{id}`: `name`, `list_type` (`retail|wholesale|custom`), `is_default`,
  `valid_from`, `valid_to`, `is_active`. `GET /prices/lists` returns `item_count` and `customer_count` and **no longer embeds items**.
  Exactly one list is the default: set `is_default: true` on another list to switch. The default cannot be deleted,
  deactivated, given a validity window, or un-defaulted directly. Deleting a list unassigns its customers
  (`{"deleted": true, "customers_unassigned": 3}`).
* `GET /prices/lists/{id}/items?search=&skip=&limit=` → `{"total", "skip", "limit", "items": [{id, product_id, variation_id, product_name, variation_name, sku, unit_price, min_qty, updated_at}]}`.
* `POST /prices/lists/{id}/items/bulk` with `{"items": [{"product_id", "variation_id": null, "price": "90.00", "min_qty": "1"}]}`
  (at most `PRICE_BULK_MAX_ITEMS`) → `{"created", "updated", "unchanged", "total"}`. `DELETE /prices/lists/{id}/items/{item_id}` → 204.
* `POST /prices/lists/{id}/copy` `{"name", "percent"?, "list_type"?, "valid_from"?, "valid_to"?}`;
  `POST /prices/lists/{id}/adjust` `{"percent": "-5", "category_id"?, "round_to"?, "dry_run"?}` → `{dry_run, updated, unchanged, sample}`.
* `POST /prices/lists/{id}/import` `{"csv": "sku,price,min_qty\n..." | "rows": [...], "dry_run": true, "on_error": "abort|skip"}`.
  Rows match by `sku` (product, then variation) or `barcode`. See *Import reports* below.
* Customers carry `price_list_id` (create/update/sync). **Sale pricing rule:** a sale with a customer and no explicit
  `price_list_id` uses the customer's list; a line without `unit_price` is priced server-side from that list (quantity tiers via
  `min_qty`), falling back to the default list when the list is inactive, outside its validity window, or has no price.
  `GET /prices/product/{id}?customer_id=&qty=` shows the result.
* All list, item and customer changes are stamped for the sync feed; `customer.price_list_id` and `price_list.valid_from/valid_to` are in pull payloads.

### Product import

`POST /products/import` (`products.create`; applying also needs `products.update`; opening stock needs `inventory.adjust`)
with `{"csv": "name,sku,barcode,category,unit,cost,price,opening_stock\n...", "dry_run": true, "on_error": "abort", "branch_id"?}`.
Products match by `sku`, then `barcode`, so re-running a file changes nothing. The price goes to the default list; unknown
categories are created; the unit must already exist (name or symbol); opening stock is posted once as an `opening_balance`
adjustment (skipped for products that already have stock history).

### Import reports (products and price lists)

```json
{"dry_run": true,
 "summary": {"total_rows": 4, "create": 1, "update": 0, "unchanged": 0, "errors": 3, "applied": 0},
 "unknown_columns": [],
 "rows": [{"row": 2, "action": "create", "sku": "SOAP-1", "errors": [], "warnings": []},
          {"row": 3, "action": "error", "sku": "BU-1", "errors": ["unit 'Parsec' not found"], "warnings": []}]}
```

`row` is the file line (header = 1). Applying with errors and `on_error: "abort"` (the default) writes nothing and answers
`422 {"detail": {"code": "import_has_errors", "message": "...", "report": {...}}}`; `on_error: "skip"` applies the valid rows.
More than `IMPORT_MAX_ROWS` rows → 413.

### Image upload

`POST /uploads/image` (`products.update` or `settings.manage`), multipart field `file` (jpg, png, gif, webp; checked by
content, at most `UPLOAD_MAX_IMAGE_BYTES`) → `201 {"url": "..."}`. With `CLOUDINARY_URL` set the image goes to Cloudinary
(folder `CLOUDINARY_FOLDER/<business id>`); otherwise it is stored under `UPLOAD_LOCAL_DIR` and served from `/static/uploads`
(development only; production answers 503 without Cloudinary). 415 for a wrong type, 413 when too large.

### Phase 5 tests

`tests/integration/phase5/` (receipt links, exports, price lists, product import, uploads).

## Receipt layouts, numbering and pharmacy batches

An invoice is a sale rendered with a **layout**. A layout is business data (`receipt_layouts`), assigned per channel
(`print`, `whatsapp`, `link`) and optionally per branch. ONE render model
(`build_receipt_model(sale, layout, channel)` in `app/services/receipt_render.py`) feeds the HTML, PDF and WhatsApp
text renderers and the public `/r/{token}` page. Migrations: `027_receipt_layouts` (idempotent, seeds existing businesses) and `028_receipt_language` (config v2, legacy options).

Designs: `thermal_classic`, `thermal_compact` (papers `roll58` / `roll80`), `a4_tax_invoice`, `a4_elegant` (paper `a4`).
Thermal PDFs are exactly 58 / 80 mm wide with an exact height; A4 designs are A4. Every business gets all four layouts;
`thermal_classic` is the default for all three channels. Pharmacy presets turn `items.show_batch` / `show_expiry` on.

### Layout config (`config`, JSONB, validated, unknown keys rejected, defaults filled in)

```json
{
  "v": 2,
  "header": {"show_logo": true, "show_business_name": true, "show_address": true, "show_phone": true,
             "show_ntn": true, "ntn_label": "NTN", "extra_lines": []},
  "meta": {"show_number": true, "number_source": "register", "show_datetime": true, "show_cashier": true,
           "show_branch": false, "show_register": false, "show_customer": true, "show_customer_phone": false,
           "walkin_label": "Walk-in"},
  "items": {"show_name": true, "show_variation": true, "show_sku": false, "show_barcode": false, "show_unit": false,
            "show_qty": true, "show_unit_price": true, "show_line_discount": true, "show_line_total": true,
            "show_batch": false, "show_expiry": false},
  "totals": {"show_subtotal": true, "show_discount": true, "show_tax_breakdown": true, "show_rounding": true,
             "show_includes_tax_note": true, "show_payments": true, "show_change": true,
             "show_previous_balance": false, "show_balance_after": false},
  "footer": {"text_en": "", "text_ur": "", "show_powered_by": false, "show_qr": true},
  "labels": {"total": {"en": "Total", "ur": "کل"}},
  "style": {"language": "en", "font_scale": 1.0, "show_dividers": true, "compact_items": false}
}
```

* `header.extra_lines`: at most 3 lines. `meta.number_source`: `register` | `official`. `style.font_scale`: 0.8-1.4.
* **`style.language`**: `"en"` (default) | `"ur"` | `"both"`. It is the ONE language switch of the layout (see "Language"
  below). It replaces `items.name_language`, which old configs may still send: it is accepted, ignored and dropped on save.
* **Config version**: stored/submitted configs are version 2. A version-1 config (or one without `v`) is upgraded when
  it is read (`GET /receipt-layouts`, the sync feed, rendering) and when it is saved: `items.name_language` is dropped and
  `style.language` becomes `"both"` when the config had an edited Urdu caption (a `labels.*.ur` that differs from the
  built-in text) or a `footer.text_ur`, otherwise `"en"`. A config with a `v` above 2 is rejected (422).
* `labels` is a partial map of `{key: {"en", "ur"}}`; keys must be known (`GET /receipt-layouts/designs` lists the
  defaults for every key: receipt, tax_invoice, invoice, number, date, cashier, customer, item, qty, price, amount, sku,
  barcode, batch, expiry, subtotal, discount, tax, rounding, total, paid, balance_due, change, payment methods, ...).
  Every key has an English and an Urdu default (complete in both languages); an empty `en`/`ur` falls back to it.
  Other layout texts follow the same language: `header.ntn_label` (the default `NTN` becomes `این ٹی این` in Urdu; a custom
  label is printed as written), `meta.walkin_label` (same rule), the tax caption of a tax line (`GST`, `VAT`, `Sales Tax`
  have Urdu captions; any other custom tax name is printed as written), and the `Batch` / `Exp` / `SKU` item captions.
* Per-design defaults differ (e.g. `thermal_compact`: `compact_items`, `font_scale` 0.9, no cashier/customer/QR).

### Language

`style.language` decides what a receipt says, for print, WhatsApp and the public page alike. The render model resolves
EVERY caption and the footer to exactly one language, never a mixed "Total / کل":

| `style.language` | captions | footer | product name | direction |
|---|---|---|---|---|
| `en` | English only | `footer.text_en`, else "Thank you for shopping with us!" | product name | LTR |
| `ur` | Urdu only | `footer.text_ur`, else "خریداری کا شکریہ!" (the English text is never used as a fallback) | `name_secondary` if set, else the name | RTL |
| `both` | two lines: English, then Urdu | two lines: `text_en` (or default), `text_ur` (or default) | name, `name_secondary` on a second line | LTR |

* Model additions: `"language"`, `"rtl"` (true only for `ur`), `"dir"` (`"rtl"` | `"ltr"`), and `"captions"` (`sku`, `batch`,
  `expiry`, `receipt`, `download_pdf`, on one line). In `both` a label/title is `"Total\nکل"` (a line break between the two).
* RTL (`ur`): HTML gets `<html lang="ur" dir="rtl">` and a mirrored page (captions right, amounts left, columns reversed),
  the PDF mirrors the caption/amount rows and the A4 table, and the WhatsApp text prefixes every line with a right-to-left
  mark (U+200F). `en` and `both` are left-to-right (`lang="en"`).
* Data is never translated: business name, address, customer, product names, batch numbers, dates and amounts print as
  stored. Only captions, the footer and the choice between product name / `name_secondary` follow the language.
* The language belongs to the layout, so each channel (`print`, `whatsapp`, `link`) can differ by assigning a different
  layout. The public `/r/{token}` page, `GET /invoice/{id}/receipt` (all formats) and `GET /invoice/{id}/thermal` (its
  `receipt`, `footer` and `paper_size` come from the print layout) all follow it. The "receipt unavailable" error page
  of an invalid link has no layout and stays English.

### Deprecated receipt settings

Receipt options used to live in `app_settings` / the business config. Since layouts they are DEPRECATED: rows that exist
stay readable (`GET /settings`, any signed-in POS user for the `receipt.` prefix) but nothing renders from them and the
sync `settings` entity no longer carries them. Use the layout instead:

| Deprecated key | Replaced by |
|---|---|
| `receipt.footer_text` (and business config `receipt_footer`, `receipt.footer`) | `footer.text_en` / `footer.text_ur` |
| `receipt.paper_size` (`58mm`, `80mm`, `a4`) | layout `paper` / which layout is assigned to `print` |
| `receipt.show_tax` | `totals.show_tax_breakdown` |
| `receipt.show_logo` | `header.show_logo` |
| sync `settings.receipt_footer` | `receipt_layout` entity |

Still server-side and current: `receipt.number_prefix`, `sales.invoice_*`, `inventory.*` (numbering and batch policy) and
`tax.*` (e.g. `tax.show_breakdown_on_receipt`, which feeds the sale snapshot). Printing-hardware preferences (printer,
cut, drawer) are local to the device; there are none on the server.

Migration `028_receipt_language` (idempotent; only layouts below config v2 are touched, so a second run changes nothing):
1. every stored layout config is upgraded to v2 (`name_language` dropped, `style.language` set as described above);
2. for each business, legacy options are copied ONCE into its seeded layouts (the four built-in names), only where the
   owner had changed them from the defaults: footer -> `footer.text_en` (Urdu script -> `footer.text_ur`), `58mm` ->
   `roll58` on the thermal layouts, `receipt.show_tax = false` -> `totals.show_tax_breakdown = false`,
   `receipt.show_logo = false` -> `header.show_logo = false`; legacy `a4` makes the A4 tax invoice the business default of
   the `print` channel while it still points at the seeded classic layout. `app_settings` values win over the business
   config (`receipt_footer`, `receipt` object);
3. layouts with an edited Urdu caption or an Urdu footer get `style.language = "both"`, all others `"en"`.
The legacy rows are never deleted. The logic is `app/services/receipt_legacy.py`.

### API (all `settings.manage`; changes are audited with before/after)

| Method | Path | Notes |
|---|---|---|
| GET | `/receipt-layouts` | list |
| POST | `/receipt-layouts` | `{"name","design","paper"?,"config"?}` -> 201; 422 on a bad config or a design/paper mismatch |
| GET/PUT/DELETE | `/receipt-layouts/{id}` | PUT `{"name"?,"design"?,"paper"?,"config"?,"is_active"?}` (see "Changing the design"); deleting / deactivating a layout that is assigned -> 409 |
| POST | `/receipt-layouts/{id}/duplicate` | `{"name"?}` -> 201 |
| GET | `/receipt-layouts/designs` | `{"designs":[{design,paper options,default config...}],"channels":["print","whatsapp","link"]}` |
| GET | `/receipt-layouts/assignments` | list |
| PUT | `/receipt-layouts/assignments` | upsert; `layout_id: null` removes a branch override (the business default cannot be removed) |
| POST | `/receipt-layouts/preview` | see below |

Layout response: `{"id","business_id","name","design","paper","config","is_active","created_at","updated_at"}`.

**Changing the design** (`PUT /receipt-layouts/{id}` with `design`): the config schema is the same for every design, so the
stored config (language, footer, labels, toggles) is kept as is and stays valid; send `config` too to change it in the same
request (it is validated like any save). `paper` follows the design: name one explicitly, otherwise the current paper is
kept when the new design prints on it (`roll58` <-> `roll80` between thermal designs) and the new design's own paper is
used when it does not (thermal -> `a4`, A4 -> `roll80`). An unknown design, a design/paper mismatch or an invalid config is
422 and nothing is changed. The change is audited (before/after `design` and `paper`). The layout keeps its name and its
channel assignments.

Assignments: `PUT {"assignments":[{"channel":"whatsapp","branch_id":"<uuid>|null","layout_id":"<uuid>|null"}]}`;
`GET` rows are `{"id","branch_id","channel","layout_id","updated_at"}`. `branch_id: null` = business default. Resolution
for a sale: the branch assignment for the channel, else the business default.

Preview: `POST /receipt-layouts/preview {"layout_id"?|"design"?, "paper"?, "config"?, "sale_id"?, "channel"?: "print"}`
returns `{"mock": bool, "model": {...}, "html": "...", "text": "..."}`. Without `sale_id` a built-in sample sale (Urdu
names, a batch with expiry, tax, discount, cash change) is used. `config` previews unsaved edits.

Receipts: `GET /invoice/{sale_id}/receipt?channel=print|whatsapp|link&format=json|html|pdf|text` (`sales.view`);
`GET /invoice/{id}/thermal` now also carries `"receipt"` (the print model); `/r/{token}` uses the `link` layout.

### Render model (`format=json`)

`{"v":1,"channel","layout":{"id","name","design"},"paper":{"code","width_mm","a4"},"style","language","rtl","dir","captions","currency","is_void",
"title","header":{"logo_url","business_name","address","phone","ntn":{"label","value"}|null,"extra_lines"},
"meta":[{"key","label","value"}],"columns","items":[{"name","name_secondary","sku","barcode","unit","qty","unit_price",
"discount","line_total","batches":[{"batch_no","expiry","qty"}]}],"totals":[{"key","label","value","strong"}],
"payments","notes","footer":{"lines","powered_by"},"qr":{"content"}|null,"link","show_dividers"}`.

Rules: no internal sync/status text; ONE receipt number per `meta.number_source` (`register` = `client_receipt_no`, else
the official `sale_number`; falls back to the other); the name is the PRODUCT name (the variation only when it is not
"Default"/empty); quantities have no decimals unless the unit has `allows_decimal`; NTN prints with its label from the
business `tax_id` (a value that looks like a tax label/rate is dropped, never printed); tax and rounding come from the
sale snapshot; expiry prints `MM/YYYY`; the QR is the public receipt link and is omitted unless the sale is synced (and
always omitted on the `link` channel); the `link` channel never prints customer, cashier or balances.
The server draws the QR only if the optional `segno` package is installed (`pip install segno`); without it the model's
`qr.content` still carries the URL for the client to draw.

### Receipt numbers

* Register number (device): `sync_devices.short_code` (2-4 chars, auto `C1`, `C2`, ... in registration order, unique per
  business case-insensitively). Returned as `device_short_code` on `GET /sync/pull` and `POST /sync/push`, and as
  `short_code` on `GET /devices`. Owner edit: `PUT /devices/{device_id} {"short_code": "SH1"}` (409 `short_code_taken`,
  422 for bad length/characters). The app builds `"{short_code}-{counter:04d}"`, prefixed with the `number_prefix`
  returned by pull (setting `receipt.number_prefix`, empty by default). `client_receipt_no` stays unique per business.
* Official number (server `sale_number`), settings `sales.invoice_prefix` (default `INV`), `sales.invoice_digits`
  (default 4), `sales.invoice_start` (default 1). Neither digits nor start configured -> the long-standing daily
  `INV-YYYYMMDD-####`. Once either is set: one continuous counter `PREFIX-0001`; `sales.sale_seq` stores the numeric counter
  so numbers sort numerically past the digit width. Existing numbers never change. All three keys, plus
  `receipt.number_prefix`, `inventory.pick_policy` and `inventory.expired_sale`, are business-wide `app_settings`
  (422 on invalid values) and are also in the sync `settings` entity under `app_settings`.

### Pharmacy: batch, expiry, FEFO

* Layers (`purchase_lines`) carry `batch_number` / `expiry_date`; purchase receipts, adjustments (`qty_delta > 0`) and the
  product CSV import (`batch_no`, `expiry_date` columns, with `opening_stock`) accept them. `batch_no` is accepted as an
  alias of `batch_number` on input. `products.track_expiry` / `name_secondary` and `units.allows_decimal` are in the
  product / unit create, update and response shapes.
* `inventory.pick_policy`: `fifo` (default) | `fefo` (earliest expiry first, no expiry last, then oldest). Costing stays per
  layer. `inventory.expired_sale`: `block` (online sale of expired stock -> `400 {"detail":{"code":"expired_batch"}}`,
  unexpired stock is used when it covers the quantity) | `warn` (default: accepted, flag `expired_batch_sold`) |
  `allow`. Pharmacy businesses registered from now on start with `fefo` + `block`; existing businesses keep their settings.
* Offline replay (`sale.create`) always accepts; an expired batch is flagged `expired_batch_sold`.
* `sale.create` lines (and online `POST /sales` lines) may carry the device's allocation:
  `"batches": [{"batch_no": "B2407", "expiry_date": "2027-05-31", "qty": "2"}]`. The server consumes by its own policy and
  stores the ACTUAL allocation in `sale_line_batches` (`sale_line_id, layer_id, batch_no, expiry_date, qty`); a difference
  adds the flag `batch_mismatch` (never a rejection). Receipts print batch/expiry from the stored allocation.
* New flags: `expired_batch_sold`, `batch_mismatch` (in `sync_flags`, `GET /sync/flags`).

Sync entity `stock_batch` (branch scoped; `inventory.view` or `sales.create`), derived from layers that still hold stock:

```json
{"entity": "stock_batch", "id": "<layer id>", "op": "upsert", "seq": 1234,
 "data": {"id": "<layer id>", "branch_id": "<uuid>", "product_id": "<uuid>", "variation_id": "<uuid>|null",
          "batch_no": "B2407", "expiry_date": "2027-05-31", "qty_remaining": "4.0000",
          "cost_per_unit": "8.0000", "received_at": "2026-11-01T09:00:00Z"}}
```

A layer that is sold out (or deleted) arrives as `op: "delete"`; only layers with a batch number or expiry appear.
Sync entities `receipt_layout` (all columns of the layout row) and `receipt_layout_assignment`
(`id, branch_id|null, channel, layout_id`; a business-default row with `branch_id: null` is visible to every branch) are
served to every signed-in user.

Reports: `GET /stock/expiry-report?within_days=30&branch_id=` (`inventory.view`) ->
`{"as_of","within_days","expired":[row],"near_expiry":[row],"totals":{"expired_qty","expired_value","near_expiry_qty","near_expiry_value"}}`
with `row = {layer_id, product_id, product, variation, sku, branch_id, branch, batch_no, expiry_date, days_to_expiry, qty, cost_per_unit, value}`.
Export dataset `expiry` (`GET /export/expiry`, `reports.export` + `inventory.view`): expired stock plus stock expiring in 90 days.

### Receipt tests

`tests/unit/test_receipt_render.py` (render rules, no server), `tests/unit/test_receipt_language.py` (language modes, RTL,
config upgrade, legacy mapping) and `tests/integration/receipt/` (layouts API, numbering, batches, `test_language.py`:
invoice/public page language and the legacy-options migration). `tax_id` on `PUT /business/me` now rejects tax-label text such as "GST 17%" (422).
