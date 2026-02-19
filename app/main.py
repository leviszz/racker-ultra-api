from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.schemas import UserRead, UserCreate, UserUpdate

from app.scan import router as scan_router
from app.users import fastapi_users, auth_backend
from app.models import User

from app.db import engine, Base
import asyncio
import os

app = FastAPI(title="Racker Ultra PRO Turbo", version="17.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# rotas scanner
app.include_router(scan_router)

# rotas auth
app.include_router(
    fastapi_users.get_auth_router(auth_backend),
    prefix="/auth/jwt",
    tags=["auth"],
)



app.include_router(
    fastapi_users.get_users_router(
        UserRead,
        UserUpdate,
    ),
    prefix="/users",
    tags=["users"],
)

app.include_router(
    fastapi_users.get_reset_password_router(),
    prefix="/auth",
    tags=["auth"],
)


from sqlalchemy import select
from app.db import AsyncSessionLocal


app.include_router(
    fastapi_users.get_register_router(UserRead, UserCreate),
    prefix="/auth",
    tags=["auth"],
)


@app.on_event("startup")
async def on_startup():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)



from fastapi import Depends, HTTPException
from app.users import get_user_manager
from fastapi_users.manager import BaseUserManager
from app.models import User
import uuid

ADMIN_SECRET = os.getenv("ADMIN_SECRET")


from app.schemas import UserCreate
from fastapi import Body

@app.post("/admin/create-user")
async def create_user_admin(
    email: str = Body(...),
    admin_secret: str = Body(...),
    user_manager=Depends(get_user_manager),
):
    if admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Not authorized")

    user_create = UserCreate(
        email=email,
        password="DefaultPass123",
        is_active=True,
        is_verified=True,
    )

    user = await user_manager.create(user_create)

    await user_manager.forgot_password(user)




    return {"status": "created", "email": user.email}

