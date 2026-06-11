from typing import Optional
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from database import get_db_connection

router = APIRouter()

class AddToBoxRequest(BaseModel):
    operator: str
    material_code: str
    quantity: int

# 1. 取得箱子列表
@router.get("/")
async def list_boxes(
    doc_number: Optional[str] = Query(None),
    status: Optional[str] = Query(None)
):
    conn = await get_db_connection()
    try:
        query = "SELECT * FROM boxes WHERE 1=1"
        params = []
        if doc_number:
            query += " AND parent_doc_number = $" + str(len(params)+1)
            params.append(doc_number)
        if status:
            query += " AND status = $" + str(len(params)+1)
            params.append(status)
        rows = await conn.fetch(query, *params)
        return [dict(r) for r in rows]
    finally:
        await conn.close()

# 2. 取得單個箱子的明細
@router.get("/{box_number}/details")
async def get_box_details(box_number: str):
    conn = await get_db_connection()
    try:
        box = await conn.fetchrow("SELECT * FROM boxes WHERE box_number = $1", box_number)
        if not box:
            raise HTTPException(404, "箱子不存在")
        details = await conn.fetch(
            "SELECT material_code, material_name, spec, quantity FROM box_details WHERE box_number = $1",
            box_number
        )
        return {"box": dict(box), "details": [dict(d) for d in details]}
    finally:
        await conn.close()

# 3. 添加物料到箱子（從包裝倉扣減）
@router.post("/{box_number}/add-item")
async def add_item_to_box(box_number: str, req: AddToBoxRequest):
    conn = await get_db_connection()
    async with conn.transaction():
        box = await conn.fetchrow("SELECT status, parent_doc_number FROM boxes WHERE box_number = $1 FOR UPDATE", box_number)
        if not box or box['status'] != '包裝中':
            raise HTTPException(409, "箱子不可添加")
        doc_number = box['parent_doc_number']
        material_code = req.material_code
        quantity = req.quantity

        stock_row = await conn.fetchrow(
            "SELECT qty FROM warehouse_stock WHERE doc_number = $1 AND material_code = $2 AND warehouse = '包裝倉' FOR UPDATE",
            doc_number, material_code
        )
        if not stock_row or stock_row['qty'] < quantity:
            raise HTTPException(400, f"包裝倉庫存不足，尚餘 {stock_row['qty'] if stock_row else 0}，無法入箱 {quantity}")

        new_qty = stock_row['qty'] - quantity
        if new_qty == 0:
            await conn.execute(
                "DELETE FROM warehouse_stock WHERE doc_number = $1 AND material_code = $2 AND warehouse = '包裝倉'",
                doc_number, material_code
            )
        else:
            await conn.execute(
                "UPDATE warehouse_stock SET qty = $1 WHERE doc_number = $2 AND material_code = $3 AND warehouse = '包裝倉'",
                new_qty, doc_number, material_code
            )

        material = await conn.fetchrow("SELECT name, spec FROM materials WHERE material_code = $1", material_code)
        if not material:
            raise HTTPException(404, "物料不存在")
        existing = await conn.fetchrow(
            "SELECT id, quantity FROM box_details WHERE box_number = $1 AND material_code = $2 FOR UPDATE",
            box_number, material_code
        )
        if existing:
            await conn.execute("UPDATE box_details SET quantity = quantity + $1 WHERE id = $2", quantity, existing['id'])
        else:
            await conn.execute(
                "INSERT INTO box_details (box_number, material_code, material_name, spec, quantity) VALUES ($1, $2, $3, $4, $5)",
                box_number, material_code, material['name'], material['spec'], quantity
            )
        await conn.execute("UPDATE boxes SET qty_inside = qty_inside + $1 WHERE box_number = $2", quantity, box_number)

    return {"success": True}

# 4. 封箱（將箱內物料寫入包裝成品倉）
@router.post("/{box_number}/seal")
async def seal_box(box_number: str, operator: str = Query(...)):
    conn = await get_db_connection()
    async with conn.transaction():
        box = await conn.fetchrow("SELECT status, parent_doc_number FROM boxes WHERE box_number = $1 FOR UPDATE", box_number)
        if not box or box['status'] != '包裝中':
            raise HTTPException(409, "箱子無法封箱")
        doc_number = box['parent_doc_number']
        
        # 取得箱內所有物料明細
        details = await conn.fetch(
            "SELECT material_code, quantity FROM box_details WHERE box_number = $1",
            box_number
        )
        for d in details:
            await conn.execute(
                """
                INSERT INTO warehouse_stock (doc_number, material_code, warehouse, qty)
                VALUES ($1, $2, '包裝成品倉', $3)
                ON CONFLICT (doc_number, material_code, warehouse)
                DO UPDATE SET qty = warehouse_stock.qty + EXCLUDED.qty
                """,
                doc_number, d['material_code'], d['quantity']
            )
        await conn.execute(
            "UPDATE boxes SET status = '已封箱', current_site = '包裝成品倉' WHERE box_number = $1",
            box_number
        )
    return {"success": True}