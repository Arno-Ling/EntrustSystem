"""
全流程节点追踪（Stage 7-KANBAN）

10 个节点按业务事件触发状态更新，写入 workflow_tracking 表。
在业务代码里调用 track(project_id, node_code, action) 即可。

节点清单：
  1  drafting     草稿中
  2  confirmed    项目确认
  3  deciding     决策中
  4  decided      决策完成
  5  purchasing   采购中
  6  producing    生产中（自制/外协都算）
  7  outsourcing  外协中
  8  inspecting   质检中
  9  exception    异常处理中
 10  delivered    已交付
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from mvp import db

NODES: list[tuple[str, str]] = [
    ("drafting",    "草稿"),
    ("confirmed",   "项目确认"),
    ("deciding",    "决策中"),
    ("decided",     "决策完成"),
    ("purchasing",  "采购中"),
    ("producing",   "生产中"),
    ("outsourcing", "外协中"),
    ("inspecting",  "质检中"),
    ("exception",   "异常处理"),
    ("delivered",   "已交付"),
]
NODE_ORDER = {code: i+1 for i, (code, _) in enumerate(NODES)}
NODE_NAME  = dict(NODES)


def ensure_nodes(project_id: int) -> None:
    """首次建项目时调用：把 10 个节点插成 pending 行。"""
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            for code, name in NODES:
                cur.execute(
                    """
                    INSERT INTO workflow_tracking
                        (project_id, node_code, node_name, node_order, status)
                    VALUES (%s, %s, %s, %s, 'pending')
                    ON CONFLICT (project_id, node_code) DO NOTHING
                    """,
                    (project_id, code, name, NODE_ORDER[code]),
                )


def track(project_id: int, node_code: str, status: str,
          actor_user_id: Optional[int] = None, remark: Optional[str] = None) -> None:
    """推进一个节点的状态。
    status: pending / in_progress / done / skipped / blocked / on_hold
    规则：
      - 进入 in_progress → started_at = now（若无）
      - 进入 done         → ended_at   = now（若无），计算 duration_hours
    """
    if node_code not in NODE_ORDER:
        return

    now = datetime.utcnow()
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            # 确保行存在
            cur.execute(
                """
                INSERT INTO workflow_tracking
                    (project_id, node_code, node_name, node_order, status)
                VALUES (%s, %s, %s, %s, 'pending')
                ON CONFLICT (project_id, node_code) DO NOTHING
                """,
                (project_id, node_code, NODE_NAME[node_code], NODE_ORDER[node_code]),
            )

            # 查当前
            cur.execute(
                "SELECT started_at, ended_at FROM workflow_tracking WHERE project_id=%s AND node_code=%s",
                (project_id, node_code),
            )
            row = cur.fetchone()
            started_at, ended_at = (row[0], row[1]) if row else (None, None)

            new_started = started_at
            new_ended   = ended_at
            duration    = None

            if status == "in_progress" and not started_at:
                new_started = now
            if status == "done":
                new_started = started_at or now
                new_ended   = now
                if new_started and new_ended:
                    duration = (new_ended - new_started).total_seconds() / 3600.0

            cur.execute(
                """
                UPDATE workflow_tracking
                SET status = %s,
                    started_at = %s,
                    ended_at = %s,
                    duration_hours = COALESCE(%s, duration_hours),
                    actor_user_id = COALESCE(%s, actor_user_id),
                    remark = COALESCE(%s, remark)
                WHERE project_id = %s AND node_code = %s
                """,
                (status, new_started, new_ended, duration,
                 actor_user_id, remark, project_id, node_code),
            )


def hold(project_id: int, node_code: str, reason: str,
         actor_user_id: Optional[int] = None) -> None:
    """挂起某节点。"""
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE workflow_tracking
                SET status = 'on_hold', is_blocking = TRUE, blocker_reason = %s,
                    actor_user_id = COALESCE(%s, actor_user_id)
                WHERE project_id = %s AND node_code = %s
                """,
                (reason, actor_user_id, project_id, node_code),
            )


def skip(project_id: int, node_code: str, reason: str,
         actor_user_id: Optional[int] = None) -> None:
    """跳过某节点。"""
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE workflow_tracking
                SET status='skipped', remark=%s,
                    actor_user_id = COALESCE(%s, actor_user_id)
                WHERE project_id = %s AND node_code = %s
                """,
                (reason, actor_user_id, project_id, node_code),
            )
