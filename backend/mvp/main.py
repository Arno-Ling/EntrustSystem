"""
Lightweight MVP FastAPI application (独立入口, 端口 8001).

不修改也不依赖 backend/main.py。旧的 /dashboard 和 /api/exception/analyze
继续在端口 8000 运行。

启动:
    uvicorn mvp.main:app --reload --port 8001 --app-dir backend

访问:
    http://localhost:8001/          -> 跳登录页
    http://localhost:8001/static/   -> 静态资源
    http://localhost:8001/docs      -> Swagger
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from mvp.auth import router as auth_router
from mvp.routes.admin import router as admin_router
from mvp.routes.basis import router as basis_router
from mvp.routes.common import router as common_router
from mvp.routes.internal import router as internal_router
from mvp.routes.material import router as material_router
from mvp.routes.processor import router as processor_router
from mvp.routes.qc import router as qc_router, get_counterparty_router as _qc_counterparty
from mvp.routes.recon import (
    internal_router  as recon_internal_router,
    material_router  as recon_material_router,
    processor_router as recon_processor_router,
)
from mvp.routes.inquiry import (
    internal_router  as inquiry_internal_router,
    material_router  as inquiry_material_router,
    processor_router as inquiry_processor_router,
)
from mvp.routes.materials import router as materials_router
from mvp.routes.drawings import router as drawings_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("mvp")


BACKEND_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BACKEND_DIR / "static"
STATIC_DIR.mkdir(exist_ok=True)
UPLOADS_DIR = BACKEND_DIR / "uploads"
UPLOADS_DIR.mkdir(exist_ok=True)

app = FastAPI(
    title="模具委外采购系统 - 轻量 MVP",
    version="0.1.0",
    description="阶段 0/1/3-简化版：我方 + 加工方二端闭环",
)

# CORS (MVP 单机，全开)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static files
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/uploads", StaticFiles(directory=str(UPLOADS_DIR)), name="uploads")

# Routes
app.include_router(auth_router)
app.include_router(internal_router)
app.include_router(admin_router)
app.include_router(basis_router)
app.include_router(processor_router)
app.include_router(material_router)
app.include_router(qc_router)
app.include_router(_qc_counterparty())
app.include_router(recon_internal_router)
app.include_router(recon_material_router)
app.include_router(recon_processor_router)
app.include_router(inquiry_internal_router)
app.include_router(inquiry_material_router)
app.include_router(inquiry_processor_router)
app.include_router(common_router)
app.include_router(materials_router)
app.include_router(drawings_router)


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/static/login.html", status_code=302)


@app.on_event("startup")
async def on_startup():
    logger.info("MVP app started on port 8001")
    logger.info(f"Static dir: {STATIC_DIR}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("mvp.main:app", host="0.0.0.0", port=8001, reload=True,
                app_dir=str(BACKEND_DIR))
