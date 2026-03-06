import asyncio
from sqlalchemy import text
from app.db import AsyncSessionLocal

async def atualizar():
    async with AsyncSessionLocal() as db:
        try:
            # Comando SQL direto para criar a coluna no PostgreSQL
            await db.execute(text("ALTER TABLE user_clicks ADD COLUMN coin VARCHAR;"))
            await db.commit()
            print("✅ Sucesso: Coluna 'coin' adicionada no banco do Render!")
        except Exception as e:
            print(f"❌ Erro: {e}")

if __name__ == "__main__":
    asyncio.run(atualizar())