import os
import aiosmtplib
from email.message import EmailMessage

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_USER = os.getenv("EMAIL_USER")
SMTP_PASS = os.getenv("EMAIL_PASS")


async def send_reset_email(to_email: str, token: str):
    reset_link = f"http://127.0.0.1:5500/reset.html?token={token}"

    message = EmailMessage()
    message["From"] = SMTP_USER
    message["To"] = to_email
    message["Subject"] = "Defina sua senha"

    message.set_content(
        f"""
Olá,

Você foi cadastrado no V-BOSS Racker.

Clique no link abaixo para definir sua senha:

{reset_link}

Se você não solicitou isso, ignore este email.
"""
    )

    await aiosmtplib.send(
        message,
        hostname=SMTP_HOST,
        port=SMTP_PORT,
        start_tls=True,
        username=SMTP_USER,
        password=SMTP_PASS,
    )
