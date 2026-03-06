import uuid
from sqlalchemy import Boolean, Column, ForeignKey, DateTime, func, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from fastapi_users.db import SQLAlchemyBaseUserTableUUID
from app.db import Base

class User(SQLAlchemyBaseUserTableUUID, Base):
    __tablename__ = "users"

# === ADICIONE ESTAS LINHAS ABAIXO ===

class UserClick(Base):
    __tablename__ = "user_clicks"

    id = Column(Integer, primary_key=True, index=True)
    # Vincula o clique ao UUID do usuário que está na tabela "users"
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    # Registra a data e hora automaticamente
    timestamp = Column(DateTime(timezone=True), server_default=func.now())
    coin = Column(String, nullable=True)