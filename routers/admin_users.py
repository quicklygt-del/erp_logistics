from fastapi import APIRouter, HTTPException, Depends, Query, Request
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime
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
    is_active: Optional[bool] = True
    quick_login_token: Optional[str] = None
    created_at: datetime

def hash_password(password: str) -> str:
    return password

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
        for r in rows:
            item = dict(r)
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
            updates.append(f"hashed_password = ${len(params) + 1}")
            params.append(hash_password(update.password))
        if update.role is not None:
            updates.append(f"role = ${len(params) + 1}")
            params.append(update.role)
        if update.full_name is not None:
            updates.append(f"full_name = ${len(params) + 1}")
            params.append(update.full_name)
        if update.department is not None:
            updates.append(f"department = ${len(params) + 1}")
            params.append(update.department)
        if update.is_active is not None:
            updates.append(f"is_active = ${len(params) + 1}")
            params.append(update.is_active)
        if update.quick_login_token is not None:
            updates.append(f"quick_login_token = ${len(params) + 1}")
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

        result = await conn.execute("DELETE FROM system_users WHERE id = $1", user_id)
        if result == "DELETE 0":
            raise HTTPException(404, "用户不存在")
        return {"success": True}
    finally:
        await conn.close()

@router.post("/{user_id}/generate-qr-token")
async def generate_qr_token(user_id: int, request: Request, current_user: TokenData = Depends(get_current_user)):
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
        base = str(request.base_url).rstrip("/")
        return {"token": new_token, "qr_url": f"{base}/quick_login.html?token={new_token}"}
    finally:
        await conn.close()

@router.get("/by-role/{role_name}")
async def get_users_by_role(role_name: str, current_user: TokenData = Depends(get_current_user)):
    if current_user.role not in ['admin', 'warehouse']:
        raise HTTPException(403, "權限不足")
    conn = await get_db_connection()
    try:
        rows = await conn.fetch(
            "SELECT id, username, full_name FROM system_users WHERE role = $1 AND is_active = true ORDER BY username",
            role_name
        )
        return [{"id": r["id"], "username": r["username"], "full_name": r["full_name"]} for r in rows]
    finally:
        await conn.close()