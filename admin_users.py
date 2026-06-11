from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime, timedelta
from database import get_db_connection
from routers.auth import get_current_user, TokenData

router = APIRouter(prefix="/admin/users", tags=["用户管理"])

class UserCreate(BaseModel):
    username: str
    password: str
    role: str
    full_name: Optional[str] = None
    department: Optional[str] = None

class UserUpdate(BaseModel):
    password: Optional[str] = None
    role: Optional[str] = None
    full_name: Optional[str] = None
    department: Optional[str] = None
    is_active: Optional[bool] = None
    quick_login_token: Optional[str] = None

class UserResponse(BaseModel):
    id: int
    username: str
    role: str
    full_name: Optional[str] = None
    department: Optional[str] = None
    is_active: bool = True
    quick_login_token: Optional[str] = None
    created_at: datetime

def hash_password(password: str) -> str:
    return password  # 明文，仅测试

@router.get("/", response_model=List[UserResponse])
async def list_users(
    current_user: TokenData = Depends(get_current_user),
    role: Optional[str] = Query(None)
):
    if current_user.role != 'admin':
        raise HTTPException(403, "只有管理员可以查看用户列表")
    conn = await get_db_connection()
    try:
        if role:
            rows = await conn.fetch(
                "SELECT id, username, role, full_name, department, is_active, quick_login_token, created_at FROM system_users WHERE role = $1 ORDER BY id",
                role
            )
        else:
            rows = await conn.fetch(
                "SELECT id, username, role, full_name, department, is_active, quick_login_token, created_at FROM system_users ORDER BY id"
            )
        result = []
        for row in rows:
            item = dict(row)
            if item.get('is_active') is None:
                item['is_active'] = True
            result.append(item)
        return result
    finally:
        await conn.close()

@router.post("/", status_code=201)
async def create_user(user: UserCreate, current_user: TokenData = Depends(get_current_user)):
    if current_user.role != 'admin':
        raise HTTPException(403, "只有管理员可以创建用户")
    conn = await get_db_connection()
    try:
        existing = await conn.fetchrow("SELECT id FROM system_users WHERE username = $1", user.username)
        if existing:
            raise HTTPException(409, "用户名已存在")
        hashed_pw = hash_password(user.password)
        await conn.execute(
            "INSERT INTO system_users (username, hashed_password, role, full_name, department) VALUES ($1, $2, $3, $4, $5)",
            user.username, hashed_pw, user.role, user.full_name, user.department
        )
        return {"success": True, "message": "用户创建成功"}
    finally:
        await conn.close()

@router.patch("/{user_id}")
async def update_user(user_id: int, update: UserUpdate, current_user: TokenData = Depends(get_current_user)):
    if current_user.role != 'admin':
        raise HTTPException(403, "只有管理员可以修改用户")
    conn = await get_db_connection()
    try:
        current_db_user = await conn.fetchrow("SELECT id FROM system_users WHERE username = $1", current_user.username)
        if current_db_user and current_db_user['id'] == user_id:
            raise HTTPException(400, "不能修改自己的账号")
        
        user = await conn.fetchrow("SELECT id FROM system_users WHERE id = $1", user_id)
        if not user:
            raise HTTPException(404, "用户不存在")
        
        updates = []
        params = []
        if update.password:
            updates.append("hashed_password = $1")
            params.append(hash_password(update.password))
        if update.role is not None:
            updates.append("role = $2")
            params.append(update.role)
        if update.full_name is not None:
            updates.append("full_name = $3")
            params.append(update.full_name)
        if update.department is not None:
            updates.append("department = $4")
            params.append(update.department)
        if update.is_active is not None:
            updates.append("is_active = $5")
            params.append(update.is_active)
        if update.quick_login_token is not None:
            updates.append("quick_login_token = $6")
            params.append(update.quick_login_token)
        if not updates:
            return {"success": True, "message": "无更新内容"}
        params.append(user_id)
        query = f"UPDATE system_users SET {', '.join(updates)}, updated_at = NOW() WHERE id = ${len(params)}"
        await conn.execute(query, *params)
        return {"success": True, "message": "更新成功"}
    finally:
        await conn.close()

@router.delete("/{user_id}")
async def delete_user(user_id: int, current_user: TokenData = Depends(get_current_user)):
    if current_user.role != 'admin':
        raise HTTPException(403, "只有管理员可以删除用户")
    conn = await get_db_connection()
    try:
        current_db_user = await conn.fetchrow("SELECT id FROM system_users WHERE username = $1", current_user.username)
        if current_db_user and current_db_user['id'] == user_id:
            raise HTTPException(400, "不能删除自己")
        
        target_role = await conn.fetchval("SELECT role FROM system_users WHERE id = $1", user_id)
        if target_role in ['admin', 'warehouse', 'receiver', 'qc', 'packer']:
            raise HTTPException(403, "固定角色账号不可删除")
        
        result = await conn.execute("DELETE FROM system_users WHERE id = $1", user_id)
        if result == "DELETE 0":
            raise HTTPException(404, "用户不存在")
        return {"success": True}
    finally:
        await conn.close()

@router.post("/{user_id}/generate-qr-token")
async def generate_qr_token(user_id: int, current_user: TokenData = Depends(get_current_user)):
    if current_user.role != 'admin':
        raise HTTPException(403, "只有管理员可以生成QR码")
    import secrets
    new_token = secrets.token_urlsafe(16)
    conn = await get_db_connection()
    try:
        user = await conn.fetchrow("SELECT id FROM system_users WHERE id = $1", user_id)
        if not user:
            raise HTTPException(404, "用户不存在")
        await conn.execute(
            "UPDATE system_users SET quick_login_token = $1, updated_at = NOW() WHERE id = $2",
            new_token, user_id
        )
        return {"token": new_token, "qr_url": f"http://127.0.0.1:8000/static/quick_login.html?token={new_token}"}
    finally:
        await conn.close()

# 根据角色获取用户（用于指派下拉框）
@router.get("/by-role/{role_name}")
async def get_users_by_role(
    role_name: str,
    current_user: TokenData = Depends(get_current_user)
):
    if current_user.role not in ['admin', 'warehouse']:
        raise HTTPException(403, "权限不足")
    conn = await get_db_connection()
    try:
        rows = await conn.fetch(
            "SELECT id, username, full_name FROM system_users WHERE role = $1 AND is_active = true ORDER BY username",
            role_name
        )
        return [{"id": r["id"], "username": r["username"], "full_name": r["full_name"]} for r in rows]
    finally:
        await conn.close()

# 管理后台概览接口
@router.get("/admin/dashboard/overview")
async def get_admin_overview(current_user: TokenData = Depends(get_current_user)):
    if current_user.role != 'admin':
        raise HTTPException(403, "权限不足")
    
    conn = await get_db_connection()
    try:
        # 1. 关键任务统计
        inbound_pending = await conn.fetchval("SELECT COUNT(*) FROM inbound_tasks WHERE status != 'completed'")
        picking_pending = await conn.fetchval(
            "SELECT COUNT(*) FROM documents WHERE doc_type = 'manufacture' AND picking_status IN ('待指派', '待檢貨', '進行中')"
        )
        stocktake_pending = await conn.fetchval(
            "SELECT COUNT(*) FROM stock_take_sheets WHERE status IN ('待盤點', '進行中')"
        )
        
        # 2. 库存预警（库存低于10）
        low_stock_items = await conn.fetch(
            "SELECT material_code, name, spec, stock_qty, location FROM materials WHERE stock_qty < 10 ORDER BY stock_qty LIMIT 20"
        )
        low_stock_list = [dict(item) for item in low_stock_items]
        
        # 3. 系统状态（健康检查）
        db_ok = True
        try:
            await conn.fetchval("SELECT 1")
        except:
            db_ok = False
        
        # 4. 超时预警（根据 alert_rules 表）
        rules = await conn.fetch("SELECT id, rule_name, node_type, threshold_hours FROM alert_rules WHERE is_active = true")
        now_utc = datetime.utcnow()   # 使用 UTC 时间，确保 naive
        timeout_alerts = []
        
        for rule in rules:
            node_type = rule['node_type']
            threshold_hours = rule['threshold_hours']
            threshold_time = now_utc - timedelta(hours=threshold_hours)
            
            if node_type == '待驗倉':
                rows = await conn.fetch(
                    "SELECT doc_number, doc_type, created_at, status, '待驗倉' as current_site FROM documents WHERE status = '待驗' AND created_at < $1",
                    threshold_time
                )
                for r in rows:
                    created_at = r['created_at'].replace(tzinfo=None) if r['created_at'].tzinfo else r['created_at']
                    diff = now_utc - created_at
                    waiting_hours = round(diff.total_seconds() / 3600, 1)
                    timeout_alerts.append({
                        "rule_name": rule['rule_name'],
                        "node_type": node_type,
                        "doc_number": r['doc_number'],
                        "doc_type": r['doc_type'],
                        "current_site": r['current_site'],
                        "created_at": created_at.isoformat(),
                        "waiting_hours": waiting_hours
                    })
            elif node_type == '待入庫倉':
                rows = await conn.fetch(
                    "SELECT doc_number, doc_type, updated_at as reference_time, '待入庫倉' as current_site FROM documents WHERE status = '檢驗完成' AND updated_at < $1",
                    threshold_time
                )
                for r in rows:
                    ref_time = r['reference_time'].replace(tzinfo=None) if r['reference_time'].tzinfo else r['reference_time']
                    diff = now_utc - ref_time
                    waiting_hours = round(diff.total_seconds() / 3600, 1)
                    timeout_alerts.append({
                        "rule_name": rule['rule_name'],
                        "node_type": node_type,
                        "doc_number": r['doc_number'],
                        "doc_type": r['doc_type'],
                        "current_site": r['current_site'],
                        "created_at": ref_time.isoformat(),
                        "waiting_hours": waiting_hours
                    })
            elif node_type == '待出庫倉':
                # 以 documents.current_site = '待出貨倉' 为例
                rows = await conn.fetch(
                    "SELECT doc_number, doc_type, current_site, updated_at as reference_time FROM documents WHERE current_site = '待出貨倉' AND updated_at < $1",
                    threshold_time
                )
                for r in rows:
                    ref_time = r['reference_time'].replace(tzinfo=None) if r['reference_time'].tzinfo else r['reference_time']
                    diff = now_utc - ref_time
                    waiting_hours = round(diff.total_seconds() / 3600, 1)
                    timeout_alerts.append({
                        "rule_name": rule['rule_name'],
                        "node_type": node_type,
                        "doc_number": r['doc_number'],
                        "doc_type": r['doc_type'],
                        "current_site": r['current_site'],
                        "created_at": ref_time.isoformat(),
                        "waiting_hours": waiting_hours
                    })
            elif node_type == '包裝成品倉':
                rows = await conn.fetch(
                    "SELECT box_number, parent_doc_number as doc_number, current_site, updated_at as reference_time FROM boxes WHERE current_site = '包裝成品倉' AND updated_at < $1",
                    threshold_time
                )
                for r in rows:
                    ref_time = r['reference_time'].replace(tzinfo=None) if r['reference_time'].tzinfo else r['reference_time']
                    diff = now_utc - ref_time
                    waiting_hours = round(diff.total_seconds() / 3600, 1)
                    timeout_alerts.append({
                        "rule_name": rule['rule_name'],
                        "node_type": node_type,
                        "doc_number": r['doc_number'],
                        "box_number": r['box_number'],
                        "current_site": r['current_site'],
                        "created_at": ref_time.isoformat(),
                        "waiting_hours": waiting_hours
                    })
            elif node_type == '待出貨倉':
                rows = await conn.fetch(
                    "SELECT box_number, parent_doc_number as doc_number, current_site, updated_at as reference_time FROM boxes WHERE current_site = '待出貨倉' AND updated_at < $1",
                    threshold_time
                )
                for r in rows:
                    ref_time = r['reference_time'].replace(tzinfo=None) if r['reference_time'].tzinfo else r['reference_time']
                    diff = now_utc - ref_time
                    waiting_hours = round(diff.total_seconds() / 3600, 1)
                    timeout_alerts.append({
                        "rule_name": rule['rule_name'],
                        "node_type": node_type,
                        "doc_number": r['doc_number'],
                        "box_number": r['box_number'],
                        "current_site": r['current_site'],
                        "created_at": ref_time.isoformat(),
                        "waiting_hours": waiting_hours
                    })
        
        return {
            "task_summary": {
                "inbound_pending": inbound_pending,
                "picking_pending": picking_pending,
                "stocktake_pending": stocktake_pending,
                "total_pending": inbound_pending + picking_pending + stocktake_pending
            },
            "low_stock_items": low_stock_list,
            "system_status": {
                "api": "healthy",
                "database": "connected" if db_ok else "disconnected",
                "timestamp": datetime.utcnow().isoformat()
            },
            "timeout_alerts": timeout_alerts
        }
    finally:
        await conn.close()
# ========== 预警规则管理 API ==========
from pydantic import BaseModel
from typing import Optional

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

@router.get("/alert-rules")
async def list_alert_rules(current_user: TokenData = Depends(get_current_user)):
    if current_user.role != 'admin':
        raise HTTPException(403, "权限不足")
    conn = await get_db_connection()
    try:
        rows = await conn.fetch("SELECT id, rule_name, node_type, threshold_hours, is_active, created_at, updated_at FROM alert_rules ORDER BY id")
        return [dict(r) for r in rows]
    finally:
        await conn.close()

@router.post("/alert-rules", status_code=201)
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

@router.patch("/alert-rules/{rule_id}")
async def update_alert_rule(rule_id: int, update: AlertRuleUpdate, current_user: TokenData = Depends(get_current_user)):
    if current_user.role != 'admin':
        raise HTTPException(403, "权限不足")
    conn = await get_db_connection()
    try:
        # 构建动态更新语句
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

@router.delete("/alert-rules/{rule_id}")
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
