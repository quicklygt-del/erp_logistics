from typing import Optional, List
from fastapi import APIRouter, HTTPException, Query, UploadFile, File, Depends
from pydantic import BaseModel
import pandas as pd
import io
from database import get_db_connection
from routers.auth import get_current_user, TokenData

router = APIRouter()

# ---------- Pydantic 模型 ----------
class CreateDocumentRequest(BaseModel):
    doc_number: str
    doc_type: str
    operator: str

class ReceiveItem(BaseModel):
    material_code: str
    received_qty: int

class ReceiveBatchRequest(BaseModel):
    operator: str
    items: list[ReceiveItem]

class ShipRequest(BaseModel):
    operator: str
    items: Optional[list[ReceiveItem]] = None

class InboundRequest(BaseModel):
    operator: str
    items: list[ReceiveItem]

class StartBoxRequest(BaseModel):
    operator: str
    box_number: str
    remark: Optional[str] = None

class AddToBoxRequest(BaseModel):
    operator: str
    material_code: str
    quantity: int

class MoveBoxesRequest(BaseModel):
    box_numbers: list[str]
    operator: str

class PickItemRequest(BaseModel):
    material_code: str
    picked_qty: int
    operator: str

class ShipBoxesRequest(BaseModel):
    box_numbers: list[str]
    operator: str

# ========== 單據相關介面 ==========
@router.post("/")
async def create_document(req: CreateDocumentRequest, current_user: TokenData = Depends(get_current_user)):
    if current_user.role not in ['admin', 'warehouse', 'receiver']:
        raise HTTPException(403, "權限不足")
    conn = await get_db_connection()
    try:
        existing = await conn.fetchrow("SELECT doc_number FROM documents WHERE doc_number = $1", req.doc_number)
        if existing:
            raise HTTPException(409, "單據編號已存在")
        if req.doc_type == 'manufacture':
            status, site = '待發料', '主儲倉'
            picking_status = '待指派'
        else:
            status, site = '待收貨', '待驗倉'
            picking_status = None
        await conn.execute(
            "INSERT INTO documents (doc_number, doc_type, status, current_site, picking_status) VALUES ($1, $2, $3, $4, $5)",
            req.doc_number, req.doc_type, status, site, picking_status
        )
        return {"success": True}
    finally:
        await conn.close()

@router.get("/")
async def list_documents(current_user: TokenData = Depends(get_current_user)):
    if current_user.role not in ['admin', 'warehouse']:
        raise HTTPException(403, "權限不足")
    conn = await get_db_connection()
    try:
        rows = await conn.fetch(
            """
            SELECT d.*, u.full_name AS assigned_picker_name
            FROM documents d
            LEFT JOIN system_users u ON d.assigned_picker = u.username
            ORDER BY d.created_at DESC
            """
        )
        return [dict(r) for r in rows]
    finally:
        await conn.close()

@router.get("/ready-to-pack")
async def get_ready_to_pack(current_user: TokenData = Depends(get_current_user)):
    conn = await get_db_connection()
    try:
        rows = await conn.fetch(
            """
            SELECT DISTINCT d.doc_number, d.doc_type, d.status
            FROM documents d
            JOIN warehouse_stock ws ON d.doc_number = ws.doc_number
            WHERE d.doc_type = 'manufacture'
              AND d.picking_status = '已完成'
              AND ws.warehouse = '包裝倉'
              AND ws.qty > 0
            """
        )
        return [dict(r) for r in rows]
    finally:
        await conn.close()

@router.get("/ready-to-ship")
async def get_ready_to_ship(current_user: TokenData = Depends(get_current_user)):
    conn = await get_db_connection()
    try:
        rows = await conn.fetch(
            """
            SELECT DISTINCT d.doc_number, d.doc_type, d.status
            FROM documents d
            JOIN boxes b ON d.doc_number = b.parent_doc_number
            WHERE b.status = '待出貨倉'
            """
        )
        return [dict(r) for r in rows]
    finally:
        await conn.close()

@router.post("/{doc_number}/receive-batch")
async def receive_batch(doc_number: str, req: ReceiveBatchRequest, current_user: TokenData = Depends(get_current_user)):
    if current_user.role not in ['receiver', 'admin']:
        raise HTTPException(403, "權限不足")
    conn = await get_db_connection()
    async with conn.transaction():
        doc = await conn.fetchrow("SELECT status FROM documents WHERE doc_number = $1 FOR UPDATE", doc_number)
        if not doc:
            await conn.execute(
                "INSERT INTO documents (doc_number, doc_type, status, current_site) VALUES ($1, 'purchase', '待收貨', '待驗倉')",
                doc_number
            )
            doc = await conn.fetchrow("SELECT status FROM documents WHERE doc_number = $1 FOR UPDATE", doc_number)
        
        if doc['status'] not in ['待收貨', '待驗']:
            raise HTTPException(409, "單據狀態不允許收貨")
        
        for item in req.items:
            detail = await conn.fetchrow(
                "SELECT id, accepted_qty, required_qty FROM document_details WHERE doc_number = $1 AND material_code = $2 FOR UPDATE",
                doc_number, item.material_code
            )
            if detail:
                new_accepted = detail['accepted_qty'] + item.received_qty
                await conn.execute(
                    "UPDATE document_details SET accepted_qty = $1, version = version + 1, updated_at = NOW() WHERE id = $2",
                    new_accepted, detail['id']
                )
            else:
                material = await conn.fetchrow("SELECT name, spec FROM materials WHERE material_code = $1", item.material_code)
                material_name = material['name'] if material else ''
                spec = material['spec'] if material else ''
                await conn.execute(
                    "INSERT INTO document_details (doc_number, material_code, material_name, spec, required_qty, accepted_qty) VALUES ($1, $2, $3, $4, 0, $5)",
                    doc_number, item.material_code, material_name, spec, item.received_qty
                )
        if doc['status'] == '待收貨':
            await conn.execute("UPDATE documents SET status = '待驗', updated_at = NOW() WHERE doc_number = $1", doc_number)
    return {"success": True}

@router.post("/{doc_number}/ship")
async def ship_document(doc_number: str, req: ShipRequest, current_user: TokenData = Depends(get_current_user)):
    if current_user.role not in ['receiver', 'admin']:
        raise HTTPException(403, "權限不足")
    conn = await get_db_connection()
    async with conn.transaction():
        doc = await conn.fetchrow("SELECT status, current_site FROM documents WHERE doc_number = $1 FOR UPDATE", doc_number)
        if not doc or doc['current_site'] != '待出貨倉':
            raise HTTPException(409, "單據不在待出貨狀態")
        boxes = await conn.fetch("SELECT box_number FROM boxes WHERE parent_doc_number = $1 AND status = '待出貨倉'", doc_number)
        if not boxes:
            raise HTTPException(400, "沒有待出貨的箱子")
        for box in boxes:
            await conn.execute(
                "UPDATE boxes SET status = '已出庫', current_site = '出庫完成', updated_at = NOW() WHERE box_number = $1",
                box['box_number']
            )
        await conn.execute("UPDATE documents SET status = '已出庫', current_site = '出庫完成', updated_at = NOW() WHERE doc_number = $1", doc_number)
    return {"success": True}

@router.get("/{doc_number}/details")
async def get_document_details(doc_number: str, current_user: TokenData = Depends(get_current_user)):
    conn = await get_db_connection()
    try:
        doc = await conn.fetchrow("SELECT * FROM documents WHERE doc_number = $1", doc_number)
        if not doc:
            raise HTTPException(404, "單據不存在")
        details = await conn.fetch(
            "SELECT material_code, material_name, spec, required_qty, issued_qty, accepted_qty, reject_qty FROM document_details WHERE doc_number = $1",
            doc_number
        )
        return {"document": dict(doc), "details": [dict(d) for d in details]}
    finally:
        await conn.close()

# ========== 入庫/退貨 ==========
@router.get("/pending-inbound")
async def get_pending_inbound(current_user: TokenData = Depends(get_current_user)):
    conn = await get_db_connection()
    rows = await conn.fetch(
        """
        SELECT d.doc_number, dd.material_code, COALESCE(dd.material_name, m.name) AS material_name, 
               COALESCE(dd.spec, m.spec) AS spec, dd.accepted_qty AS qty, m.location AS default_location,
               it.assigned_to, it.id AS task_id
        FROM documents d 
        JOIN document_details dd ON d.doc_number = dd.doc_number 
        LEFT JOIN materials m ON dd.material_code = m.material_code
        LEFT JOIN inbound_tasks it ON d.doc_number = it.doc_number AND dd.material_code = it.material_code AND it.status != 'completed'
        WHERE d.status = '檢驗完成' AND dd.accepted_qty > 0
        """
    )
    return [dict(r) for r in rows]

@router.post("/inbound")
async def execute_inbound(req: InboundRequest, current_user: TokenData = Depends(get_current_user)):
    if current_user.role not in ['warehouse', 'admin']:
        raise HTTPException(403, "權限不足")
    conn = await get_db_connection()
    async with conn.transaction():
        first = req.items[0]
        doc_number = await conn.fetchval("SELECT doc_number FROM document_details WHERE material_code = $1 AND accepted_qty > 0 LIMIT 1", first.material_code)
        if not doc_number:
            raise HTTPException(404, "無待入庫記錄")
        doc = await conn.fetchrow("SELECT status FROM documents WHERE doc_number = $1 FOR UPDATE", doc_number)
        if not doc or doc['status'] != '檢驗完成':
            raise HTTPException(409, "單據狀態不是檢驗完成")
        for item in req.items:
            detail = await conn.fetchrow("SELECT id, accepted_qty FROM document_details WHERE doc_number = $1 AND material_code = $2 FOR UPDATE", doc_number, item.material_code)
            if not detail or item.received_qty > detail['accepted_qty']:
                raise HTTPException(400, "入庫數量超過合格數量")
            material = await conn.fetchrow("SELECT stock_qty, version FROM materials WHERE material_code = $1 FOR UPDATE", item.material_code)
            new_stock = material['stock_qty'] + item.received_qty
            await conn.execute(
                "UPDATE materials SET stock_qty = $1, version = version + 1, updated_at = NOW() WHERE material_code = $2 AND version = $3",
                new_stock, item.material_code, material['version']
            )
            await conn.execute(
                """
                INSERT INTO stock_ledger (material_code, warehouse, change_type, change_qty, before_qty, after_qty, reference_doc, operator, remark)
                VALUES ($1, $2, 'INBOUND', $3, $4, $5, $6, $7, $8)
                """,
                item.material_code, '主儲倉', item.received_qty, material['stock_qty'], new_stock, doc_number, req.operator, '採購入庫'
            )
            await conn.execute("UPDATE document_details SET accepted_qty = accepted_qty - $1 WHERE id = $2", item.received_qty, detail['id'])
        remaining = await conn.fetchval("SELECT SUM(accepted_qty) FROM document_details WHERE doc_number = $1", doc_number)
        if remaining == 0:
            await conn.execute("UPDATE documents SET status = '已入庫', current_site = '主儲倉', updated_at = NOW() WHERE doc_number = $1", doc_number)
    return {"success": True}

@router.get("/pending-return")
async def get_pending_return(current_user: TokenData = Depends(get_current_user)):
    conn = await get_db_connection()
    rows = await conn.fetch(
        "SELECT doc_number, material_code, material_name, spec, reject_qty AS qty "
        "FROM document_details WHERE reject_qty > 0"
    )
    return [dict(r) for r in rows]

@router.post("/return")
async def execute_return(req: InboundRequest, current_user: TokenData = Depends(get_current_user)):
    if current_user.role not in ['warehouse', 'admin', 'receiver']:
        raise HTTPException(403, "權限不足")
    conn = await get_db_connection()
    async with conn.transaction():
        if not req.items:
            raise HTTPException(400, "退貨項目不可為空")
        doc_numbers = set()
        for item in req.items:
            row = await conn.fetchrow(
                "SELECT doc_number FROM document_details WHERE material_code = $1 AND reject_qty > 0 LIMIT 1",
                item.material_code
            )
            if row:
                doc_numbers.add(row['doc_number'])
        if not doc_numbers:
            raise HTTPException(404, "無待退貨記錄")
        if len(doc_numbers) != 1:
            raise HTTPException(400, "一次退貨只能處理同一張單據，請分批操作")
        doc_number = next(iter(doc_numbers))
        for item in req.items:
            detail = await conn.fetchrow(
                "SELECT id, reject_qty FROM document_details WHERE doc_number = $1 AND material_code = $2 FOR UPDATE",
                doc_number, item.material_code
            )
            if not detail:
                raise HTTPException(404, f"物料 {item.material_code} 沒有不良品記錄")
            if item.received_qty > detail['reject_qty']:
                raise HTTPException(400, f"退貨數量 {item.received_qty} 超過不良數量 {detail['reject_qty']}")
            new_reject_qty = detail['reject_qty'] - item.received_qty
            await conn.execute("UPDATE document_details SET reject_qty = $1 WHERE id = $2", new_reject_qty, detail['id'])
            await conn.execute(
                """
                INSERT INTO stock_ledger (material_code, warehouse, change_type, change_qty, before_qty, after_qty, reference_doc, operator, remark)
                VALUES ($1, $2, 'RETURN', $3, $4, $5, $6, $7, $8)
                """,
                item.material_code, '不良品倉', item.received_qty, detail['reject_qty'], new_reject_qty, doc_number, req.operator, '不良品退貨'
            )
    return {"success": True, "message": "退貨處理完成"}

# ========== 發料（檢貨單上傳） ==========
@router.post("/{doc_number}/pick-from-excel")
async def pick_materials_from_excel(doc_number: str, file: UploadFile = File(...), operator: str = Query(...), current_user: TokenData = Depends(get_current_user)):
    if current_user.role not in ['admin', 'warehouse']:
        raise HTTPException(403, "權限不足")
    if not file.filename.endswith(('.xlsx', '.xls')):
        raise HTTPException(400, "僅支援 .xlsx 或 .xls 檔案")
    contents = await file.read()
    try:
        df = pd.read_excel(io.BytesIO(contents), engine='openpyxl')
    except Exception as e:
        raise HTTPException(400, f"Excel解析失敗: {str(e)}")
    
    material_col = next((col for col in df.columns if '料號' in col or '料号' in col), None)
    if not material_col:
        raise HTTPException(400, "Excel 缺少料號欄位")
    qty_col = next((col for col in df.columns if '數量' in col or '数量' in col), None)
    
    conn = await get_db_connection()
    async with conn.transaction():
        doc = await conn.fetchrow("SELECT status, doc_type, picking_status FROM documents WHERE doc_number = $1 FOR UPDATE", doc_number)
        if not doc:
            await conn.execute(
                "INSERT INTO documents (doc_number, doc_type, status, current_site, picking_status) VALUES ($1, 'manufacture', '待發料', '主儲倉', '待指派')",
                doc_number
            )
            doc = await conn.fetchrow("SELECT status, doc_type, picking_status FROM documents WHERE doc_number = $1 FOR UPDATE", doc_number)
        
        if doc['picking_status'] not in ['待指派', '待檢貨', '待检货']:
            raise HTTPException(409, f"目前檢貨狀態 {doc['picking_status']} 不允許上傳檢貨單")
        await conn.execute("DELETE FROM document_details WHERE doc_number = $1", doc_number)
        success_items = []
        for _, row in df.iterrows():
            material_code = str(row[material_col]).strip()
            if not material_code:
                continue
            qty = int(row[qty_col]) if qty_col and pd.notna(row[qty_col]) else 1
            material = await conn.fetchrow("SELECT name, spec FROM materials WHERE material_code = $1", material_code)
            if not material:
                raise HTTPException(404, f"料號 {material_code} 不存在於物料主檔")
            await conn.execute(
                "INSERT INTO document_details (doc_number, material_code, material_name, spec, required_qty, issued_qty) VALUES ($1, $2, $3, $4, $5, 0)",
                doc_number, material_code, material['name'], material['spec'], qty
            )
            success_items.append({"material_code": material_code, "qty": qty})
        await conn.execute(
            "UPDATE documents SET picking_status = '待指派', status = '待發料', updated_at = NOW() WHERE doc_number = $1",
            doc_number
        )
        return {"success": True, "items": success_items}

@router.delete("/{doc_number}/pick-list")
async def delete_pick_list(doc_number: str, operator: str = Query(...), current_user: TokenData = Depends(get_current_user)):
    if current_user.role not in ['admin', 'warehouse']:
        raise HTTPException(403, "權限不足")
    conn = await get_db_connection()
    async with conn.transaction():
        doc = await conn.fetchrow("SELECT picking_status, status FROM documents WHERE doc_number = $1 FOR UPDATE", doc_number)
        if not doc:
            raise HTTPException(404, "單據不存在")
        if doc['picking_status'] not in ['待指派', '待檢貨', '待检货']:
            raise HTTPException(409, f"目前檢貨狀態 {doc['picking_status']} 不允許刪除檢貨單")
        issued = await conn.fetchval("SELECT SUM(issued_qty) FROM document_details WHERE doc_number = $1", doc_number)
        if issued and issued > 0:
            raise HTTPException(409, "已有部分發料記錄，無法刪除檢貨單")
        await conn.execute("DELETE FROM document_details WHERE doc_number = $1", doc_number)
        await conn.execute(
            "UPDATE documents SET picking_status = '待指派', status = '待發料', assigned_picker = NULL, updated_at = NOW() WHERE doc_number = $1",
            doc_number
        )
    return {"success": True}

@router.patch("/{doc_number}/assign-picker")
async def assign_picker(doc_number: str, assigned_to: str = Query(...), current_user: TokenData = Depends(get_current_user)):
    if current_user.role not in ['admin', 'warehouse']:
        raise HTTPException(403, "權限不足")
    conn = await get_db_connection()
    async with conn.transaction():
        doc = await conn.fetchrow("SELECT picking_status FROM documents WHERE doc_number = $1 FOR UPDATE", doc_number)
        if not doc:
            raise HTTPException(404, "單據不存在")
        if doc['picking_status'] not in ['待指派', '待檢貨', '待检货', '進行中', '进行中']:
            raise HTTPException(409, f"目前狀態 {doc['picking_status']} 不允許指派")
        await conn.execute(
            "UPDATE documents SET assigned_picker = $1, picking_status = '待檢貨' WHERE doc_number = $2",
            assigned_to, doc_number
        )
    return {"success": True}

# ========== 檢貨員任務 ==========
@router.get("/my-picking-tasks")
async def get_my_picking_tasks(current_user: TokenData = Depends(get_current_user)):
    conn = await get_db_connection()
    try:
        rows = await conn.fetch(
            "SELECT doc_number, doc_type, picking_status, status FROM documents WHERE assigned_picker = $1 AND picking_status IN ('待檢貨', '待检货', '進行中', '进行中')",
            current_user.username
        )
        return [dict(r) for r in rows]
    finally:
        await conn.close()

@router.get("/picking-tasks/{doc_number}/items")
async def get_picking_task_items(doc_number: str, current_user: TokenData = Depends(get_current_user)):
    conn = await get_db_connection()
    try:
        doc = await conn.fetchrow("SELECT assigned_picker, picking_status FROM documents WHERE doc_number = $1", doc_number)
        if not doc or doc['assigned_picker'] != current_user.username:
            raise HTTPException(403, "您不是該任務的指定檢貨員")
        rows = await conn.fetch(
            "SELECT material_code, material_name, spec, required_qty, issued_qty, (required_qty - issued_qty) AS pending_qty FROM document_details WHERE doc_number = $1",
            doc_number
        )
        return [dict(r) for r in rows]
    finally:
        await conn.close()

# ========== 檢貨完成寫入包裝倉 ==========
@router.post("/picking-tasks/{doc_number}/pick")
async def pick_item(doc_number: str, req: PickItemRequest, current_user: TokenData = Depends(get_current_user)):
    if req.operator != current_user.username:
        raise HTTPException(403, "操作員不匹配")
    conn = await get_db_connection()
    async with conn.transaction():
        doc = await conn.fetchrow("SELECT assigned_picker, picking_status FROM documents WHERE doc_number = $1 FOR UPDATE", doc_number)
        if not doc or doc['assigned_picker'] != req.operator:
            raise HTTPException(403, "您不是該任務的指定檢貨員")
        if doc['picking_status'] not in ['待檢貨', '待检货', '進行中', '进行中']:
            raise HTTPException(409, "任務狀態不允許發料")
        detail = await conn.fetchrow(
            "SELECT id, required_qty, issued_qty FROM document_details WHERE doc_number = $1 AND material_code = $2 FOR UPDATE",
            doc_number, req.material_code
        )
        if not detail:
            raise HTTPException(404, "物料不在檢貨單中")
        new_issued = detail['issued_qty'] + req.picked_qty
        if new_issued > detail['required_qty']:
            raise HTTPException(400, f"發料數量超過需求 {detail['required_qty']}")
        await conn.execute(
            "UPDATE document_details SET issued_qty = $1, version = version + 1, updated_at = NOW() WHERE id = $2",
            new_issued, detail['id']
        )
        material = await conn.fetchrow("SELECT stock_qty, version FROM materials WHERE material_code = $1 FOR UPDATE", req.material_code)
        if material['stock_qty'] < req.picked_qty:
            raise HTTPException(400, "庫存不足")
        new_stock = material['stock_qty'] - req.picked_qty
        await conn.execute(
            "UPDATE materials SET stock_qty = $1, version = version + 1, updated_at = NOW() WHERE material_code = $2 AND version = $3",
            new_stock, req.material_code, material['version']
        )
        await conn.execute(
            """
            INSERT INTO stock_ledger (material_code, warehouse, change_type, change_qty, before_qty, after_qty, reference_doc, operator, remark)
            VALUES ($1, '主儲倉', 'OUTBOUND', $2, $3, $4, $5, $6, '製令發料')
            """,
            req.material_code, req.picked_qty, material['stock_qty'], new_stock, doc_number, req.operator
        )
        all_done = await conn.fetchval(
            "SELECT COUNT(*) FROM document_details WHERE doc_number = $1 AND issued_qty < required_qty",
            doc_number
        )
        if all_done == 0:
            await conn.execute(
                "UPDATE documents SET picking_status = '已完成', status = '待包裝', updated_at = NOW() WHERE doc_number = $1",
                doc_number
            )
            rows = await conn.fetch(
                "SELECT material_code, issued_qty FROM document_details WHERE doc_number = $1",
                doc_number
            )
            for r in rows:
                await conn.execute(
                    """
                    INSERT INTO warehouse_stock (doc_number, material_code, warehouse, qty)
                    VALUES ($1, $2, '包裝倉', $3)
                    ON CONFLICT (doc_number, material_code, warehouse)
                    DO UPDATE SET qty = EXCLUDED.qty
                    """,
                    doc_number, r['material_code'], r['issued_qty']
                )
        else:
            if doc['picking_status'] in ['待檢貨', '待检货']:
                await conn.execute("UPDATE documents SET picking_status = '進行中' WHERE doc_number = $1", doc_number)
    return {"success": True, "message": f"已發料 {req.picked_qty} 個 {req.material_code}"}

# ========== 箱子操作 ==========
@router.post("/{doc_number}/start-box")
async def start_box(doc_number: str, req: StartBoxRequest, current_user: TokenData = Depends(get_current_user)):
    if current_user.role not in ['warehouse', 'admin', 'packer']:
        raise HTTPException(403, "權限不足")
    conn = await get_db_connection()
    try:
        doc = await conn.fetchrow("SELECT status FROM documents WHERE doc_number = $1", doc_number)
        if not doc or doc['status'] != '待包裝':
            raise HTTPException(409, "單據狀態不允許包裝")
        existing = await conn.fetchrow("SELECT box_number FROM boxes WHERE box_number = $1", req.box_number)
        if existing:
            raise HTTPException(409, "箱子編號已存在")
        await conn.execute(
            "INSERT INTO boxes (box_number, parent_doc_number, qty_inside, current_site, status, operator) VALUES ($1, $2, 0, '包裝倉', '包裝中', $3)",
            req.box_number, doc_number, req.operator
        )
        return {"success": True}
    finally:
        await conn.close()

# 移動箱子到待出貨倉（同步更新 warehouse_stock）
@router.post("/{doc_number}/move-boxes-to-shipping")
async def move_boxes_to_shipping(doc_number: str, req: MoveBoxesRequest, current_user: TokenData = Depends(get_current_user)):
    if current_user.role not in ['warehouse', 'admin', 'packer']:
        raise HTTPException(403, "權限不足")
    conn = await get_db_connection()
    async with conn.transaction():
        doc = await conn.fetchrow("SELECT doc_number FROM documents WHERE doc_number = $1 FOR UPDATE", doc_number)
        if not doc:
            raise HTTPException(404, "單據不存在")
        moved = 0
        for box_number in req.box_numbers:
            box = await conn.fetchrow("SELECT status, parent_doc_number FROM boxes WHERE box_number = $1 FOR UPDATE", box_number)
            if not box or box['parent_doc_number'] != doc_number or box['status'] != '已封箱':
                raise HTTPException(400, f"箱子 {box_number} 狀態不允許移轉")
            # 取得箱內物料明細
            details = await conn.fetch(
                "SELECT material_code, quantity FROM box_details WHERE box_number = $1",
                box_number
            )
            for d in details:
                # 從包裝成品倉扣減
                await conn.execute(
                    "UPDATE warehouse_stock SET qty = qty - $1 WHERE doc_number = $2 AND material_code = $3 AND warehouse = '包裝成品倉'",
                    d['quantity'], doc_number, d['material_code']
                )
                # 刪除數量為0的包裝成品倉記錄
                await conn.execute(
                    "DELETE FROM warehouse_stock WHERE doc_number = $1 AND material_code = $2 AND warehouse = '包裝成品倉' AND qty = 0",
                    doc_number, d['material_code']
                )
                # 增加到待出貨倉
                await conn.execute(
                    """
                    INSERT INTO warehouse_stock (doc_number, material_code, warehouse, qty)
                    VALUES ($1, $2, '待出貨倉', $3)
                    ON CONFLICT (doc_number, material_code, warehouse)
                    DO UPDATE SET qty = warehouse_stock.qty + EXCLUDED.qty
                    """,
                    doc_number, d['material_code'], d['quantity']
                )
            await conn.execute(
                "UPDATE boxes SET status = '待出貨倉', current_site = '待出貨倉', updated_at = NOW() WHERE box_number = $1",
                box_number
            )
            moved += 1
        remaining = await conn.fetchval("SELECT COUNT(*) FROM boxes WHERE parent_doc_number = $1 AND status = '已封箱'", doc_number)
        if remaining == 0:
            await conn.execute(
                "UPDATE documents SET status = '待出庫', current_site = '待出貨倉', updated_at = NOW() WHERE doc_number = $1",
                doc_number
            )
    return {"success": True, "message": f"已將 {moved} 個箱子移至待出貨倉"}

@router.post("/boxes/{box_number}/unseal")
async def unseal_box(box_number: str, operator: str = Query(...), current_user: TokenData = Depends(get_current_user)):
    if current_user.role not in ['warehouse', 'admin', 'packer']:
        raise HTTPException(403, "權限不足")
    conn = await get_db_connection()
    async with conn.transaction():
        box = await conn.fetchrow("SELECT status, parent_doc_number FROM boxes WHERE box_number = $1 FOR UPDATE", box_number)
        if not box:
            raise HTTPException(404, "箱子不存在")
        if box['status'] != '已封箱':
            raise HTTPException(409, "只有已封箱的箱子可以解封")
        # 解封時需要將物料從包裝成品倉移回包裝倉（這裡先不做，因為複雜，暫不處理）
        await conn.execute(
            "UPDATE boxes SET status = '包裝中', current_site = '包裝倉', updated_at = NOW() WHERE box_number = $1",
            box_number
        )
    return {"success": True, "message": "箱子已解封，可繼續添加物料"}

@router.get("/with-sealed-boxes")
async def get_documents_with_sealed_boxes(current_user: TokenData = Depends(get_current_user)):
    conn = await get_db_connection()
    try:
        rows = await conn.fetch(
            """
            SELECT DISTINCT d.doc_number, d.doc_type, COUNT(b.box_number) as box_count
            FROM documents d
            JOIN boxes b ON d.doc_number = b.parent_doc_number
            WHERE b.status = '已封箱'
            GROUP BY d.doc_number, d.doc_type
            ORDER BY d.doc_number
            """
        )
        return [dict(r) for r in rows]
    finally:
        await conn.close()

# 出貨（同步更新 warehouse_stock）
@router.post("/ship-boxes")
async def ship_boxes(req: ShipBoxesRequest, current_user: TokenData = Depends(get_current_user)):
    if current_user.role not in ['receiver', 'admin']:
        raise HTTPException(403, "權限不足")
    conn = await get_db_connection()
    async with conn.transaction():
        invalid_boxes = []
        doc_numbers = set()
        for box_number in req.box_numbers:
            box = await conn.fetchrow("SELECT status, parent_doc_number FROM boxes WHERE box_number = $1 FOR UPDATE", box_number)
            if not box:
                invalid_boxes.append(f"{box_number} (不存在)")
            elif box['status'] != '待出貨倉':
                invalid_boxes.append(f"{box_number} (狀態不是待出貨倉)")
            else:
                doc_numbers.add(box['parent_doc_number'])
        if invalid_boxes:
            raise HTTPException(400, f"無效箱子: {', '.join(invalid_boxes)}")

        for box_number in req.box_numbers:
            # 取得箱子明細
            details = await conn.fetch(
                "SELECT material_code, quantity FROM box_details WHERE box_number = $1",
                box_number
            )
            box = await conn.fetchrow("SELECT parent_doc_number FROM boxes WHERE box_number = $1", box_number)
            doc_number = box['parent_doc_number']

            for d in details:
                material_code = d['material_code']
                qty = d['quantity']
                # 從 warehouse_stock 扣減待出貨倉庫存（確保庫存正確）
                await conn.execute(
                    "UPDATE warehouse_stock SET qty = qty - $1 WHERE doc_number = $2 AND material_code = $3 AND warehouse = '待出貨倉'",
                    qty, doc_number, material_code
                )
                # 刪除數量為0的記錄
                await conn.execute(
                    "DELETE FROM warehouse_stock WHERE doc_number = $1 AND material_code = $2 AND warehouse = '待出貨倉' AND qty = 0",
                    doc_number, material_code
                )
                # 寫入 stock_ledger 臺帳（出庫）
                # 需要查詢出貨前的庫存量（可從 warehouse_stock 或歷史推算，此處簡單記錄 -qty，before_qty/after_qty 可暫設0，因出貨後數量已歸零）
                # 為了完整，可以查詢 warehouse_stock 扣減前的數量，但這裡為簡化，直接記錄 change_qty 為 qty，before/after 可忽略
                await conn.execute(
                    """
                    INSERT INTO stock_ledger (material_code, warehouse, change_type, change_qty, before_qty, after_qty, reference_doc, operator, remark)
                    VALUES ($1, '待出貨倉', 'OUTBOUND', $2, 0, 0, $3, $4, '箱子出貨')
                    """,
                    material_code, qty, doc_number, req.operator
                )
            # 更新箱子狀態
            await conn.execute(
                "UPDATE boxes SET status = '已出庫', current_site = '出庫完成', updated_at = NOW() WHERE box_number = $1",
                box_number
            )

        for doc_number in doc_numbers:
            remaining = await conn.fetchval(
                "SELECT COUNT(*) FROM boxes WHERE parent_doc_number = $1 AND status != '已出庫'",
                doc_number
            )
            if remaining == 0:
                await conn.execute(
                    "UPDATE documents SET status = '已出庫', current_site = '出庫完成', updated_at = NOW() WHERE doc_number = $1",
                    doc_number
                )
    return {"success": True, "message": f"已出貨 {len(req.box_numbers)} 個箱子"}