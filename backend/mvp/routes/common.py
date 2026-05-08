"""Common routes: health check + home redirect."""
from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse

from mvp.auth import CurrentUser, get_current_user

router = APIRouter(tags=["common"])


@router.get("/health")
async def health():
    return {"status": "healthy", "service": "mvp"}


@router.get("/home")
async def home(user: CurrentUser = Depends(get_current_user)):
    """After login, redirect to the tenant-specific home page."""
    if user.tenant_type == "internal":
        return RedirectResponse(url="/static/internal/home.html", status_code=302)
    elif user.tenant_type == "processor":
        return RedirectResponse(url="/static/processor/home.html", status_code=302)
    elif user.tenant_type == "material":
        return RedirectResponse(url="/static/material/home.html", status_code=302)
    else:
        return RedirectResponse(url="/static/login.html", status_code=302)
