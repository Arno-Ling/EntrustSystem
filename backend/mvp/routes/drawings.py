"""
Drawing Library CRUD + Project Drawing Links (图纸存储库).

Endpoints:
  GET    /api/internal/drawing-library                    列表（租户隔离，支持筛选）
  POST   /api/internal/drawing-library                    上传图纸
  GET    /api/internal/drawing-library/{id}               详情
  PATCH  /api/internal/drawing-library/{id}               编辑元数据
  DELETE /api/internal/drawing-library/{id}               删除（无关联时）
  GET    /api/internal/drawing-library/{id}/download      下载文件

  GET    /api/internal/projects/{pid}/drawing-links       项目关联图纸列表
  POST   /api/internal/projects/{pid}/drawing-links       关联图纸
  DELETE /api/internal/projects/{pid}/drawing-links/{lid}  取消关联
"""
from __future__ import annotations

import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from mvp import db
from mvp.auth import CurrentUser, require_tenant_type

router = APIRouter(prefix="/api/internal", tags=["drawing-library"])

_require_internal = require_tenant_type("internal")

LIBRARY_DIR = Path(__file__).resolve().parent.parent.parent / "uploads" / "library"
LIBRARY_DIR.mkdir(parents=True, exist_ok=True)


# =============================================================================
# Pydantic
# =============================================================================

class DrawingUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=255)
    description: Optional[str] = None
    category: Optional[str] = Field(None, max_length=64)
    tags: Optional[list[str]] = None


class DrawingLinkCreate(BaseModel):
    drawing_ids: list[int] = Field(..., min_length=1)


# =============================================================================
# Drawing Library CRUD
# =============================================================================

@router.get("/drawing-library")
async def list_drawings(
    user: CurrentUser = Depends(_require_internal),
    category: Optional[str] = None,
    keyword: Optional[str] = None,
):
    """列出当前租户的图纸库，支持分类筛选和名称搜索。"""
    where = "WHERE dl.tenant_id = %s"
    params: list[Any] = [user.tenant_id]
    if category:
        where += " AND dl.category = %s"
        params.append(category)
    if keyword:
        where += " AND dl.name ILIKE %s"
        params.append(f"%{keyword}%")

    rows = db.fetch_all(
        f"""
        SELECT dl.id, dl.name, dl.description, dl.file_name, dl.file_size,
               dl.mime_type, dl.category, dl.tags, dl.uploaded_by,
               dl.created_at, dl.updated_at,
               u.display_name AS uploader_name
        FROM drawing_library dl
        LEFT JOIN users u ON dl.uploaded_by = u.id
        {where}
        ORDER BY dl.created_at DESC
        LIMIT 200
        """,
        tuple(params),
    )
    return {"items": rows, "total": len(rows)}


@router.post("/drawing-library", status_code=201)
async def upload_drawing(
    file: UploadFile = File(...),
    name: str = Form(...),
    description: str = Form(""),
    category: str = Form(""),
    tags: str = Form(""),  # comma-separated
    user: CurrentUser = Depends(_require_internal),
):
    """上传图纸到图纸库。"""
    # 文件大小限制 50MB
    content = await file.read()
    if len(content) > 50 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="文件大小超过限制 (50MB)")

    ext = Path(file.filename or "").suffix
    stored_name = f"{uuid.uuid4().hex}{ext}"
    target = LIBRARY_DIR / stored_name
    target.write_bytes(content)
    file_size = len(content)

    tags_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []

    new_id = db.execute(
        """
        INSERT INTO drawing_library
            (tenant_id, name, description, file_name, file_path, file_size,
             mime_type, category, tags, uploaded_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (user.tenant_id, name, description or None, file.filename,
         f"library/{stored_name}", file_size, file.content_type or "application/octet-stream",
         category or None, tags_list, user.user_id),
    )
    return {"id": new_id, "name": name, "file_name": file.filename}


@router.get("/drawing-library/{drawing_id}")
async def get_drawing(
    drawing_id: int,
    user: CurrentUser = Depends(_require_internal),
):
    row = db.fetch_one(
        """
        SELECT dl.*, u.display_name AS uploader_name
        FROM drawing_library dl
        LEFT JOIN users u ON dl.uploaded_by = u.id
        WHERE dl.id = %s
        """,
        (drawing_id,),
    )
    if row is None or row["tenant_id"] != user.tenant_id:
        raise HTTPException(status_code=404, detail="Drawing not found")
    return row


@router.patch("/drawing-library/{drawing_id}")
async def update_drawing(
    drawing_id: int,
    payload: DrawingUpdate,
    user: CurrentUser = Depends(_require_internal),
):
    row = db.fetch_one(
        "SELECT id, tenant_id FROM drawing_library WHERE id = %s",
        (drawing_id,),
    )
    if row is None or row["tenant_id"] != user.tenant_id:
        raise HTTPException(status_code=404, detail="Drawing not found")

    updates = []
    params = []
    for f, v in payload.model_dump(exclude_unset=True).items():
        updates.append(f"{f} = %s")
        params.append(v)
    if not updates:
        return {"updated": False}

    params.append(drawing_id)
    db.execute(
        f"UPDATE drawing_library SET {', '.join(updates)} WHERE id = %s",
        tuple(params),
    )
    return {"updated": True}


@router.delete("/drawing-library/{drawing_id}")
async def delete_drawing(
    drawing_id: int,
    user: CurrentUser = Depends(_require_internal),
):
    row = db.fetch_one(
        "SELECT id, tenant_id, file_path FROM drawing_library WHERE id = %s",
        (drawing_id,),
    )
    if row is None or row["tenant_id"] != user.tenant_id:
        raise HTTPException(status_code=404, detail="Drawing not found")

    # 检查是否被项目关联
    links = db.fetch_all(
        """
        SELECT p.project_no FROM project_drawing_links pdl
        JOIN projects p ON pdl.project_id = p.id
        WHERE pdl.drawing_id = %s
        """,
        (drawing_id,),
    )
    if links:
        proj_list = ", ".join(l["project_no"] for l in links[:5])
        raise HTTPException(status_code=409,
                            detail=f"该图纸已关联到项目: {proj_list}，无法删除")

    # 删除文件
    uploads_dir = Path(__file__).resolve().parent.parent.parent / "uploads"
    file_path = uploads_dir / row["file_path"]
    if file_path.exists():
        file_path.unlink()

    db.execute("DELETE FROM drawing_library WHERE id = %s", (drawing_id,))
    return {"deleted": True}


@router.get("/drawing-library/{drawing_id}/download")
async def download_drawing(
    drawing_id: int,
    user: CurrentUser = Depends(_require_internal),
):
    row = db.fetch_one(
        "SELECT id, tenant_id, file_name, file_path, mime_type FROM drawing_library WHERE id = %s",
        (drawing_id,),
    )
    if row is None or row["tenant_id"] != user.tenant_id:
        raise HTTPException(status_code=404, detail="Drawing not found")

    uploads_dir = Path(__file__).resolve().parent.parent.parent / "uploads"
    full_path = uploads_dir / row["file_path"]
    if not full_path.exists():
        raise HTTPException(status_code=410, detail="File missing from storage")

    return FileResponse(
        path=str(full_path),
        filename=row["file_name"],
        media_type=row["mime_type"] or "application/octet-stream",
    )


# =============================================================================
# Project Drawing Links
# =============================================================================

@router.get("/projects/{project_id}/drawing-links")
async def list_drawing_links(
    project_id: int,
    user: CurrentUser = Depends(_require_internal),
):
    # 校验项目归属
    proj = db.fetch_one(
        "SELECT id, tenant_id FROM projects WHERE id = %s",
        (project_id,),
    )
    if proj is None or proj["tenant_id"] != user.tenant_id:
        raise HTTPException(status_code=404)

    rows = db.fetch_all(
        """
        SELECT pdl.id AS link_id, pdl.drawing_id, pdl.linked_at,
               dl.name, dl.file_name, dl.category, dl.file_size, dl.mime_type
        FROM project_drawing_links pdl
        JOIN drawing_library dl ON pdl.drawing_id = dl.id
        WHERE pdl.project_id = %s
        ORDER BY pdl.linked_at DESC
        """,
        (project_id,),
    )
    return {"items": rows, "total": len(rows)}


@router.post("/projects/{project_id}/drawing-links", status_code=201)
async def link_drawings(
    project_id: int,
    payload: DrawingLinkCreate,
    user: CurrentUser = Depends(_require_internal),
):
    proj = db.fetch_one(
        "SELECT id, status, tenant_id FROM projects WHERE id = %s",
        (project_id,),
    )
    if proj is None or proj["tenant_id"] != user.tenant_id:
        raise HTTPException(status_code=404)
    if proj["status"] != "drafted":
        raise HTTPException(status_code=409,
                            detail="只有 drafted 状态的项目可以修改图纸关联")

    # 校验所有 drawing_ids 存在且属于当前租户
    if payload.drawing_ids:
        placeholders = ",".join(["%s"] * len(payload.drawing_ids))
        existing = db.fetch_all(
            f"SELECT id FROM drawing_library WHERE id IN ({placeholders}) AND tenant_id = %s",
            (*payload.drawing_ids, user.tenant_id),
        )
        existing_ids = {r["id"] for r in existing}
        invalid = [did for did in payload.drawing_ids if did not in existing_ids]
        if invalid:
            raise HTTPException(status_code=400,
                                detail=f"无效的图纸ID: {invalid}")

    now = datetime.utcnow()
    linked = 0
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            for did in payload.drawing_ids:
                try:
                    cur.execute(
                        """
                        INSERT INTO project_drawing_links (project_id, drawing_id, linked_by, linked_at)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (project_id, drawing_id) DO NOTHING
                        """,
                        (project_id, did, user.user_id, now),
                    )
                    if cur.rowcount:
                        linked += 1
                except Exception:
                    pass

    return {"linked_count": linked}


@router.delete("/projects/{project_id}/drawing-links/{link_id}")
async def unlink_drawing(
    project_id: int,
    link_id: int,
    user: CurrentUser = Depends(_require_internal),
):
    proj = db.fetch_one(
        "SELECT id, status, tenant_id FROM projects WHERE id = %s",
        (project_id,),
    )
    if proj is None or proj["tenant_id"] != user.tenant_id:
        raise HTTPException(status_code=404)
    if proj["status"] != "drafted":
        raise HTTPException(status_code=409,
                            detail="只有 drafted 状态的项目可以修改图纸关联")

    row = db.fetch_one(
        "SELECT id FROM project_drawing_links WHERE id = %s AND project_id = %s",
        (link_id, project_id),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Link not found")

    db.execute("DELETE FROM project_drawing_links WHERE id = %s", (link_id,))
    return {"deleted": True}
