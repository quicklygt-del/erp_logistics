from fastapi import APIRouter, HTTPException, Query, UploadFile, File, Depends
from typing import List, Optional
from datetime import datetime, timedelta
import pandas as pd
import io
import xlsxwriter
from fastapi.responses import StreamingResponse
from database import get_db_connection
from pydantic import BaseModel
from routers.auth import get_current_user, TokenData

router = APIRouter()

class ReceiveItem(BaseModel):
    material_code: str
    received_qty: float

class CreateSheetRequest(BaseModel):
    sheet_no: str
    sheet_name: Optional[str] = None
    warehouse: str
    sheet_type: str = "OPEN"
    created_by: str

class AssignSheetRequest(BaseModel):
    assigned_to: str

class ScanCountRequest(BaseModel):
    material_code: str
    counted_qty: int
    operator: str

# ---------- 1. 創建盤點單 ----------
@router.post("/sheets")
async def create_sheet(req: CreateSheetRequest, current_user: TokenData = Depends(get_current_user)):
    if current_user.role not in ['admin', 'warehouse']:
        raise HTTPException(403, "權限不足")
    conn = await get_db_connection()
    try:
        existing = await conn.fetchrow("SELECT sheet_no FROM stock_take_sheets WHERE sheet_no = $1", req.sheet_no)
        if existing:
            raise HTTPException(409, "盤點單號已存在")
        await conn.execute(
            "INSERT INTO stock_take_sheets (sheet_no, sheet_name, warehouse, sheet_type, created_by) VALUES ($1, $2, $3, $4, $5)",
            req.sheet_no, req.sheet_name, req.warehouse, req.sheet_type, req.created_by
        )
        return {"success": True, "sheet_no": req.sheet_no}
    finally:
        await conn.close()

# ---------- 2. 上傳 Excel ----------
@router.post("/sheets/{sheet_no}/upload-excel")
async def upload_sheet_items(sheet_no: str, file: UploadFile = File(...), current_user: TokenData = Depends(get_current_user)):
    if current_user.role not in ['admin', 'warehouse']:
        raise HTTPException(403, "權限不足")
    conn = await get_db_connection()
    try:
        sheet = await conn.fetchrow("SELECT sheet_no FROM stock_take_sheets WHERE sheet_no = $1", sheet_no)
        if not sheet:
            raise HTTPException(404, "盤點單不存在")
        contents = await file.read()
        df = pd.read_excel(io.BytesIO(contents), engine='openpyxl')
        material_col = None
        for col in df.columns:
            if '料號' in col or '料号' in col:
                material_col = col
                break
        if not material_col:
            raise HTTPException(400, "Excel 缺少料號列")
        added = 0
        for _, row in df.iterrows():
            code = str(row[material_col]).strip()
            if not code:
                continue
            material = await conn.fetchrow("SELECT stock_qty FROM materials WHERE material_code = $1", code)
            if not material:
                continue
            expected = material['stock_qty']
            existing_item = await conn.fetchrow("SELECT id FROM stock_take_items WHERE sheet_no = $1 AND material_code = $2", sheet_no, code)
            if not existing_item:
                await conn.execute("INSERT INTO stock_take_items (sheet_no, material_code, expected_qty) VALUES ($1, $2, $3)", sheet_no, code, expected)
                added += 1
        total = await conn.fetchval("SELECT COUNT(*) FROM stock_take_items WHERE sheet_no = $1", sheet_no)
        await conn.execute("UPDATE stock_take_sheets SET total_items = $1 WHERE sheet_no = $2", total, sheet_no)
        return {"success": True, "added": added, "total": total}
    except Exception as e:
        raise HTTPException(400, f"處理失敗: {str(e)}")
    finally:
        await conn.close()

# ---------- 3. 指派盤點單 ----------
@router.patch("/sheets/{sheet_no}/assign")
async def assign_sheet(sheet_no: str, req: AssignSheetRequest, current_user: TokenData = Depends(get_current_user)):
    if current_user.role not in ['admin', 'warehouse']:
        raise HTTPException(403, "權限不足")
    conn = await get_db_connection()
    try:
        async with conn.transaction():
            sheet = await conn.fetchrow("SELECT status FROM stock_take_sheets WHERE sheet_no = $1 FOR UPDATE", sheet_no)
            if not sheet:
                raise HTTPException(404, "盤點單不存在")
            if sheet['status'] not in ['待指派', '待盤點']:
                raise HTTPException(409, f"當前狀態 {sheet['status']} 不允許指派")
            await conn.execute(
                "UPDATE stock_take_sheets SET assigned_to = $1, status = '待盤點', updated_at = NOW() WHERE sheet_no = $2",
                req.assigned_to, sheet_no
            )
        return {"success": True}
    finally:
        await conn.close()

# ---------- 4. 盤點員任務 ----------
@router.get("/my-tasks")
async def get_my_tasks(current_user: TokenData = Depends(get_current_user)):
    conn = await get_db_connection()
    try:
        rows = await conn.fetch(
            "SELECT * FROM stock_take_sheets WHERE assigned_to = $1 AND status IN ('待盤點', '進行中', '进行中') ORDER BY created_at DESC",
            current_user.username
        )
        return [dict(r) for r in rows]
    finally:
        await conn.close()

# ---------- 5. 開始盤點 ----------
@router.patch("/sheets/{sheet_no}/start")
async def start_sheet(sheet_no: str, current_user: TokenData = Depends(get_current_user)):
    conn = await get_db_connection()
    try:
        async with conn.transaction():
            sheet = await conn.fetchrow("SELECT status, assigned_to FROM stock_take_sheets WHERE sheet_no = $1 FOR UPDATE", sheet_no)
            if not sheet:
                raise HTTPException(404, "盤點單不存在")
            if sheet['assigned_to'] != current_user.username:
                raise HTTPException(403, "您不是該盤點單的指定盤點員")
            if sheet['status'] not in ['待盤點', '进行中']:
                raise HTTPException(409, "盤點單狀態不允許開始")
            await conn.execute("UPDATE stock_take_sheets SET status = '進行中', updated_at = NOW() WHERE sheet_no = $1", sheet_no)
        return {"success": True}
    finally:
        await conn.close()

# ---------- 6. 錄入實盤 ----------
@router.post("/sheets/{sheet_no}/scan")
async def scan_count(sheet_no: str, req: ScanCountRequest, current_user: TokenData = Depends(get_current_user)):
    if req.operator != current_user.username:
        raise HTTPException(403, "操作員不匹配")
    conn = await get_db_connection()
    try:
        async with conn.transaction():
            sheet = await conn.fetchrow("SELECT status, sheet_type, assigned_to FROM stock_take_sheets WHERE sheet_no = $1 FOR UPDATE", sheet_no)
            if not sheet:
                raise HTTPException(404, "盤點單不存在")
            if sheet['assigned_to'] != current_user.username:
                raise HTTPException(403, "您不是該盤點單的指定盤點員")
            if sheet['status'] != '進行中':
                raise HTTPException(409, "盤點單不是進行中狀態")
            item = await conn.fetchrow("SELECT id, expected_qty FROM stock_take_items WHERE sheet_no = $1 AND material_code = $2 FOR UPDATE", sheet_no, req.material_code)
            if not item:
                raise HTTPException(404, "該物料不在當前盤點單中")
            diff = item['expected_qty'] - req.counted_qty
            await conn.execute(
                "UPDATE stock_take_items SET counted_qty = $1, diff_qty = $2, status = '已盤', scanned_by = $3, scanned_at = NOW() WHERE id = $4",
                req.counted_qty, diff, current_user.username, item['id']
            )
            response = {"success": True, "material_code": req.material_code, "counted_qty": req.counted_qty}
            if sheet['sheet_type'] == 'OPEN':
                response["expected_qty"] = item['expected_qty']
                response["diff"] = diff
            else:
                response["message"] = "已錄入（暗盤）"
            return response
    finally:
        await conn.close()

# ---------- 7. 獲取盤點明細 ----------
@router.get("/sheets/{sheet_no}/items")
async def get_sheet_items(sheet_no: str, current_user: TokenData = Depends(get_current_user)):
    conn = await get_db_connection()
    try:
        sheet = await conn.fetchrow("SELECT sheet_type, assigned_to FROM stock_take_sheets WHERE sheet_no = $1", sheet_no)
        if not sheet:
            raise HTTPException(404, "盤點單不存在")
        if sheet['assigned_to'] != current_user.username:
            raise HTTPException(403, "您不是該盤點單的指定盤點員")
        rows = await conn.fetch(
            "SELECT i.material_code, m.name, m.spec, i.expected_qty, i.counted_qty, i.diff_qty, i.status, i.scanned_by, i.scanned_at "
            "FROM stock_take_items i LEFT JOIN materials m ON i.material_code = m.material_code WHERE i.sheet_no = $1 ORDER BY i.material_code",
            sheet_no
        )
        items = []
        for r in rows:
            item = dict(r)
            if sheet['sheet_type'] == 'BLIND' and item['counted_qty'] == 0:
                item['expected_qty'] = None
            items.append(item)
        return items
    finally:
        await conn.close()

# ---------- 8. 結束盤點 ----------
@router.patch("/sheets/{sheet_no}/finish")
async def finish_sheet(sheet_no: str, current_user: TokenData = Depends(get_current_user)):
    conn = await get_db_connection()
    try:
        async with conn.transaction():
            sheet = await conn.fetchrow("SELECT status, assigned_to FROM stock_take_sheets WHERE sheet_no = $1 FOR UPDATE", sheet_no)
            if not sheet:
                raise HTTPException(404, "盤點單不存在")
            if sheet['assigned_to'] != current_user.username:
                raise HTTPException(403, "您不是該盤點單的指定盤點員")
            if sheet['status'] != '進行中':
                raise HTTPException(409, "盤點單不是進行中狀態")
            await conn.execute("UPDATE stock_take_sheets SET status = '已結束', updated_at = NOW() WHERE sheet_no = $1", sheet_no)
        return {"success": True}
    finally:
        await conn.close()

# ---------- 9. 列表 ----------
@router.get("/sheets")
async def list_sheets(current_user: TokenData = Depends(get_current_user)):
    if current_user.role not in ['admin', 'warehouse']:
        raise HTTPException(403, "權限不足")
    conn = await get_db_connection()
    try:
        rows = await conn.fetch(
            """
            SELECT s.*, u.full_name AS assigned_to_name
            FROM stock_take_sheets s
            LEFT JOIN system_users u ON s.assigned_to = u.username
            ORDER BY s.created_at DESC
            """
        )
        return [dict(r) for r in rows]
    finally:
        await conn.close()

# ---------- 10. 導出 ----------
@router.get("/sheets/{sheet_no}/export")
async def export_sheet(sheet_no: str, current_user: TokenData = Depends(get_current_user)):
    if current_user.role not in ['admin', 'warehouse']:
        raise HTTPException(403, "權限不足")
    conn = await get_db_connection()
    try:
        sheet = await conn.fetchrow("SELECT * FROM stock_take_sheets WHERE sheet_no = $1", sheet_no)
        if not sheet:
            raise HTTPException(404, "盤點單不存在")
        items = await conn.fetch(
            "SELECT i.material_code, m.name, m.spec, i.expected_qty, i.counted_qty, i.diff_qty, i.status, i.scanned_by, i.scanned_at "
            "FROM stock_take_items i LEFT JOIN materials m ON i.material_code = m.material_code WHERE i.sheet_no = $1",
            sheet_no
        )
        output = io.BytesIO()
        workbook = xlsxwriter.Workbook(output, {'in_memory': True})
        worksheet = workbook.add_worksheet("盤點明細")
        headers = ["料號", "品名", "規格", "賬面庫存", "實盤數量", "差異", "狀態", "盤點員", "盤點時間"]
        for col, h in enumerate(headers):
            worksheet.write(0, col, h)
        for row_idx, it in enumerate(items, start=1):
            worksheet.write(row_idx, 0, it['material_code'])
            worksheet.write(row_idx, 1, it['name'] or '')
            worksheet.write(row_idx, 2, it['spec'] or '')
            worksheet.write(row_idx, 3, it['expected_qty'])
            worksheet.write(row_idx, 4, it['counted_qty'])
            worksheet.write(row_idx, 5, it['diff_qty'])
            worksheet.write(row_idx, 6, it['status'])
            worksheet.write(row_idx, 7, it['scanned_by'] or '')
            worksheet.write(row_idx, 8, it['scanned_at'].strftime('%Y-%m-%d %H:%M:%S') if it['scanned_at'] else '')
        workbook.close()
        output.seek(0)
        return StreamingResponse(
            output,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename=盤點_{sheet_no}.xlsx"}
        )
    finally:
        await conn.close()

@router.delete("/sheets/{sheet_no}")
async def delete_sheet(sheet_no: str, current_user: TokenData = Depends(get_current_user)):
    if current_user.role not in ['admin', 'warehouse']:
        raise HTTPException(403, "權限不足")
    conn = await get_db_connection()
    try:
        sheet = await conn.fetchrow("SELECT sheet_no FROM stock_take_sheets WHERE sheet_no = $1", sheet_no)
        if not sheet:
            raise HTTPException(404, "盤點單不存在")
        await conn.execute("DELETE FROM stock_take_items WHERE sheet_no = $1", sheet_no)
        await conn.execute("DELETE FROM stock_take_sheets WHERE sheet_no = $1", sheet_no)
        return {"success": True, "message": "刪除成功"}
    finally:
        await conn.close()

# ========== 智慧庫存查詢 API ==========
@router.get("/warehouses")
async def get_warehouses(current_user: TokenData = Depends(get_current_user)):
    warehouses = ["待驗倉", "不良品倉", "待入庫倉", "包裝倉", "包裝成品倉", "待出貨倉"]
    return warehouses

# 修改點：包裝倉直接從 warehouse_stock 查詢
@router.get("/stock-by-warehouse")
async def stock_by_warehouse(
    warehouse: str = Query(...),
    search: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
    current_user: TokenData = Depends(get_current_user)
):
    conn = await get_db_connection()
    try:
        if warehouse == "待驗倉":
            query = """
                SELECT d.doc_number, dd.material_code, m.name, m.spec, dd.accepted_qty AS qty
                FROM documents d
                JOIN document_details dd ON d.doc_number = dd.doc_number
                LEFT JOIN materials m ON dd.material_code = m.material_code
                WHERE d.status = '待驗' AND dd.accepted_qty > 0
            """
            if search:
                query += " AND (dd.material_code ILIKE '%' || $1 || '%' OR m.name ILIKE '%' || $1 || '%')"
                rows = await conn.fetch(query + " ORDER BY d.doc_number LIMIT $2 OFFSET $3", search, limit, offset)
                total = await conn.fetchval("SELECT COUNT(*) FROM (" + query + ") sub", search)
            else:
                rows = await conn.fetch(query + " ORDER BY d.doc_number LIMIT $1 OFFSET $2", limit, offset)
                total = await conn.fetchval("SELECT COUNT(*) FROM (" + query + ") sub")
        elif warehouse == "待入庫倉":
            query = """
                SELECT d.doc_number, dd.material_code, m.name, m.spec, dd.accepted_qty AS qty
                FROM documents d
                JOIN document_details dd ON d.doc_number = dd.doc_number
                LEFT JOIN materials m ON dd.material_code = m.material_code
                WHERE d.status = '檢驗完成' AND dd.accepted_qty > 0
            """
            if search:
                rows = await conn.fetch(query + " AND (dd.material_code ILIKE '%' || $1 || '%' OR m.name ILIKE '%' || $1 || '%') ORDER BY d.doc_number LIMIT $2 OFFSET $3", search, limit, offset)
                total = await conn.fetchval("SELECT COUNT(*) FROM (" + query + ") sub", search)
            else:
                rows = await conn.fetch(query + " ORDER BY d.doc_number LIMIT $1 OFFSET $2", limit, offset)
                total = await conn.fetchval("SELECT COUNT(*) FROM (" + query + ") sub")
        elif warehouse == "不良品倉":
            query = """
                SELECT d.doc_number, dd.material_code, m.name, m.spec, dd.reject_qty AS qty
                FROM documents d
                JOIN document_details dd ON d.doc_number = dd.doc_number
                LEFT JOIN materials m ON dd.material_code = m.material_code
                WHERE dd.reject_qty > 0
            """
            if search:
                rows = await conn.fetch(query + " AND (dd.material_code ILIKE '%' || $1 || '%' OR m.name ILIKE '%' || $1 || '%') ORDER BY d.doc_number LIMIT $2 OFFSET $3", search, limit, offset)
                total = await conn.fetchval("SELECT COUNT(*) FROM (" + query + ") sub", search)
            else:
                rows = await conn.fetch(query + " ORDER BY d.doc_number LIMIT $1 OFFSET $2", limit, offset)
                total = await conn.fetchval("SELECT COUNT(*) FROM (" + query + ") sub")
        elif warehouse == "包裝倉":
            # 直接從 warehouse_stock 查詢，並關聯物料名稱規格
            base_query = """
                SELECT ws.doc_number, ws.material_code, 
                       COALESCE(m.name, '') AS name, 
                       COALESCE(m.spec, '') AS spec, 
                       ws.qty
                FROM warehouse_stock ws
                LEFT JOIN materials m ON ws.material_code = m.material_code
                WHERE ws.warehouse = '包裝倉'
            """
            # 計算總筆數（獨立查詢，避免子查詢語法問題）
            count_query = "SELECT COUNT(*) FROM warehouse_stock WHERE warehouse = '包裝倉'"
            if search:
                count_query += " AND (material_code ILIKE '%' || $1 || '%')"
                total = await conn.fetchval(count_query, f"%{search}%")
                base_query += " AND (ws.material_code ILIKE '%' || $1 || '%')"
                rows = await conn.fetch(
                    base_query + " ORDER BY ws.doc_number LIMIT $2 OFFSET $3",
                    f"%{search}%", limit, offset
                )
            else:
                total = await conn.fetchval(count_query)
                rows = await conn.fetch(
                    base_query + " ORDER BY ws.doc_number LIMIT $1 OFFSET $2",
                    limit, offset
                )
        elif warehouse == "包裝成品倉":
            query = """
                SELECT ws.doc_number, ws.material_code, COALESCE(m.name, '') AS name, 
                       COALESCE(m.spec, '') AS spec, ws.qty
                FROM warehouse_stock ws
                LEFT JOIN materials m ON ws.material_code = m.material_code
                WHERE ws.warehouse = '包裝成品倉'
            """
            if search:
                query += " AND (ws.material_code ILIKE '%' || $1 || '%' OR m.name ILIKE '%' || $1 || '%')"
                rows = await conn.fetch(query + " ORDER BY ws.doc_number LIMIT $2 OFFSET $3", f"%{search}%", limit, offset)
                total = await conn.fetchval("SELECT COUNT(*) FROM warehouse_stock WHERE warehouse = '包裝成品倉' AND material_code ILIKE '%' || $1 || '%'", f"%{search}%")
            else:
                rows = await conn.fetch(query + " ORDER BY ws.doc_number LIMIT $1 OFFSET $2", limit, offset)
                total = await conn.fetchval("SELECT COUNT(*) FROM warehouse_stock WHERE warehouse = '包裝成品倉'")
        elif warehouse == "待出貨倉":
            query = """
                SELECT ws.doc_number, ws.material_code, COALESCE(m.name, '') AS name, 
                       COALESCE(m.spec, '') AS spec, ws.qty
                FROM warehouse_stock ws
                LEFT JOIN materials m ON ws.material_code = m.material_code
                WHERE ws.warehouse = '待出貨倉'
            """
            if search:
                query += " AND (ws.material_code ILIKE '%' || $1 || '%' OR m.name ILIKE '%' || $1 || '%')"
                rows = await conn.fetch(query + " ORDER BY ws.doc_number LIMIT $2 OFFSET $3", f"%{search}%", limit, offset)
                total = await conn.fetchval("SELECT COUNT(*) FROM warehouse_stock WHERE warehouse = '待出貨倉' AND material_code ILIKE '%' || $1 || '%'", f"%{search}%")
            else:
                rows = await conn.fetch(query + " ORDER BY ws.doc_number LIMIT $1 OFFSET $2", limit, offset)
                total = await conn.fetchval("SELECT COUNT(*) FROM warehouse_stock WHERE warehouse = '待出貨倉'")
        else:
            # 其他倉庫仍用箱子邏輯
            query = """
                SELECT b.parent_doc_number AS doc_number, bd.material_code, m.name, m.spec, SUM(bd.quantity) AS qty
                FROM boxes b
                JOIN box_details bd ON b.box_number = bd.box_number
                LEFT JOIN materials m ON bd.material_code = m.material_code
                WHERE b.current_site = $1
                GROUP BY b.parent_doc_number, bd.material_code, m.name, m.spec
            """
            if search:
                rows = await conn.fetch(query + " HAVING (bd.material_code ILIKE '%' || $2 || '%' OR m.name ILIKE '%' || $2 || '%') ORDER BY doc_number LIMIT $3 OFFSET $4", warehouse, search, limit, offset)
                total = await conn.fetchval("SELECT COUNT(*) FROM (" + query + ") sub", warehouse)
            else:
                rows = await conn.fetch(query + " ORDER BY doc_number LIMIT $2 OFFSET $3", warehouse, limit, offset)
                total = await conn.fetchval("SELECT COUNT(*) FROM (" + query + ") sub", warehouse)
        items = [dict(r) for r in rows]
        return {"data": items, "total": total}
    finally:
        await conn.close()

# ========== 依單據查詢（修改包裝倉來源）==========
@router.get("/by-document/{doc_number}")
async def query_by_document(doc_number: str, current_user: TokenData = Depends(get_current_user)):
    conn = await get_db_connection()
    try:
        doc = await conn.fetchrow("SELECT * FROM documents WHERE doc_number = $1", doc_number)
        if not doc:
            raise HTTPException(404, "單據不存在")
        details = await conn.fetch(
            """
            SELECT dd.material_code, 
                   COALESCE(dd.material_name, m.name) AS material_name,
                   COALESCE(dd.spec, m.spec) AS spec
            FROM document_details dd
            LEFT JOIN materials m ON dd.material_code = m.material_code
            WHERE dd.doc_number = $1
            """,
            doc_number
        )
        if not details:
            return {"doc_number": doc_number, "doc_type": doc['doc_type'], "status": doc['status'], "items": []}
        
        items_result = []
        for d in details:
            material_code = d['material_code']
            material_name = d['material_name']
            spec = d['spec']
            distribution = []
            
            if doc['doc_type'] == 'purchase':
                row = await conn.fetchrow(
                    "SELECT accepted_qty, reject_qty FROM document_details WHERE doc_number = $1 AND material_code = $2",
                    doc_number, material_code
                )
                if row:
                    if doc['status'] in ['待收貨', '待驗']:
                        distribution.append({"warehouse": "待驗倉", "qty": row['accepted_qty'] or 0})
                    elif doc['status'] == '檢驗完成':
                        distribution.append({"warehouse": "待入庫倉", "qty": row['accepted_qty'] or 0})
                    if row['reject_qty'] and row['reject_qty'] > 0:
                        distribution.append({"warehouse": "不良品倉", "qty": row['reject_qty']})
            else:  # manufacture
                pack_stock = await conn.fetchrow(
                    "SELECT qty FROM warehouse_stock WHERE doc_number = $1 AND material_code = $2 AND warehouse = '包裝倉'",
                    doc_number, material_code
                )
                if pack_stock and pack_stock['qty'] > 0:
                    distribution.append({"warehouse": "包裝倉", "qty": pack_stock['qty']})
                
                box_rows = await conn.fetch(
                    """
                    SELECT b.current_site, SUM(bd.quantity) as qty
                    FROM boxes b
                    JOIN box_details bd ON b.box_number = bd.box_number
                    WHERE b.parent_doc_number = $1 AND bd.material_code = $2
                      AND b.current_site IN ('包裝成品倉', '待出貨倉')
                    GROUP BY b.current_site
                    """,
                    doc_number, material_code
                )
                for br in box_rows:
                    site = br['current_site']
                    qty = br['qty'] or 0
                    if qty > 0:
                        display = '包裝成品' if site == '包裝成品倉' else '待出貨'
                        distribution.append({"warehouse": display, "qty": qty})
            
            items_result.append({
                "material_code": material_code,
                "material_name": material_name,
                "spec": spec,
                "distribution": distribution
            })
        return {
            "doc_number": doc_number,
            "doc_type": doc['doc_type'],
            "status": doc['status'],
            "items": items_result
        }
    finally:
        await conn.close()
# ========== 依料號查詢（修改包裝倉數量來源）==========
@router.get("/by-material/{material_code}")
async def query_by_material(material_code: str, current_user: TokenData = Depends(get_current_user)):
    conn = await get_db_connection()
    try:
        material = await conn.fetchrow("SELECT name, spec FROM materials WHERE material_code = $1", material_code)
        if not material:
            raise HTTPException(404, "物料不存在")
        main_stock = await conn.fetchval("SELECT stock_qty FROM materials WHERE material_code = $1", material_code)
        pending_inspect = await conn.fetchval(
            "SELECT COALESCE(SUM(dd.accepted_qty), 0) FROM documents d JOIN document_details dd ON d.doc_number = dd.doc_number WHERE d.status = '待驗' AND dd.material_code = $1",
            material_code
        )
        pending_inbound = await conn.fetchval(
            "SELECT COALESCE(SUM(dd.accepted_qty), 0) FROM documents d JOIN document_details dd ON d.doc_number = dd.doc_number WHERE d.status = '檢驗完成' AND dd.material_code = $1",
            material_code
        )
        reject = await conn.fetchval(
            "SELECT COALESCE(SUM(dd.reject_qty), 0) FROM document_details dd WHERE dd.material_code = $1",
            material_code
        )
        # 包裝倉數量從 warehouse_stock 彙總
        pack = await conn.fetchval(
            "SELECT COALESCE(SUM(qty), 0) FROM warehouse_stock WHERE material_code = $1 AND warehouse = '包裝倉'",
            material_code
        )
        # 包裝成品倉數量從 warehouse_stock 彙總
        pack_finished = await conn.fetchval(
            "SELECT COALESCE(SUM(qty), 0) FROM warehouse_stock WHERE material_code = $1 AND warehouse = '包裝成品倉'",
            material_code
        )
        # 待出貨倉數量從 warehouse_stock 彙總
        ready_ship = await conn.fetchval(
            "SELECT COALESCE(SUM(qty), 0) FROM warehouse_stock WHERE material_code = $1 AND warehouse = '待出貨倉'",
            material_code
        )
        distribution = [
            {"warehouse": "主儲倉", "qty": main_stock or 0},
            {"warehouse": "待驗倉", "qty": pending_inspect},
            {"warehouse": "待入庫倉", "qty": pending_inbound},
            {"warehouse": "不良品倉", "qty": reject},
            {"warehouse": "包裝倉", "qty": pack},
            {"warehouse": "包裝成品倉", "qty": pack_finished},
            {"warehouse": "待出貨倉", "qty": ready_ship}
        ]
        return {
            "material_code": material_code,
            "name": material['name'],
            "spec": material['spec'],
            "distribution": distribution
        }
    finally:
        await conn.close()

# ========== pending-documents ==========
@router.get("/pending-documents")
async def get_pending_documents(current_user: TokenData = Depends(get_current_user)):
    conn = await get_db_connection()
    try:
        rows = await conn.fetch(
            """
            SELECT DISTINCT d.doc_number, d.doc_type, d.status
            FROM documents d
            INNER JOIN document_details dd ON d.doc_number = dd.doc_number
            WHERE (d.doc_type = 'purchase' AND d.status != '已入庫')
               OR (d.doc_type = 'manufacture' AND d.status != '已出庫')
            ORDER BY d.doc_number
            """
        )
        return [dict(r) for r in rows]
    finally:
        await conn.close()

# ========== 歷史紀錄查詢（保持原樣） ==========
@router.get("/history/inbound")
async def get_inbound_history(
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    limit: int = 100,
    offset: int = 0,
    current_user: TokenData = Depends(get_current_user)
):
    conn = await get_db_connection()
    try:
        if not end_date:
            end_date = datetime.now()
        if not start_date:
            start_date = end_date - timedelta(days=180)
        query = """
            SELECT sl.id, sl.material_code, m.name, m.spec, sl.change_qty AS qty, 
                   sl.reference_doc AS doc_number, sl.operator, sl.created_at
            FROM stock_ledger sl
            LEFT JOIN materials m ON sl.material_code = m.material_code
            WHERE sl.change_type = 'INBOUND' 
              AND sl.warehouse = '主儲倉'
              AND sl.created_at BETWEEN $1 AND $2
            ORDER BY sl.created_at DESC
            LIMIT $3 OFFSET $4
        """
        rows = await conn.fetch(query, start_date, end_date, limit, offset)
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM stock_ledger WHERE change_type = 'INBOUND' AND warehouse = '主儲倉' AND created_at BETWEEN $1 AND $2",
            start_date, end_date
        )
        return {"data": [dict(r) for r in rows], "total": total}
    finally:
        await conn.close()

@router.get("/history/outbound")
async def get_outbound_history(
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    limit: int = 100,
    offset: int = 0,
    current_user: TokenData = Depends(get_current_user)
):
    conn = await get_db_connection()
    try:
        if not end_date:
            end_date = datetime.now()
        if not start_date:
            start_date = end_date - timedelta(days=180)
        query = """
            SELECT sl.id, sl.material_code, m.name, m.spec, sl.change_qty AS qty, 
                   sl.reference_doc AS doc_number, sl.operator, sl.created_at
            FROM stock_ledger sl
            LEFT JOIN materials m ON sl.material_code = m.material_code
            WHERE sl.change_type = 'OUTBOUND' 
              AND sl.warehouse = '待出貨倉'
              AND sl.created_at BETWEEN $1 AND $2
            ORDER BY sl.created_at DESC
            LIMIT $3 OFFSET $4
        """
        rows = await conn.fetch(query, start_date, end_date, limit, offset)
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM stock_ledger WHERE change_type = 'OUTBOUND' AND warehouse = '待出貨倉' AND created_at BETWEEN $1 AND $2",
            start_date, end_date
        )
        return {"data": [dict(r) for r in rows], "total": total}
    finally:
        await conn.close()

@router.get("/history/return")
async def get_return_history(
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    limit: int = 100,
    offset: int = 0,
    current_user: TokenData = Depends(get_current_user)
):
    conn = await get_db_connection()
    try:
        if not end_date:
            end_date = datetime.now()
        if not start_date:
            start_date = end_date - timedelta(days=180)
        query = """
            SELECT sl.id, sl.material_code, m.name, m.spec, sl.change_qty AS qty, 
                   sl.reference_doc AS doc_number, sl.operator, sl.created_at
            FROM stock_ledger sl
            LEFT JOIN materials m ON sl.material_code = m.material_code
            WHERE sl.change_type = 'RETURN' 
              AND sl.created_at BETWEEN $1 AND $2
            ORDER BY sl.created_at DESC
            LIMIT $3 OFFSET $4
        """
        rows = await conn.fetch(query, start_date, end_date, limit, offset)
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM stock_ledger WHERE change_type = 'RETURN' AND created_at BETWEEN $1 AND $2",
            start_date, end_date
        )
        return {"data": [dict(r) for r in rows], "total": total}
    finally:
        await conn.close()

# ========== 入庫任務管理 API（保持原樣） ==========
class AssignInboundTaskRequest(BaseModel):
    task_id: int
    assigned_to: str

class BatchAssignInboundRequest(BaseModel):
    task_ids: List[int]
    assigned_to: str

class CompleteInboundRequest(BaseModel):
    task_id: int
    completed_qty: int

class CreateInboundTasksRequest(BaseModel):
    doc_number: str
    items: List[ReceiveItem]

@router.post("/inbound-tasks/create-from-doc")
async def create_inbound_tasks_from_doc(req: CreateInboundTasksRequest, current_user: TokenData = Depends(get_current_user)):
    if current_user.role not in ['admin', 'warehouse']:
        raise HTTPException(403, "權限不足")
    conn = await get_db_connection()
    async with conn.transaction():
        doc = await conn.fetchrow("SELECT status FROM documents WHERE doc_number = $1 FOR UPDATE", req.doc_number)
        if not doc or doc['status'] != '檢驗完成':
            raise HTTPException(409, "單據狀態不是檢驗完成，無法建立入庫任務")
        task_ids = []
        for item in req.items:
            existing = await conn.fetchrow(
                "SELECT id FROM inbound_tasks WHERE doc_number = $1 AND material_code = $2 AND status != 'completed'",
                req.doc_number, item.material_code
            )
            if existing:
                task_ids.append(existing['id'])
            else:
                row = await conn.fetchrow(
                    """
                    INSERT INTO inbound_tasks (doc_number, material_code, expected_qty, completed_qty, status)
                    VALUES ($1, $2, $3, 0, 'pending')
                    RETURNING id
                    """,
                    req.doc_number, item.material_code, int(item.received_qty)
                )
                task_ids.append(row['id'])
        return {"success": True, "task_ids": task_ids}

@router.get("/inbound-tasks/pending")
async def get_pending_inbound_tasks(current_user: TokenData = Depends(get_current_user)):
    if current_user.role not in ['admin', 'warehouse']:
        raise HTTPException(403, "權限不足")
    conn = await get_db_connection()
    try:
        rows = await conn.fetch(
            """
            SELECT t.id, t.doc_number, t.material_code, t.expected_qty, t.completed_qty, t.status, t.assigned_to,
                   u.full_name AS assigned_to_name,
                   COALESCE(dd.material_name, m.name, '') AS material_name_alt,
                   dd.spec
            FROM inbound_tasks t
            LEFT JOIN documents d ON t.doc_number = d.doc_number
            LEFT JOIN document_details dd ON t.doc_number = dd.doc_number AND t.material_code = dd.material_code
            LEFT JOIN materials m ON t.material_code = m.material_code
            LEFT JOIN system_users u ON t.assigned_to = u.username
            WHERE t.status IN ('pending', 'in_progress')
            ORDER BY t.created_at
            """
        )
        return [dict(r) for r in rows]
    finally:
        await conn.close()

@router.patch("/inbound-tasks/assign")
async def assign_inbound_task(req: AssignInboundTaskRequest, current_user: TokenData = Depends(get_current_user)):
    if current_user.role not in ['admin', 'warehouse']:
        raise HTTPException(403, "權限不足")
    conn = await get_db_connection()
    async with conn.transaction():
        task = await conn.fetchrow("SELECT status FROM inbound_tasks WHERE id = $1 FOR UPDATE", req.task_id)
        if not task:
            raise HTTPException(404, "任務不存在")
        if task['status'] == 'completed':
            raise HTTPException(409, "已完成任務不可指派")
        await conn.execute(
            "UPDATE inbound_tasks SET assigned_to = $1, assigned_by = $2, status = 'in_progress', updated_at = NOW() WHERE id = $3",
            req.assigned_to, current_user.username, req.task_id
        )
    return {"success": True}

@router.post("/inbound-tasks/batch-assign")
async def batch_assign_inbound(req: BatchAssignInboundRequest, current_user: TokenData = Depends(get_current_user)):
    if current_user.role not in ['admin', 'warehouse']:
        raise HTTPException(403, "權限不足")
    conn = await get_db_connection()
    async with conn.transaction():
        for tid in req.task_ids:
            await conn.execute(
                "UPDATE inbound_tasks SET assigned_to = $1, assigned_by = $2, status = 'in_progress', updated_at = NOW() WHERE id = $3 AND status = 'pending'",
                req.assigned_to, current_user.username, tid
            )
    return {"success": True}

@router.get("/my-inbound-tasks")
async def get_my_inbound_tasks(current_user: TokenData = Depends(get_current_user)):
    conn = await get_db_connection()
    try:
        rows = await conn.fetch(
            """
            SELECT t.id, t.doc_number, t.material_code, t.expected_qty, t.completed_qty, t.status,
                   COALESCE(dd.material_name, m.name, '') as material_name_alt,
                   dd.spec
            FROM inbound_tasks t
            LEFT JOIN documents d ON t.doc_number = d.doc_number
            LEFT JOIN document_details dd ON t.doc_number = dd.doc_number AND t.material_code = dd.material_code
            LEFT JOIN materials m ON t.material_code = m.material_code
            WHERE t.assigned_to = $1 AND t.status != 'completed'
            ORDER BY t.created_at
            """,
            current_user.username
        )
        return [dict(r) for r in rows]
    finally:
        await conn.close()

@router.post("/inbound-tasks/complete")
async def complete_inbound_task(req: CompleteInboundRequest, current_user: TokenData = Depends(get_current_user)):
    conn = await get_db_connection()
    async with conn.transaction():
        task = await conn.fetchrow(
            "SELECT id, expected_qty, completed_qty, assigned_to, doc_number, material_code, status FROM inbound_tasks WHERE id = $1 FOR UPDATE",
            req.task_id
        )
        if not task:
            raise HTTPException(404, "任務不存在")
        if task['assigned_to'] != current_user.username:
            raise HTTPException(403, "您不是該任務的指定執行人")
        if task['status'] != 'in_progress':
            raise HTTPException(409, "任務狀態不允許入庫")
        new_completed = task['completed_qty'] + req.completed_qty
        if new_completed > task['expected_qty']:
            raise HTTPException(400, "入庫數量超過待入庫數量")
        new_status = 'completed' if new_completed == task['expected_qty'] else 'in_progress'
        await conn.execute(
            "UPDATE inbound_tasks SET completed_qty = $1, status = $2, updated_at = NOW() WHERE id = $3",
            new_completed, new_status, task['id']
        )
        material = await conn.fetchrow("SELECT stock_qty, version FROM materials WHERE material_code = $1 FOR UPDATE", task['material_code'])
        new_stock = material['stock_qty'] + req.completed_qty
        await conn.execute(
            "UPDATE materials SET stock_qty = $1, version = version + 1, updated_at = NOW() WHERE material_code = $2 AND version = $3",
            new_stock, task['material_code'], material['version']
        )
        await conn.execute(
            """
            INSERT INTO stock_ledger (material_code, warehouse, change_type, change_qty, before_qty, after_qty, reference_doc, operator, remark)
            VALUES ($1, '主儲倉', 'INBOUND', $2, $3, $4, $5, $6, '入庫任務執行')
            """,
            task['material_code'], req.completed_qty, material['stock_qty'], new_stock, task['doc_number'], current_user.username
        )
        all_completed = await conn.fetchval(
            "SELECT COUNT(*) FROM inbound_tasks WHERE doc_number = $1 AND status != 'completed'",
            task['doc_number']
        )
        if all_completed == 0:
            await conn.execute(
                "UPDATE documents SET status = '已入庫', updated_at = NOW() WHERE doc_number = $1",
                task['doc_number']
            )
    return {"success": True, "completed": req.completed_qty, "new_completed": new_completed, "status": new_status}