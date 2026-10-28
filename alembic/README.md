# Database migrations

## Setup

```bash
cd d:\flutter\sas\backend
pip install -r requirements.txt
cp .env.example .env
# Edit DATABASE_URL in .env
```

## Run migrations

```bash
alembic upgrade head
```

## Revision chain

| Revision | Description |
|----------|-------------|
| `001_initial_schema` | All `schema/001`–`012` SQL files |
| `002_seed_permissions` | Default permission keys |
| `003_fifo_functions` | `get_fifo_cost()` function |
| `004_business_types_lookup` | `business_types` lookup; drop `business_type` enum |
| `005_auth_security` | User lockout columns; `refresh_tokens` table |

## Feature flags (required)

After registration, read features only from `business_configs.config_json` via `app.services.feature_flags`. See `ARCHITECTURE.md`.

## FIFO cost lookup

```sql
SELECT get_fifo_cost(
    '<business_id>'::uuid,
    '<product_id>'::uuid,
    '<variation_id>'::uuid,  -- or NULL
    2.5                      -- qty
);
```
