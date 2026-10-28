# Architecture rules

## Feature flags vs business type

`business_types` and `businesses.business_type_id` are **classification and onboarding only**.

- At **registration**, resolve `business_types.code` and apply one-time defaults via `app.services.onboarding_presets` → `business_configs.config_json` (and boolean column mirrors).
- After registration, **never** use `business_type`, `business_type_id`, or `business_types.code` to enable or disable product behavior.

### Wrong

```python
if business.business_type == "restaurant":
    enable_kot()
```

### Correct

```python
from app.services.feature_flags import get_feature_flag

if get_feature_flag(config.config_json, "enable_kot"):
    enable_kot()
```

This applies to FastAPI routers/services, SQL views (no `CASE business_type`), and Flutter UI.

Runtime source of truth: **`business_configs.config_json`** only (`app.services.feature_flags.get_feature_flag`).
