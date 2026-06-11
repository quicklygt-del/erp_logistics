from fastapi import APIRouter
router = APIRouter()

@router.get("/")
async def get_flows():
    return {"message": "站點流向端點佔位"}