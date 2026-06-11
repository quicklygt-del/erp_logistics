from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse
from database import get_db_connection
from routers import materials, documents, quality, boxes, inventory, auth, admin_users, alert_rules, transactions
from routers.auth import get_current_user, TokenData

app = FastAPI(title="ERP 輔助物流系統 API", version="1.0.0")

# CORS 配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ========== 修改點 1：將靜態檔案目錄掛載到根路徑 ==========
# 原本是 app.mount("/static", StaticFiles(directory="."), name="static")
# 改為掛載 static 資料夾到根路徑，並啟用 html 模式
app.mount("/", StaticFiles(directory="static", html=True), name="static")

app.include_router(materials.router, prefix="/api/v1/materials", tags=["materials"])
app.include_router(documents.router, prefix="/api/v1/documents", tags=["documents"])
app.include_router(quality.router, prefix="/api/v1/quality", tags=["quality"])
app.include_router(boxes.router, prefix="/api/v1/boxes", tags=["boxes"])
app.include_router(inventory.router, prefix="/api/v1/inventory", tags=["inventory"])
app.include_router(auth.router, prefix="/api/v1", tags=["authentication"])
app.include_router(admin_users.router, prefix="/api/v1", tags=["admin"])
app.include_router(alert_rules.router, prefix="/api/v1", tags=["alert_rules"])
app.include_router(transactions.router, prefix="/api/v1", tags=["transactions"])

def _naive_utc(dt):
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt

@app.get("/api/v1/admin/dashboard/overview")
async def admin_overview(current_user: TokenData = Depends(get_current_user)):
    if current_user.role not in ['admin', 'warehouse']:
        raise HTTPException(403, "權限不足")

    conn = await get_db_connection()
    try:
        inbound_pending = await conn.fetchval("SELECT COUNT(*) FROM inbound_tasks WHERE status != 'completed'")
        # 只统计有明细的检货任务
        picking_pending = await conn.fetchval(
            """
            SELECT COUNT(DISTINCT d.doc_number)
            FROM documents d
            INNER JOIN document_details dd ON d.doc_number = dd.doc_number
            WHERE d.doc_type = 'manufacture' 
              AND d.picking_status IN ('待指派', '待檢貨', '進行中')
              AND dd.required_qty > 0
            """
        )
        stocktake_pending = await conn.fetchval(
            "SELECT COUNT(*) FROM stock_take_sheets WHERE status IN ('待盤點', '進行中')"
        )

        low_stock_items = await conn.fetch(
            """
            SELECT 
                m.material_code,
                m.name,
                m.spec,
                m.location,
                COALESCE(m.safety_stock, 0) AS safety_stock,
                COALESCE(m.stock_qty, 0) AS main_stock,
                COALESCE(
                    (SELECT SUM(dd.accepted_qty) 
                     FROM documents d 
                     JOIN document_details dd ON d.doc_number = dd.doc_number 
                     WHERE d.status = '檢驗完成' AND dd.material_code = m.material_code), 0
                ) AS pending_inbound_qty
            FROM materials m
            WHERE COALESCE(m.safety_stock, 0) > 0
              AND (COALESCE(m.stock_qty, 0) + COALESCE(
                    (SELECT SUM(dd.accepted_qty) 
                     FROM documents d 
                     JOIN document_details dd ON d.doc_number = dd.doc_number 
                     WHERE d.status = '檢驗完成' AND dd.material_code = m.material_code), 0
                  )) < COALESCE(m.safety_stock, 0)
            ORDER BY (COALESCE(m.safety_stock, 0) - (COALESCE(m.stock_qty, 0) + COALESCE(
                    (SELECT SUM(dd.accepted_qty) 
                     FROM documents d 
                     JOIN document_details dd ON d.doc_number = dd.doc_number 
                     WHERE d.status = '檢驗完成' AND dd.material_code = m.material_code), 0
                  ))) DESC
            LIMIT 20
            """
        )
        low_stock_list = [dict(item) for item in low_stock_items]

        db_ok = True
        try:
            await conn.fetchval("SELECT 1")
        except Exception:
            db_ok = False

        rules = await conn.fetch("SELECT id, rule_name, node_type, threshold_hours FROM alert_rules WHERE is_active = true")
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        timeout_alerts = []

        for rule in rules:
            node_type = rule['node_type']
            threshold_time = now_utc - timedelta(hours=rule['threshold_hours'])

            if node_type == '待驗倉':
                rows = await conn.fetch(
                    "SELECT doc_number, doc_type, created_at, status, '待驗倉' as current_site FROM documents WHERE status = '待驗' AND created_at < $1",
                    threshold_time
                )
                for r in rows:
                    ref_time = _naive_utc(r['created_at'])
                    doc_type_display = '採購單' if r['doc_type'] == 'purchase' else ('製令單' if r['doc_type'] == 'manufacture' else r['doc_type'])
                    timeout_alerts.append({
                        "rule_name": rule['rule_name'],
                        "node_type": node_type,
                        "doc_number": r['doc_number'],
                        "doc_type": doc_type_display,
                        "current_site": r['current_site'],
                        "created_at": ref_time.isoformat(),
                        "waiting_hours": round((now_utc - ref_time).total_seconds() / 3600, 1)
                    })
            elif node_type == '待入庫倉':
                rows = await conn.fetch(
                    "SELECT doc_number, doc_type, updated_at as reference_time, '待入庫倉' as current_site FROM documents WHERE status = '檢驗完成' AND updated_at < $1",
                    threshold_time
                )
                for r in rows:
                    ref_time = _naive_utc(r['reference_time'])
                    doc_type_display = '採購單' if r['doc_type'] == 'purchase' else ('製令單' if r['doc_type'] == 'manufacture' else r['doc_type'])
                    timeout_alerts.append({
                        "rule_name": rule['rule_name'],
                        "node_type": node_type,
                        "doc_number": r['doc_number'],
                        "doc_type": doc_type_display,
                        "current_site": r['current_site'],
                        "created_at": ref_time.isoformat(),
                        "waiting_hours": round((now_utc - ref_time).total_seconds() / 3600, 1)
                    })
            elif node_type == '待出庫倉':
                rows = await conn.fetch(
                    "SELECT doc_number, doc_type, current_site, updated_at as reference_time FROM documents WHERE current_site = '待出貨倉' AND updated_at < $1",
                    threshold_time
                )
                for r in rows:
                    ref_time = _naive_utc(r['reference_time'])
                    doc_type_display = '採購單' if r['doc_type'] == 'purchase' else ('製令單' if r['doc_type'] == 'manufacture' else r['doc_type'])
                    timeout_alerts.append({
                        "rule_name": rule['rule_name'],
                        "node_type": node_type,
                        "doc_number": r['doc_number'],
                        "doc_type": doc_type_display,
                        "current_site": r['current_site'],
                        "created_at": ref_time.isoformat(),
                        "waiting_hours": round((now_utc - ref_time).total_seconds() / 3600, 1)
                    })
            elif node_type == '包裝成品倉':
                rows = await conn.fetch(
                    "SELECT box_number, parent_doc_number as doc_number, current_site, updated_at as reference_time FROM boxes WHERE current_site = '包裝成品倉' AND updated_at < $1",
                    threshold_time
                )
                for r in rows:
                    ref_time = _naive_utc(r['reference_time'])
                    timeout_alerts.append({
                        "rule_name": rule['rule_name'],
                        "node_type": node_type,
                        "doc_number": r['doc_number'],
                        "box_number": r['box_number'],
                        "current_site": r['current_site'],
                        "doc_type": "箱子",
                        "created_at": ref_time.isoformat(),
                        "waiting_hours": round((now_utc - ref_time).total_seconds() / 3600, 1)
                    })
            elif node_type == '待出貨倉':
                rows = await conn.fetch(
                    "SELECT box_number, parent_doc_number as doc_number, current_site, updated_at as reference_time FROM boxes WHERE current_site = '待出貨倉' AND updated_at < $1",
                    threshold_time
                )
                for r in rows:
                    ref_time = _naive_utc(r['reference_time'])
                    timeout_alerts.append({
                        "rule_name": rule['rule_name'],
                        "node_type": node_type,
                        "doc_number": r['doc_number'],
                        "box_number": r['box_number'],
                        "current_site": r['current_site'],
                        "doc_type": "箱子",
                        "created_at": ref_time.isoformat(),
                        "waiting_hours": round((now_utc - ref_time).total_seconds() / 3600, 1)
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
                "timestamp": now_utc.isoformat()
            },
            "timeout_alerts": timeout_alerts
        }
    finally:
        await conn.close()

@app.get("/api/v1/dashboard/task-summary")
async def get_tasks_summary(current_user: TokenData = Depends(get_current_user)):
    if current_user.role not in ['admin', 'warehouse']:
        raise HTTPException(403, "權限不足")
    conn = await get_db_connection()
    try:
        inbound_pending = await conn.fetchval("SELECT COUNT(*) FROM inbound_tasks WHERE status IN ('pending', 'in_progress')")
        # 只统计有明细的检货任务
        picking_pending = await conn.fetchval(
            """
            SELECT COUNT(DISTINCT d.doc_number)
            FROM documents d
            INNER JOIN document_details dd ON d.doc_number = dd.doc_number
            WHERE d.doc_type = 'manufacture' 
              AND d.picking_status IN ('待指派', '待檢貨', '進行中')
              AND dd.required_qty > 0
            """
        )
        stocktake_pending = await conn.fetchval(
            "SELECT COUNT(*) FROM stock_take_sheets WHERE status IN ('待盤點', '進行中')"
        )
        # 统计每个仓管员（operator）的三类任务数量
        workers = await conn.fetch(
            """
            SELECT 
                COALESCE(u.full_name, u.username) AS assigned_to,
                COALESCE(inb.cnt, 0) AS inbound_count,
                COALESCE(pick.cnt, 0) AS picking_count,
                COALESCE(st.cnt, 0) AS stocktake_count,
                COALESCE(inb.cnt, 0) + COALESCE(pick.cnt, 0) + COALESCE(st.cnt, 0) AS task_count
            FROM system_users u
            LEFT JOIN (
                SELECT assigned_to, COUNT(*) AS cnt 
                FROM inbound_tasks 
                WHERE status IN ('pending', 'in_progress')
                GROUP BY assigned_to
            ) inb ON u.username = inb.assigned_to
            LEFT JOIN (
                SELECT assigned_picker, COUNT(*) AS cnt 
                FROM documents 
                WHERE doc_type = 'manufacture' AND picking_status IN ('待指派', '待檢貨', '進行中')
                GROUP BY assigned_picker
            ) pick ON u.username = pick.assigned_picker
            LEFT JOIN (
                SELECT assigned_to, COUNT(*) AS cnt 
                FROM stock_take_sheets 
                WHERE status IN ('待盤點', '進行中')
                GROUP BY assigned_to
            ) st ON u.username = st.assigned_to
            WHERE u.role = 'operator' AND u.is_active = true
              AND (inb.cnt IS NOT NULL OR pick.cnt IS NOT NULL OR st.cnt IS NOT NULL)
            ORDER BY task_count DESC
            """
        )
        worker_list = []
        for w in workers:
            worker_list.append({
                "assigned_to": w['assigned_to'],
                "inbound_count": w['inbound_count'],
                "picking_count": w['picking_count'],
                "stocktake_count": w['stocktake_count'],
                "task_count": w['task_count']
            })
        return {
            "inbound_pending": inbound_pending,
            "picking_pending": picking_pending,
            "stocktake_pending": stocktake_pending,
            "worker_load": worker_list
        }
    finally:
        await conn.close()

@app.get("/health")
async def health_check():
    status = {"api": "healthy", "database": "unknown"}
    try:
        conn = await get_db_connection()
        await conn.fetchval("SELECT 1")
        await conn.close()
        status["database"] = "connected"
    except Exception as e:
        status["database"] = f"error: {str(e)}"
        status["api"] = "degraded"
    return status

# ========== 修改點 2：根路徑重導向到 /login.html（不再需要 /static 前綴） ==========
@app.get("/")
async def root():
    return RedirectResponse(url="/login.html")