from fastapi import APIRouter, HTTPException, Depends, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel
from datetime import datetime, timedelta
from typing import Optional
from database import get_db_connection
import os

# 從環境變數讀取敏感資訊（若無則使用預設值，但生產環境務必設定）
SECRET_KEY = (
    os.getenv("SECRET_KEY")
    or os.getenv("JWT_SECRET_KEY")
    or "your-secret-key-change-this-in-production"
)
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "1440"))

router = APIRouter(prefix="/auth", tags=["authentication"])

pwd_context = CryptContext(schemes=["plaintext"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token")

class Token(BaseModel):
    access_token: str
    token_type: str

class TokenData(BaseModel):
    username: str
    role: str

class User(BaseModel):
    username: str
    role: str
    disabled: Optional[bool] = None

def verify_password(plain_password, hashed_password):
    return plain_password == hashed_password

async def authenticate_user(username_or_name: str, password: str):
    """支持用户名或姓名登录"""
    conn = await get_db_connection()
    try:
        row = await conn.fetchrow(
            "SELECT username, hashed_password, role, is_active FROM system_users WHERE (username = $1 OR full_name = $1) AND is_active = true",
            username_or_name
        )
        if not row:
            return False
        if not verify_password(password, row['hashed_password']):
            return False
        return dict(row)
    finally:
        await conn.close()

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=15)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

async def get_current_user(token: str = Depends(oauth2_scheme)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        role: str = payload.get("role")
        if username is None or role is None:
            raise credentials_exception
        token_data = TokenData(username=username, role=role)
    except JWTError:
        raise credentials_exception
    conn = await get_db_connection()
    try:
        user = await conn.fetchrow("SELECT username, is_active FROM system_users WHERE username = $1", token_data.username)
        if not user or not user['is_active']:
            raise credentials_exception
    finally:
        await conn.close()
    return token_data

@router.post("/token", response_model=Token)
async def login_for_access_token(form_data: OAuth2PasswordRequestForm = Depends()):
    user = await authenticate_user(form_data.username, form_data.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user['username'], "role": user['role']},
        expires_delta=access_token_expires
    )
    return {"access_token": access_token, "token_type": "bearer"}

@router.post("/quick-login", response_model=Token)
async def quick_login(token: str):
    conn = await get_db_connection()
    try:
        row = await conn.fetchrow(
            "SELECT username, role, is_active FROM system_users WHERE quick_login_token = $1 AND is_active = true",
            token
        )
        if not row:
            raise HTTPException(status_code=401, detail="Invalid quick login token")
        access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
        access_token = create_access_token(
            data={"sub": row['username'], "role": row['role']},
            expires_delta=access_token_expires
        )
        return {"access_token": access_token, "token_type": "bearer"}
    finally:
        await conn.close()

@router.get("/users/me", response_model=User)
async def read_users_me(current_user: TokenData = Depends(get_current_user)):
    return User(username=current_user.username, role=current_user.role)

@router.get("/login-users")
async def get_login_users():
    """返回所有可登录的活跃用户（用于登录页下拉框）"""
    try:
        conn = await get_db_connection()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Database connection failed: {e}")
    try:
        rows = await conn.fetch(
            "SELECT username, full_name, role FROM system_users WHERE is_active = true ORDER BY role, username"
        )
        return [{"username": r["username"], "full_name": r["full_name"], "role": r["role"]} for r in rows]
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Failed to load users: {e}")
    finally:
        await conn.close()