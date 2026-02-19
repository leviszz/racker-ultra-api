import os
import resend

# Puxa a chave da API que você configurou no Render/Local
resend.api_key = os.getenv("RESEND_API_KEY")

async def send_reset_email(email: str, token: str):
    """
    Função para enviar o email de redefinição de senha usando Resend.
    """
    
    # IMPORTANTE: Enquanto você não tiver um domínio próprio comprado, 
    # o Resend exige que o remetente seja exatamente este abaixo:
    sender = "suporte@vbossracker.com"
    
    # URL do seu frontend (Atualize para a URL da Vercel quando for testar em produção)
    reset_link = f"https://www.vbossracker.com/reset-password?token={token}"

    params = {
        "from": sender,
        # ATENÇÃO: Na conta gratuita sem domínio, você SÓ PODE enviar emails para 
        # o seu próprio email (o mesmo que você usou para criar a conta no Resend).
        "to": [email], 
        "subject": "Redefinição de Senha - Racker Ultra",
        "html": f"""
        <h2>Recuperação de Conta</h2>
        <p>Você solicitou a redefinição da sua senha.</p>
        <p>Clique no link abaixo para criar uma nova senha:</p>
        <a href='{reset_link}'>Redefinir Minha Senha</a>
        <br><br>
        <p>Se você não solicitou isso, apenas ignore este email.</p>
        """
    }

    try:
        # Envia o email
        email_response = resend.Emails.send(params)
        print("Email enviado com sucesso:", email_response)
        return True
    except Exception as e:
        print("Erro ao enviar email:", e)
        return False