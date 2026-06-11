from fastapi import APIRouter, HTTPException, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime, date
import io
import xlsxwriter
from urllib.parse import quote
from database import get_db_connection
from routers.auth import get_current_user, TokenData

router = APIRouter(prefix="/transactions", tags=["領退料"])

class TransactionItem(BaseModel):
    material_code: str
    quantity: int
    remark: Optional[str] = None

class TransactionCreate(BaseModel):
    doc_number: str
    transaction_type: str   # 'ISSUE' or 'RETURN'
    department: str
    items: List[TransactionItem]

class TransactionResponse(BaseModel):
    id: int
    doc_number: str
    transaction_type: str
    department: Optional[str]
    operator: str
    created_at: datetime
    items: Optional[List[dict]] = None

# ---------- 辅助函数 ----------
def _parse_query_date(value: str) -> date:
    """解析前端日期参数（支持 YYYY-MM-DD 或 ISO datetime）"""
    raw = value.strip()
    if "T" in raw:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    return datetime.strptime(raw[:10], "%Y-%m-%d").date()

# ---------- API 接口 ----------
@router.get("/generate-doc-number")
async def get_new_doc_number(current_user: TokenData = Depends(get_current_user)):
    if current_user.role not in ['admin', 'warehouse', 'operator']:
        raise HTTPException(403, "權限不足")
    conn = await get_db_connection()
    try:
        today = datetime.now().strftime("%Y%m%d")
        count = await conn.fetchval("SELECT COUNT(*) FROM material_transactions WHERE doc_number LIKE $1", f"LT-{today}-%")
        new_number = f"LT-{today}-{count+1:03d}"
        return {"doc_number": new_number}
    finally:
        await conn.close()

@router.get("/departments")
async def list_departments(current_user: TokenData = Depends(get_current_user)):
    if current_user.role not in ['admin', 'warehouse', 'operator']:
        raise HTTPException(403, "權限不足")
    conn = await get_db_connection()
    try:
        rows = await conn.fetch("SELECT name FROM departments WHERE is_active = true ORDER BY name")
        return [{"name": r["name"]} for r in rows]
    finally:
        await conn.close()

@router.post("/", status_code=201)
async def create_transaction(transaction: TransactionCreate, current_user: TokenData = Depends(get_current_user)):
    if current_user.role not in ['admin', 'warehouse', 'operator']:
        raise HTTPException(403, "權限不足")
    
    conn = await get_db_connection()
    async with conn.transaction():
        existing = await conn.fetchval("SELECT id FROM material_transactions WHERE doc_number = $1", transaction.doc_number)
        if existing:
            raise HTTPException(409, "單據編號已存在")
        
        row = await conn.fetchrow(
            "INSERT INTO material_transactions (doc_number, transaction_type, department, operator) VALUES ($1, $2, $3, $4) RETURNING id",
            transaction.doc_number, transaction.transaction_type, transaction.department, current_user.username
        )
        trans_id = row["id"]
        
        for item in transaction.items:
            material = await conn.fetchrow("SELECT stock_qty, version, name, spec FROM materials WHERE material_code = $1", item.material_code)
            if not material:
                raise HTTPException(404, f"物料 {item.material_code} 不存在")
            
            if transaction.transaction_type == 'ISSUE' and material['stock_qty'] < item.quantity:
                raise HTTPException(400, f"物料 {item.material_code} 庫存不足 (現有 {material['stock_qty']})")
            
            new_stock = material['stock_qty'] - item.quantity if transaction.transaction_type == 'ISSUE' else material['stock_qty'] + item.quantity
            await conn.execute(
                "UPDATE materials SET stock_qty = $1, version = version + 1, updated_at = NOW() WHERE material_code = $2 AND version = $3",
                new_stock, item.material_code, material['version']
            )
            
            await conn.execute(
                "INSERT INTO material_transaction_items (transaction_id, material_code, quantity, remark) VALUES ($1, $2, $3, $4)",
                trans_id, item.material_code, item.quantity, item.remark
            )
            
            change_type = 'OUTBOUND' if transaction.transaction_type == 'ISSUE' else 'INBOUND'
            remark = f"領料單 {transaction.doc_number}" if transaction.transaction_type == 'ISSUE' else f"退料單 {transaction.doc_number}"
            await conn.execute(
                """
                INSERT INTO stock_ledger (material_code, warehouse, change_type, change_qty, before_qty, after_qty, reference_doc, operator, remark)
                VALUES ($1, '主儲倉', $2, $3, $4, $5, $6, $7, $8)
                """,
                item.material_code, change_type, item.quantity, material['stock_qty'], new_stock, transaction.doc_number, current_user.username, remark
            )
        
        return {"success": True, "message": "提交成功", "doc_number": transaction.doc_number}

@router.get("/")
async def list_transactions(
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    transaction_type: Optional[str] = Query(None),
    limit: int = 20,
    offset: int = 0,
    current_user: TokenData = Depends(get_current_user)
):
    """查询历史领退料单（分页）"""
    if current_user.role not in ['admin', 'warehouse', 'operator']:
        raise HTTPException(403, "權限不足")
    conn = await get_db_connection()
    try:
        conditions = []
        params = []
        if start_date:
            conditions.append(f"t.created_at::date >= ${len(params) + 1}")
            params.append(_parse_query_date(start_date))
        if end_date:
            conditions.append(f"t.created_at::date <= ${len(params) + 1}")
            params.append(_parse_query_date(end_date))
        if transaction_type:
            conditions.append(f"t.transaction_type = ${len(params) + 1}")
            params.append(transaction_type)
        where_clause = " AND ".join(conditions) if conditions else "1=1"
        
        total = await conn.fetchval(f"SELECT COUNT(*) FROM material_transactions t WHERE {where_clause}", *params)
        limit_idx = len(params) + 1
        offset_idx = len(params) + 2
        # 修改：LEFT JOIN system_users 获取 full_name，若无则回退到 t.operator
        query = f"""
            SELECT t.id, t.doc_number, t.transaction_type, t.department, 
                   COALESCE(u.full_name, t.operator) AS operator,
                   t.created_at
            FROM material_transactions t
            LEFT JOIN system_users u ON t.operator = u.username
            WHERE {where_clause}
            ORDER BY t.created_at DESC
            LIMIT ${limit_idx} OFFSET ${offset_idx}
        """
        rows = await conn.fetch(query, *params, limit, offset)
        result = []
        for r in rows:
            items = await conn.fetch(
                "SELECT material_code, quantity, remark FROM material_transaction_items WHERE transaction_id = $1",
                r["id"]
            )
            utc_iso = r["created_at"].isoformat() + '+00:00'
            result.append({
                "id": r["id"],
                "doc_number": r["doc_number"],
                "transaction_type": r["transaction_type"],
                "department": r["department"],
                "operator": r["operator"],   # 現在是 full_name 或 username
                "created_at": utc_iso,
                "items": [dict(i) for i in items]
            })
        return {"data": result, "total": total}
    finally:
        await conn.close()

# ========== 导出 Excel ==========
@router.get("/export")
async def export_transactions(
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    transaction_type: Optional[str] = Query(None),
    current_user: TokenData = Depends(get_current_user)
):
    """导出领退料历史到 Excel（含明细行，每个物料一行）"""
    if current_user.role not in ['admin', 'warehouse', 'operator']:
        raise HTTPException(403, "權限不足")
    
    conn = await get_db_connection()
    try:
        conditions = []
        params = []
        if start_date:
            conditions.append(f"t.created_at::date >= ${len(params)+1}")
            params.append(_parse_query_date(start_date))
        if end_date:
            conditions.append(f"t.created_at::date <= ${len(params)+1}")
            params.append(_parse_query_date(end_date))
        if transaction_type:
            conditions.append(f"t.transaction_type = ${len(params)+1}")
            params.append(transaction_type)
        where_clause = " AND ".join(conditions) if conditions else "1=1"
        
        # 同样 JOIN system_users 取得 full_name
        rows = await conn.fetch(f"""
            SELECT t.id, t.doc_number, t.transaction_type, t.department, 
                   COALESCE(u.full_name, t.operator) AS operator,
                   t.created_at
            FROM material_transactions t
            LEFT JOIN system_users u ON t.operator = u.username
            WHERE {where_clause}
            ORDER BY t.created_at DESC
        """, *params)
        
        if not rows:
            raise HTTPException(404, "無資料可匯出")
        
        output = io.BytesIO()
        workbook = xlsxwriter.Workbook(output, {'in_memory': True})
        worksheet = workbook.add_worksheet("領退料歷史")
        headers = ["單據編號", "類型", "單位", "經手人", "時間", "料號", "品名", "規格", "數量", "備註"]
        for col, h in enumerate(headers):
            worksheet.write(0, col, h)
        
        row_idx = 1
        for r in rows:
            items = await conn.fetch(
                "SELECT material_code, quantity, remark FROM material_transaction_items WHERE transaction_id = $1",
                r["id"]
            )
            if not items:
                worksheet.write(row_idx, 0, r["doc_number"])
                worksheet.write(row_idx, 1, "領料" if r["transaction_type"] == "ISSUE" else "退料")
                worksheet.write(row_idx, 2, r["department"] or "")
                worksheet.write(row_idx, 3, r["operator"])  # 已经是姓名
                worksheet.write(row_idx, 4, r["created_at"].isoformat() + '+00:00')
                row_idx += 1
            else:
                for it in items:
                    mat = await conn.fetchrow("SELECT name, spec FROM materials WHERE material_code = $1", it["material_code"])
                    worksheet.write(row_idx, 0, r["doc_number"])
                    worksheet.write(row_idx, 1, "領料" if r["transaction_type"] == "ISSUE" else "退料")
                    worksheet.write(row_idx, 2, r["department"] or "")
                    worksheet.write(row_idx, 3, r["operator"])  # 已經改為姓名
                    worksheet.write(row_idx, 4, r["created_at"].isoformat() + '+00:00')
                    worksheet.write(row_idx, 5, it["material_code"])
                    worksheet.write(row_idx, 6, mat["name"] if mat else "")
                    worksheet.write(row_idx, 7, mat["spec"] if mat else "")
                    worksheet.write(row_idx, 8, it["quantity"])
                    worksheet.write(row_idx, 9, it["remark"] or "")
                    row_idx += 1
        
        workbook.close()
        output.seek(0)
        filename = f"领退料历史_{datetime.now().strftime('%Y%m%d')}.xlsx"
        encoded_filename = quote(filename, encoding='utf-8')
        headers = {
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"
        }
        return StreamingResponse(
            output,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers=headers
        )
    except Exception as e:
        print(f"导出错误: {e}")
        raise HTTPException(500, f"导出失败: {str(e)}")
    finally:
        await conn.close()