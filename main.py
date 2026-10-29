from contextlib import asynccontextmanager
import asyncio
import logging

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import TimeoutError as SATimeoutError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.security import HTTPBearer  # noqa: F401 — BearerAuth scheme for Swagger

from app.core.config import settings
from app.core.logging_config import configure_logging
from app.core.middleware import (
    BodySizeLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from app.database import engine
from app.services.subscription import SubscriptionBlocked
from app.routers.accounting import router as accounting_router
from app.routers.adjustments import router as adjustments_router
from app.routers.analytics import router as analytics_router
from app.routers.audit import router as audit_router
from app.routers.license_public import router as license_public_router
from app.routers.platform import router as platform_router
from app.routers.subscription import router as subscription_router
from app.routers.sync import router as sync_router
from app.routers.devices import router as devices_router
from app.routers.time_sync import router as time_router
from app.routers.auth import router as auth_router
from app.routers.brands import router as brands_router
from app.routers.business import router as business_router
from app.routers.branches import router as branches_router
from app.routers.categories import router as categories_router
from app.routers.customers import router as customers_router
from app.routers.discounts import router as discounts_router
from app.routers.expenses import router as expenses_router
from app.routers.invoice import router as invoice_router
from app.routers.prices import router as prices_router
from app.routers.permissions import router as permissions_router
from app.routers.products import router as products_router
from app.routers.registers import router as registers_router
from app.routers.manufacturing_bom import router as manufacturing_bom_router
from app.routers.manufacturing_production import router as manufacturing_production_router
from app.routers.restaurant_kot import router as restaurant_kot_router
from app.routers.restaurant_modifiers import router as restaurant_modifiers_router
from app.routers.restaurant_tables import router as restaurant_tables_router
from app.routers.roles import router as roles_router
from app.routers.notifications import router as notifications_router
from app.routers.returns import router as returns_router
from app.routers.sales import router as sales_router
from app.routers.search import router as search_router
from app.routers.shifts import router as shifts_router
from app.routers.settings import router as settings_router
from app.routers.purchases import router as purchases_router
from app.routers.stock import router as stock_router
from app.routers.suppliers import router as suppliers_router
from app.routers.supplier_ledger import router as supplier_ledger_router
from app.routers.tax_rates import router as tax_rates_router
from app.routers.transfers import router as transfers_router
from app.routers.units import router as units_router
from app.routers.users import router as users_router
from app.routers.waste import router as waste_router

configure_logging(settings.log_level)
logger = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("starting", extra={"app_name": settings.app_name, "env": settings.env})
    yield
    await engine.dispose()
    logger.info("shutdown complete")


_docs_enabled = not settings.is_production

app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
    debug=settings.debug,
    lifespan=lifespan,
    swagger_ui_parameters={"persistAuthorization": True},
)


@app.exception_handler(SATimeoutError)
async def _pool_exhausted_handler(request, exc):
    """Pool saturation is overload, not a bug: tell clients to retry."""
    logging.getLogger("app").warning("db pool exhausted: %s", request.url.path)
    return JSONResponse(
        status_code=503,
        content={"detail": "Server is busy, retry shortly"},
        headers={"Retry-After": "1"},
    )


@app.exception_handler(SubscriptionBlocked)
async def _subscription_blocked_handler(request, exc: SubscriptionBlocked):
    """402 with the machine-readable code, message and the owner's contact details."""
    return JSONResponse(status_code=402, content=exc.body())


def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    openapi_schema = get_openapi(
        title=settings.app_name,
        version="1.0.0",
        routes=app.routes,
    )
    openapi_schema["components"]["securitySchemes"] = {
        "BearerAuth": {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
        }
    }
    openapi_schema["security"] = [{"BearerAuth": []}]
    app.openapi_schema = openapi_schema
    return app.openapi_schema


app.openapi = custom_openapi

# Middleware order: the last one added is outermost.
app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_body_bytes)
app.add_middleware(SecurityHeadersMiddleware, hsts=settings.is_production)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID", "Retry-After"],
)
app.add_middleware(RequestContextMiddleware)

app.include_router(auth_router, prefix=settings.api_v1_prefix)
app.include_router(business_router, prefix=settings.api_v1_prefix)
app.include_router(branches_router, prefix=settings.api_v1_prefix)
app.include_router(users_router, prefix=settings.api_v1_prefix)
app.include_router(permissions_router, prefix=settings.api_v1_prefix)
app.include_router(roles_router, prefix=settings.api_v1_prefix)
app.include_router(categories_router, prefix=settings.api_v1_prefix)
app.include_router(brands_router, prefix=settings.api_v1_prefix)
app.include_router(units_router, prefix=settings.api_v1_prefix)
app.include_router(products_router, prefix=settings.api_v1_prefix)
app.include_router(prices_router, prefix=settings.api_v1_prefix)
app.include_router(suppliers_router, prefix=settings.api_v1_prefix)
app.include_router(stock_router, prefix=settings.api_v1_prefix)
app.include_router(purchases_router, prefix=settings.api_v1_prefix)
app.include_router(adjustments_router, prefix=settings.api_v1_prefix)
app.include_router(transfers_router, prefix=settings.api_v1_prefix)
app.include_router(waste_router, prefix=settings.api_v1_prefix)
app.include_router(customers_router, prefix=settings.api_v1_prefix)
app.include_router(tax_rates_router, prefix=settings.api_v1_prefix)
app.include_router(discounts_router, prefix=settings.api_v1_prefix)
app.include_router(sales_router, prefix=settings.api_v1_prefix)
app.include_router(search_router, prefix=settings.api_v1_prefix)
app.include_router(returns_router, prefix=settings.api_v1_prefix)
app.include_router(restaurant_tables_router, prefix=settings.api_v1_prefix)
app.include_router(restaurant_modifiers_router, prefix=settings.api_v1_prefix)
app.include_router(restaurant_kot_router, prefix=settings.api_v1_prefix)
app.include_router(manufacturing_bom_router, prefix=settings.api_v1_prefix)
app.include_router(manufacturing_production_router, prefix=settings.api_v1_prefix)
app.include_router(accounting_router, prefix=settings.api_v1_prefix)
app.include_router(registers_router, prefix=settings.api_v1_prefix)
app.include_router(shifts_router, prefix=settings.api_v1_prefix)
app.include_router(expenses_router, prefix=settings.api_v1_prefix)
app.include_router(supplier_ledger_router, prefix=settings.api_v1_prefix)
app.include_router(analytics_router, prefix=settings.api_v1_prefix)
app.include_router(settings_router, prefix=settings.api_v1_prefix)
app.include_router(notifications_router, prefix=settings.api_v1_prefix)
app.include_router(invoice_router, prefix=settings.api_v1_prefix)
app.include_router(audit_router, prefix=settings.api_v1_prefix)
app.include_router(subscription_router, prefix=settings.api_v1_prefix)
app.include_router(sync_router, prefix=settings.api_v1_prefix)
app.include_router(devices_router, prefix=settings.api_v1_prefix)
app.include_router(time_router, prefix=settings.api_v1_prefix)
# Platform owner API and offline-license endpoints live outside the tenant /api/v1 tree.
app.include_router(platform_router)
app.include_router(license_public_router)
app.include_router(time_router)  # /time next to /health for pre-login reachability pings


@app.get("/health")
async def health_check():
    try:
        async with asyncio.timeout(3):
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
    except Exception:
        logger.exception("health check: database unreachable")
        return JSONResponse(
            status_code=503,
            content={"status": "unavailable", "app": settings.app_name, "db": "down"},
        )
    return {"status": "ok", "app": settings.app_name, "db": "up"}
