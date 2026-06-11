from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional
from database import get_db_connection
from routers.auth import get_current_user, TokenData

router = APIRouter(prefix="/admin/alert-rules", tags=["预警规则"])

class AlertRuleCreate(BaseModel):
    rule_name: str
    node_type: str
    threshold_hours: int
    is_active: bool = True

class AlertRuleUpdate(BaseModel):
    rule_name: Optional[str] = None
    node_type: Optional[str] = None
    threshold_hours: Optional[int] = None
    is_active: Optional[bool] = None

@router.get("/")
async def list_alert_rules(current_user: TokenData = Depends(get_current_user)):
    if current_user.role != 'admin':
        raise HTTPException(403, "权限不足")
    conn = await get_db_connection()
    try:
        rows = await conn.fetch("SELECT id, rule_name, node_type, threshold_hours, is_active, created_at, updated_at FROM alert_rules ORDER BY id")
        return [dict(r) for r in rows]
    finally:
        await conn.close()

@router.post("/", status_code=201)
async def create_alert_rule(rule: AlertRuleCreate, current_user: TokenData = Depends(get_current_user)):
    if current_user.role != 'admin':
        raise HTTPException(403, "权限不足")
    conn = await get_db_connection()
    try:
        await conn.execute(
            "INSERT INTO alert_rules (rule_name, node_type, threshold_hours, is_active) VALUES ($1, $2, $3, $4)",
            rule.rule_name, rule.node_type, rule.threshold_hours, rule.is_active
        )
        return {"success": True}
    finally:
        await conn.close()

@router.patch("/{rule_id}")
async def update_alert_rule(rule_id: int, update: AlertRuleUpdate, current_user: TokenData = Depends(get_current_user)):
    if current_user.role != 'admin':
        raise HTTPException(403, "权限不足")
    conn = await get_db_connection()
    try:
        updates = []
        params = []
        if update.rule_name is not None:
            updates.append("rule_name = $1")
            params.append(update.rule_name)
        if update.node_type is not None:
            updates.append("node_type = $2")
            params.append(update.node_type)
        if update.threshold_hours is not None:
            updates.append("threshold_hours = $3")
            params.append(update.threshold_hours)
        if update.is_active is not None:
            updates.append("is_active = $4")
            params.append(update.is_active)
        if not updates:
            return {"success": True, "message": "无更新内容"}
        params.append(rule_id)
        query = f"UPDATE alert_rules SET {', '.join(updates)}, updated_at = NOW() WHERE id = ${len(params)}"
        await conn.execute(query, *params)
        return {"success": True}
    finally:
        await conn.close()

@router.delete("/{rule_id}")
async def delete_alert_rule(rule_id: int, current_user: TokenData = Depends(get_current_user)):
    if current_user.role != 'admin':
        raise HTTPException(403, "权限不足")
    conn = await get_db_connection()
    try:
        result = await conn.execute("DELETE FROM alert_rules WHERE id = $1", rule_id)
        if result == "DELETE 0":
            raise HTTPException(404, "规则不存在")
        return {"success": True}
    finally:
        await conn.close()