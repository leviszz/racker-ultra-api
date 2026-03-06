from app.scan import scan_loop
import asyncio
import os
import uuid
from fastapi import FastAPI, Depends, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import date, timedelta
from sqlalchemy import text, select, func, cast, Date, desc

# Imports do seu projeto
from app.schemas import UserRead, UserCreate, UserUpdate
from app.scan import router as scan_router
from app.users import fastapi_users, auth_backend, get_user_manager
from app.models import User, UserClick # Adicionamos UserClick aqui
from app.db import engine, Base, AsyncSessionLocal

current_superuser = fastapi_users.current_user(active=True, superuser=True)





# 1. Instância do FastAPI
app = FastAPI(title="Racker Ultra PRO Turbo", version="17.0")

@app.post("/admin/make-superuser")
async def make_superuser(
    email: str = Body(...),
    admin_secret: str = Body(...),
    user_manager=Depends(get_user_manager),
):
    if admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    user = await user_manager.get_by_email(email)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Atualiza a flag de superuser
    await user_manager.update(UserUpdate(is_superuser=True), user)
    return {"status": "success", "message": f"{email} agora é um superuser!"}




@app.get("/admin/dashboard-stats")
async def get_dashboard_stats(periodo: str = "hoje", user: User = Depends(current_superuser)):
    async with AsyncSessionLocal() as db:
        hoje = date.today()
        
        # 1. Lógica do Filtro de Tempo
        if periodo == "semana":
            data_limite = hoje - timedelta(days=7)
        elif periodo == "mes":
            data_limite = hoje - timedelta(days=30)
        elif periodo == "ano":
            data_limite = hoje - timedelta(days=365)
        else:
            data_limite = hoje # "hoje" (apenas o dia atual)

        # Condição de filtro para usar nas consultas
        filtro_data = cast(UserClick.timestamp, Date) >= data_limite

        # 2. Total de Requisições
        # 2. Total de Requisições (Apenas cliques no botão verde de Scan)
        q_total = select(func.count(UserClick.id)).where(filtro_data).where(UserClick.coin == "GERAL")
        res_total = await db.execute(q_total)
        total_cliques = res_total.scalar() or 0

        # 3. Usuários Ativos (Que fizeram Scans)
        q_unicos = select(func.count(func.distinct(UserClick.user_id))).where(filtro_data).where(UserClick.coin == "GERAL")
        res_unicos = await db.execute(q_unicos)
        total_unicos = res_unicos.scalar() or 0

        # 4. Ranking de Usuários (Baseado na quantidade de Scans)
        q_ranking = (
            select(User.email, func.count(UserClick.id).label("contagem"))
            .join(UserClick, User.id == UserClick.user_id)
            .where(filtro_data)
            .where(UserClick.coin == "GERAL") # <--- Garante que só os scans pontuam no ranking
            .group_by(User.email)
            .order_by(desc("contagem"))
            .limit(10)
        )
        res_ranking = await db.execute(q_ranking)
        ranking = [{"email": r[0], "cliques": r[1]} for r in res_ranking.all()]

        # 5. NOVO: Moeda Mais Pesquisada (Mantém como fizemos antes, ignorando o "GERAL")
        q_moeda = (
            select(UserClick.coin, func.count(UserClick.id).label("qtd"))
            .where(filtro_data)
            .where(UserClick.coin.isnot(None))
            .where(UserClick.coin != "GERAL") 
            .group_by(UserClick.coin)
            .order_by(desc("qtd"))
            .limit(1)
        )
        res_moeda = await db.execute(q_moeda)
        moeda_top_row = res_moeda.first()
        moeda_top = moeda_top_row[0] if moeda_top_row else "N/A"

        # 6. Cálculo da Média
        media = round(total_cliques / total_unicos, 2) if total_unicos > 0 else 0

        return {
            "periodo_atual": periodo,
            "cards": {
                "total_requisicoes": total_cliques,
                "usuarios_ativos": total_unicos,
                "media_uso_por_usuario": media,
                "moeda_top": moeda_top # Nova métrica enviada para o frontend
            },
            "ranking_usuarios": ranking
        }

# Rota antiga de visualização simples (mantida para conferência)
@app.get("/admin/ver-cliques")
async def ver_cliques(user: User = Depends(current_superuser)): # Dependência adicionada aqui
    async with AsyncSessionLocal() as session:
        query = text("SELECT id, user_id, timestamp FROM user_clicks ORDER BY timestamp DESC LIMIT 10")
        result = await session.execute(query)
        cliques = result.all()
        return {
            "mensagem": "Lista das últimas requisições registradas",
            "total_exibido": len(cliques),
            "dados": [dict(row._mapping) for row in cliques]
        }

# --- CONFIGURAÇÕES DE MIDDLEWARE ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Ajuste conforme sua necessidade de segurança
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- INCLUSÃO DE ROTAS ---
app.include_router(scan_router)
app.include_router(fastapi_users.get_auth_router(auth_backend), prefix="/auth/jwt", tags=["auth"])
app.include_router(fastapi_users.get_users_router(UserRead, UserUpdate), prefix="/users", tags=["users"])
app.include_router(fastapi_users.get_reset_password_router(), prefix="/auth", tags=["auth"])
app.include_router(fastapi_users.get_register_router(UserRead, UserCreate), prefix="/auth", tags=["auth"])

# --- EVENTOS DE INICIALIZAÇÃO ---
@app.on_event("startup")
async def on_startup():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    asyncio.create_task(scan_loop())

# --- ADMIN ROUTES ---
ADMIN_SECRET = os.getenv("ADMIN_SECRET")

@app.post("/admin/create-user")
async def create_user_admin(
    email: str = Body(...),
    admin_secret: str = Body(...),
    user_manager=Depends(get_user_manager),
):
    if admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Not authorized")
    user_create = UserCreate(email=email, password="DefaultPass123", is_active=True, is_verified=True)
    user = await user_manager.create(user_create)
    await user_manager.forgot_password(user)
    return {"status": "created", "email": user.email}