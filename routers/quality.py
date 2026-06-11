from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import List, Optional
from database import get_db_connection
from routers.auth import get_current_user, TokenData

router = APIRouter()

class IQCItem(BaseModel):
    material_code: str
    pass_qty: int
    fail_qty: int

class IQCRequest(BaseModel):
    operator: str
    items: List[IQCItem]

@router.get("/pending")
async def get_pending_documents(current_user: TokenData = Depends(get_current_user)):
    """仅返回待检验的单据（需要认证）"""
    conn = await get_db_connection()
    try:
        rows = await conn.fetch("SELECT doc_number, doc_type, status, updated_at FROM documents WHERE status = '待驗' ORDER BY updated_at")
        return [dict(r) for r in rows]
    finally:
        await conn.close()

@router.get("/pending/{doc_number}")
async def get_pending_document_details(doc_number: str, current_user: TokenData = Depends(get_current_user)):
    conn = await get_db_connection()
    try:
        doc = await conn.fetchrow("SELECT * FROM documents WHERE doc_number = $1", doc_number)
        if not doc or doc['status'] != '待驗':
            raise HTTPException(404, "單據不存在或不在待驗狀態")
        details = await conn.fetch(
            "SELECT material_code, material_name, spec, accepted_qty, reject_qty, required_qty FROM document_details WHERE doc_number = $1",
            doc_number
        )
        return {"document": dict(doc), "details": [dict(d) for d in details]}
    finally:
        await conn.close()

@router.post("/{doc_number}/inspect")
async def qc_inspect(doc_number: str, req: IQCRequest, current_user: TokenData = Depends(get_current_user)):
    """检验接口（仅允许 qc 或 admin 角色）"""
    if current_user.role not in ['qc', 'admin']:
        raise HTTPException(403, "權限不足")
    
    conn = await get_db_connection()
    async with conn.transaction():
        doc = await conn.fetchrow("SELECT status FROM documents WHERE doc_number = $1 FOR UPDATE", doc_number)
        if not doc or doc['status'] != '待驗':
            raise HTTPException(409, "單據狀態不是待驗")
        
        for item in req.items:
            detail = await conn.fetchrow(
                "SELECT id, accepted_qty, required_qty FROM document_details WHERE doc_number = $1 AND material_code = $2 FOR UPDATE",
                doc_number, item.material_code
            )
            if not detail:
                raise HTTPException(404, f"料號 {item.material_code} 不在單據中")
            
            original_qty = detail['accepted_qty'] or detail['required_qty'] or 0
            # 修复：必须检验完所有数量，良品+不良品必须等于原始数量，否则拒绝
            if item.pass_qty + item.fail_qty != original_qty:
                raise HTTPException(400, f"良品+不良品數量 ({item.pass_qty + item.fail_qty}) 不等於實際到貨數量 ({original_qty})")
            
            await conn.execute(
                "UPDATE document_details SET accepted_qty = $1, reject_qty = $2, version = version + 1, updated_at = NOW() WHERE id = $3",
                item.pass_qty, item.fail_qty, detail['id']
            )
        
        # 修复：检验完成后，单据状态改为“檢驗完成”，并移动位置到“待入庫倉”
        await conn.execute(
            "UPDATE documents SET status = '檢驗完成', current_site = '待入庫倉', updated_at = NOW() WHERE doc_number = $1",
            doc_number
        )
    return {"success": True}