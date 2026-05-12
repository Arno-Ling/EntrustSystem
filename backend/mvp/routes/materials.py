"""
Materials dictionary CRUD (材质字典管理).

Endpoints:
  GET    /api/internal/materials                 活跃材质列表（下拉用）
  GET    /api/internal/admin/materials           全部材质（含停用，管理用）
  POST   /api/internal/admin/materials           新建
  PATCH  /api/internal/admin/materials/{id}      编辑
  DELETE /api/internal/admin/materials/{id}      删除（无引用时）
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from mvp import db
from mvp.auth import CurrentUser, require_tenant_type

router = APIRouter(prefix="/api/internal", tags=["materials"])

_require_internal = require_tenant_type("internal")


# =============================================================================
# Pydantic
# =============================================================================

class MaterialCreate(BaseModel):
    code: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=128)
    category: Optional[str] = Field(None, max_length=64)
    remark: Optional[str] = None
    is_active: bool = True


class MaterialUpdate(BaseModel):
    code: Optional[str] = Field(None, max_length=64)
    name: Optional[str] = Field(None, max_length=128)
    category: Optional[str] = Field(None, max_length=64)
    remark: Optional[str] = None
    is_active: Optional[bool] = None


# =============================================================================
# 下拉用：只返回活跃材质
# =============================================================================

@router.get("/materials")
async def list_active_materials(user: CurrentUser = Depends(_require_internal)):
    """返回所有活跃材质，按 category + code 排序（供前端下拉选择）。"""
    rows = db.fetch_all(
        """
        SELECT id, code, name, category, remark
        FROM materials
        WHERE is_active = TRUE
        ORDER BY category ASC NULLS LAST, code ASC
        """
    )
    return {"items": rows, "total": len(rows)}


# =============================================================================
# 管理用：全部材质 CRUD
# =============================================================================

@router.get("/admin/materials")
async def list_all_materials(user: CurrentUser = Depends(_require_internal)):
    """返回全部材质（含停用），管理页面用。"""
    rows = db.fetch_all(
        """
        SELECT id, code, name, category, remark, is_active, created_at
        FROM materials
        ORDER BY category ASC NULLS LAST, code ASC
        """
    )
    return {"items": rows, "total": len(rows)}


@router.post("/admin/materials", status_code=201)
async def create_material(
    payload: MaterialCreate,
    user: CurrentUser = Depends(_require_internal),
):
    try:
        new_id = db.execute(
            """
            INSERT INTO materials (code, name, category, remark, is_active)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (payload.code, payload.name, payload.category,
             payload.remark, payload.is_active),
        )
    except Exception as e:
        if "materials_code_key" in str(e) or "duplicate" in str(e).lower():
            raise HTTPException(status_code=409,
                                detail=f"材质编码已存在: {payload.code}")
        raise
    return {"id": new_id, "code": payload.code}


@router.patch("/admin/materials/{material_id}")
async def update_material(
    material_id: int,
    payload: MaterialUpdate,
    user: CurrentUser = Depends(_require_internal),
):
    row = db.fetch_one("SELECT id FROM materials WHERE id = %s", (material_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="Material not found")

    updates = []
    params = []
    for f, v in payload.model_dump(exclude_unset=True).items():
        updates.append(f"{f} = %s")
        params.append(v)
    if not updates:
        return {"updated": False}

    params.append(material_id)
    try:
        db.execute(
            f"UPDATE materials SET {', '.join(updates)} WHERE id = %s",
            tuple(params),
        )
    except Exception as e:
        if "materials_code_key" in str(e) or "duplicate" in str(e).lower():
            raise HTTPException(status_code=409,
                                detail=f"材质编码已存在: {payload.code}")
        raise
    return {"updated": True}


@router.delete("/admin/materials/{material_id}")
async def delete_material(
    material_id: int,
    user: CurrentUser = Depends(_require_internal),
):
    row = db.fetch_one("SELECT id, code FROM materials WHERE id = %s", (material_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="Material not found")

    # 检查是否被引用
    ref = db.fetch_one(
        "SELECT COUNT(*) AS cnt FROM project_parts WHERE material_id = %s",
        (material_id,),
    )
    if ref and ref["cnt"] > 0:
        raise HTTPException(status_code=409,
                            detail=f"该材质已被 {ref['cnt']} 个零件引用，无法删除")

    db.execute("DELETE FROM materials WHERE id = %s", (material_id,))
    return {"deleted": True}
