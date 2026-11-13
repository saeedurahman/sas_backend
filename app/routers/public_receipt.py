"""GET /r/{token} - unauthenticated receipt page (rate limited, token = bearer secret)."""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response

from app.core.rate_limit import ip_rate_limit
from app.database import get_db
from app.services.business_time import get_business_tz
from app.services.invoice_service import get_invoice_data
from app.services.receipt_link_service import LinkProblem, resolve_link
from app.services.receipt_render import error_page, render_html, render_pdf

router = APIRouter(tags=["Public receipt"])

# The page needs inline CSS only; no scripts, no remote loads, never framed.
_HTML_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'",
    "Cache-Control": "no-store",
    "X-Robots-Tag": "noindex, nofollow",
    "Referrer-Policy": "no-referrer",
}


@router.get(
    "/r/{token}",
    response_class=HTMLResponse,
    dependencies=[Depends(ip_rate_limit("receipt-ip", "rate_limit_receipt_ip"))],
)
async def public_receipt(
    request: Request,
    token: str,
    format: str = Query(default="html", pattern="^(html|pdf)$"),
    db=Depends(get_db),
):
    try:
        link, sale_id = await resolve_link(db, token)
        invoice = await get_invoice_data(db, sale_id, link.business_id)
    except LinkProblem as problem:
        return HTMLResponse(error_page(problem.message), status_code=problem.status_code, headers=_HTML_HEADERS)
    except HTTPException as exc:  # sale deleted: same answer as an unknown link
        if exc.status_code != 404:
            raise
        return HTMLResponse(
            error_page("This receipt link is not valid."), status_code=404, headers=_HTML_HEADERS
        )

    zone = await get_business_tz(db, link.business_id)
    if format == "pdf":
        return Response(
            render_pdf(invoice, zone),
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'inline; filename="receipt-{invoice.sale_number}.pdf"',
                "Cache-Control": "no-store",
                "X-Robots-Tag": "noindex, nofollow",
            },
        )
    return HTMLResponse(
        render_html(invoice, zone, pdf_href=f"/r/{token}?format=pdf"), headers=_HTML_HEADERS
    )
