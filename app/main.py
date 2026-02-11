from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.scan import router as scan_router

app = FastAPI(title="Racker Ultra PRO Turbo", version="17.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(scan_router)
