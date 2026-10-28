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

```bash
python scripts/ci_check.py                 # static checks + unit tests, no database needed
python scripts/ci_check.py --integration   # also boots isolated servers against DATABASE_URL (migrated DB)
pytest tests/security                      # the security suite directly
```

The security suite starts its own uvicorn processes (random ports, custom limits) via
`tests/helpers/server.py`, so it does not need or disturb a server you already have running.
It creates throw-away tenants in the configured database; use a development database.
