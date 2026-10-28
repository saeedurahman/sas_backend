# Ponytail — Python / FastAPI Edition

Before writing any code, stop at the FIRST rung below that holds. Do not skip ahead, do not go further than you need to.

## The ladder (Python/FastAPI specific)

1. **Does this need to exist?** → No: skip it (YAGNI). Don't add a new endpoint, model field, or config flag the task didn't ask for.
2. **Does Python's stdlib already do it?** → Use it. (`re`, `pathlib`, `datetime`, `json`, `uuid`, `hashlib`, `urllib.parse`, `collections`) before reaching for a third-party package for something stdlib already handles cleanly.
3. **Does an already-installed framework feature do it natively?** → Use it. Check the project's web framework for built-in validation/dependency-injection mechanisms before hand-rolled validation functions or per-route boilerplate, the ORM's relationship/cascade features before manual join queries, and the database's built-in search capability (if one is already wired in) before adding a separate search library.
4. **Is it already a dependency in this project?** → Use it, don't add an overlapping package. Before reaching for a new library, check what this project already uses for: web framework, ORM/database access, data validation, and HTTP client. If this project has an established pattern for a recurring job (e.g. a shared data-extraction/scraping pipeline reused across multiple sources), new work in that area should extend the existing pattern rather than invent a new one.
5. **Is it a one-line stdlib/Pydantic call?** → Write the one line.
6. **Only then:** write the minimum custom function/class that satisfies the task.

## Never cut corners on (regardless of which rung you land on)

- **Input validation at every trust boundary** — every request body/query param on a public-facing endpoint gets a real, constrained data-validation model (not a loosely-typed catch-all), especially on auth, search, and any user-submission or payment-related endpoints.
- **Error handling around external dependencies** — network failures, malformed/missing source data, and external API failures (e.g. AI-powered translation/summary, third-party services) must be caught and logged, never allowed to crash a job silently or produce empty/garbage records.
- **Idempotent migrations** — never write a migration that isn't safe to re-run; follow whatever safe migration pattern this project already has.
- **Auth/role checks** — role boundaries between different user types are security-relevant; never skip a role/permission check "to save time" on an endpoint, even an internal-looking one.
- **Data-loss handling** — if this project has dedup, quality-threshold, or upsert logic guarding data ingestion, that logic exists specifically to prevent corrupting the database with duplicate/garbage records; don't bypass it for a quick fix.
- **Payment-related code** — if this project handles payments, no shortcuts on amount validation, currency handling, or idempotency keys around any payment flow.

## Mark every shortcut

When you take a shortcut up the ladder (rungs 2–5), leave a comment naming what was skipped and what the upgrade path is:

```python
# ponytail: using the DB's built-in search directly here instead of a search-abstraction layer — revisit if we add a second search backend
```

```python
# ponytail: reusing the existing source pattern as-is — revisit if this new source's structure diverges significantly
```

## Python/FastAPI-specific anti-patterns to flag, not silently write

- Don't add a second HTTP client library alongside whatever this project already uses for networking/scraping — pick one and reuse it.
- Don't add a task queue (Celery/RQ) for a job that the framework's built-in background-task mechanism or a simple cron-triggered script already handles, unless scale genuinely requires it.
- Don't write a custom auth implementation when a maintained library already in the project's dependencies covers it — and don't introduce a second auth mechanism alongside whatever the project already has (e.g. guest/anonymous access).
- Don't mix raw SQL strings into ORM-based code when the existing ORM models already express the relationship — keep query patterns consistent across the codebase.
- Don't write a new data-cleaning/extraction implementation per source when an existing shared pattern (e.g. this project's scraper pipeline) already covers that job — extend it instead of duplicating logic per source.

## Commands (if using a skill-capable host: Claude Code, Codex, OpenCode, Gemini)

| Command | What it does |
|---|---|
| `/ponytail [lite\|full\|ultra\|off]` | Set intensity, or turn off. No argument reports current level. |
| `/ponytail-review` | Review current diff for over-engineering, hand back a delete-list. |
| `/ponytail-audit` | Audit the whole backend/scraper for over-engineering, not just the diff. |
| `/ponytail-debt` | Harvest `# ponytail:` shortcuts into a ledger so they don't get forgotten. |

For Cursor: this file goes at `.cursor/rules/ponytail-python.md` (or `.mdc` with frontmatter if you want it scoped only to `.py` files — see note below).
For Claude Code / Codex / generic agents: append this file's content into `AGENTS.md`, or keep as a separate file the agent is told to read.
For Antigravity/Gemini CLI: drop into `.agents/rules/ponytail-python.md`.