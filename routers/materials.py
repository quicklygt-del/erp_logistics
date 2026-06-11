from fastapi import APIRouter, HTTPException, Query, UploadFile, File, Depends
from typing import Optional, List
from datetime import datetime
import pandas as pd
import io
import xlsxwriter
from fastapi.responses import StreamingResponse
from database import get_db_connection
from routers.auth import get_current_user, TokenData

router = APIRouter()

# ========== 物料主檔 CRUD ==========
@router.get("/")
async def list_materials(
    limit: int = Query(15, ge=1, le=100),
    offset: int = Query(0, ge=0),
    search: Optional[str] = Query(None),
    current_user: TokenData = Depends(get_current_user)
):
    """取得物料列表（分頁、搜尋）"""
    conn = await get_db_connection()
    try:
        where_clause = ""
        params = []
        if search:
            where_clause = " WHERE material_code ILIKE $1 OR name ILIKE $1"
            params.append(f"%{search}%")
        # 總筆數
        total = await conn.fetchval(f"SELECT COUNT(*) FROM materials{where_clause}", *params)
        # 查詢資料
        query = f"SELECT * FROM materials{where_clause} ORDER BY material_code LIMIT ${len(params)+1} OFFSET ${len(params)+2}"
        params.extend([limit, offset])
        rows = await conn.fetch(query, *params)
        return {"data": [dict(r) for r in rows], "total": total}
    finally:
        await conn.close()

@router.get("/{material_code}")
async def get_material(material_code: str, current_user: TokenData = Depends(get_current_user)):
    """查詢單一物料"""
    conn = await get_db_connection()
    try:
        row = await conn.fetchrow("SELECT * FROM materials WHERE material_code = $1", material_code)
        if not row:
            raise HTTPException(404, "物料不存在")
        return dict(row)
    finally:
        await conn.close()

@router.post("/")
async def create_material(
    material_code: str,
    name: str,
    spec: Optional[str] = None,
    stock_qty: int = 0,
    location: Optional[str] = None,
    safety_stock: int = 0,
    current_user: TokenData = Depends(get_current_user)
):
    if current_user.role not in ['admin', 'warehouse']:
        raise HTTPException(403, "權限不足")
    conn = await get_db_connection()
    try:
        existing = await conn.fetchrow("SELECT material_code FROM materials WHERE material_code = $1", material_code)
        if existing:
            raise HTTPException(409, "物料編號已存在")
        await conn.execute(
            "INSERT INTO materials (material_code, name, spec, stock_qty, location, safety_stock) VALUES ($1, $2, $3, $4, $5, $6)",
            material_code, name, spec, stock_qty, location, safety_stock
        )
        return {"success": True}
    finally:
        await conn.close()

@router.put("/{material_code}")
async def update_material(
    material_code: str,
    name: str,
    spec: Optional[str] = None,
    stock_qty: int = 0,
    location: Optional[str] = None,
    safety_stock: int = 0,
    current_user: TokenData = Depends(get_current_user)
):
    if current_user.role not in ['admin', 'warehouse']:
        raise HTTPException(403, "權限不足")
    conn = await get_db_connection()
    try:
        result = await conn.execute(
            "UPDATE materials SET name = $1, spec = $2, stock_qty = $3, location = $4, safety_stock = $5, version = version + 1, updated_at = NOW() WHERE material_code = $6",
            name, spec, stock_qty, location, safety_stock, material_code
        )
        if result == "UPDATE 0":
            raise HTTPException(404, "物料不存在")
        return {"success": True}
    finally:
        await conn.close()

@router.delete("/{material_code}")
async def delete_material(material_code: str, current_user: TokenData = Depends(get_current_user)):
    if current_user.role not in ['admin', 'warehouse']:
        raise HTTPException(403, "權限不足")
    conn = await get_db_connection()
    try:
        result = await conn.execute("DELETE FROM materials WHERE material_code = $1", material_code)
        if result == "DELETE 0":
            raise HTTPException(404, "物料不存在")
        return {"success": True}
    finally:
        await conn.close()

# ========== 上傳 Excel ==========
@router.post("/upload-excel")
async def upload_excel(
    mode: str = Query("update", description="update 或 overwrite"),
    operator: str = Query(...),
    file: UploadFile = File(...),
    current_user: TokenData = Depends(get_current_user)
):
    if current_user.role not in ['admin', 'warehouse']:
        raise HTTPException(403, "權限不足")
    
    if not file.filename.endswith(('.xlsx', '.xls')):
        raise HTTPException(400, "僅支援 .xlsx 或 .xls 檔案")
    
    contents = await file.read()
    try:
        df = pd.read_excel(io.BytesIO(contents), engine='openpyxl')
    except Exception as e:
        raise HTTPException(400, f"Excel解析失敗: {str(e)}")
    
    # 必須包含的欄位
    required_cols = ['料號', '品名']
    for col in required_cols:
        if col not in df.columns and col.replace('料號','料号') not in df.columns:
            raise HTTPException(400, f"Excel 缺少欄位: {col}")
    
    # 確定實際使用的欄位名稱
    material_col = '料號' if '料號' in df.columns else '料号'
    name_col = '品名' if '品名' in df.columns else '品名'
    spec_col = '規格' if '規格' in df.columns else ('规格' if '规格' in df.columns else None)
    stock_col = '數量' if '數量' in df.columns else ('数量' if '数量' in df.columns else None)
    location_col = '儲位' if '儲位' in df.columns else ('储位' if '储位' in df.columns else None)
    # 安全庫存列：同時支援繁簡
    safety_stock_col = None
    for possible in ['安全庫存', '安全库存', '安全庫存(下限)', '安全库存(下限)']:
        if possible in df.columns:
            safety_stock_col = possible
            break
    
    conn = await get_db_connection()
    async with conn.transaction():
        if mode == 'overwrite':
            await conn.execute("DELETE FROM materials")
        
        inserted = 0
        updated = 0
        for _, row in df.iterrows():
            material_code = str(row[material_col]).strip()
            if not material_code:
                continue
            name = str(row[name_col]).strip()
            spec = str(row[spec_col]).strip() if spec_col and pd.notna(row[spec_col]) else None
            stock_qty = int(row[stock_col]) if stock_col and pd.notna(row[stock_col]) else 0
            location = str(row[location_col]).strip() if location_col and pd.notna(row[location_col]) else None
            # 安全庫存：如果列不存在或值為空，則設為0
            safety_stock = 0
            if safety_stock_col and pd.notna(row[safety_stock_col]):
                try:
                    safety_stock = int(row[safety_stock_col])
                except:
                    safety_stock = 0
            
            existing = await conn.fetchrow("SELECT material_code FROM materials WHERE material_code = $1", material_code)
            if existing:
                await conn.execute(
                    "UPDATE materials SET name = $1, spec = $2, stock_qty = $3, location = $4, safety_stock = $5, version = version + 1, updated_at = NOW() WHERE material_code = $6",
                    name, spec, stock_qty, location, safety_stock, material_code
                )
                updated += 1
            else:
                await conn.execute(
                    "INSERT INTO materials (material_code, name, spec, stock_qty, location, safety_stock) VALUES ($1, $2, $3, $4, $5, $6)",
                    material_code, name, spec, stock_qty, location, safety_stock
                )
                inserted += 1
        
        return {"success": True, "inserted": inserted, "updated": updated}

# ========== 導出現況帳 ==========
@router.get("/export")
async def export_materials(current_user: TokenData = Depends(get_current_user)):
    """導出所有物料主檔為 Excel"""
    conn = await get_db_connection()
    try:
        rows = await conn.fetch("SELECT material_code, name, spec, stock_qty, location, safety_stock, created_at, updated_at FROM materials ORDER BY material_code")
        if not rows:
            raise HTTPException(404, "無物料資料")
        
        # 建立 Excel
        output = io.BytesIO()
        workbook = xlsxwriter.Workbook(output, {'in_memory': True})
        worksheet = workbook.add_worksheet("物料主檔")
        
        headers = ["料號", "品名", "規格", "庫存數量", "儲位", "安全庫存", "建立時間", "更新時間"]
        for col, h in enumerate(headers):
            worksheet.write(0, col, h)
        
        for row_idx, row in enumerate(rows, start=1):
            worksheet.write(row_idx, 0, row['material_code'])
            worksheet.write(row_idx, 1, row['name'])
            worksheet.write(row_idx, 2, row['spec'] or '')
            worksheet.write(row_idx, 3, row['stock_qty'])
            worksheet.write(row_idx, 4, row['location'] or '')
            worksheet.write(row_idx, 5, row['safety_stock'] or 0)
            worksheet.write(row_idx, 6, row['created_at'].isoformat() if row['created_at'] else '')
            worksheet.write(row_idx, 7, row['updated_at'].isoformat() if row['updated_at'] else '')
        
        workbook.close()
        output.seek(0)
        return StreamingResponse(
            output,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": "attachment; filename=materials.xlsx"}
        )
    finally:
        await conn.close()