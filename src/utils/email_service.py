"""
Envio de email via SMTP (Zoho Mail, Workspace, ou qualquer SMTP).

Variaveis de ambiente esperadas no Render:
  SMTP_HOST       (ex: smtp.zoho.com)
  SMTP_PORT       (ex: 587)
  SMTP_USERNAME   (ex: no-reply@inksadelivery.com.br)
  SMTP_PASSWORD   (App Password gerado no provedor)
  SMTP_FROM       (ex: no-reply@inksadelivery.com.br)
  SMTP_FROM_NAME  (ex: Inksa Delivery)

Se SMTP_HOST nao estiver setado, send_email() retorna False sem erro -
o backend continua funcionando, so nao envia email.
"""
import os
import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr

logger = logging.getLogger(__name__)


def is_configured() -> bool:
    return bool(os.environ.get("SMTP_HOST") and os.environ.get("SMTP_USERNAME") and os.environ.get("SMTP_PASSWORD"))


def send_email(to: str, subject: str, html: str, text: str | None = None, reply_to: str | None = None) -> bool:
    if not is_configured():
        logger.warning("SMTP nao configurado - email para %s nao enviado", to)
        return False

    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    username = os.environ["SMTP_USERNAME"]
    password = os.environ["SMTP_PASSWORD"]
    from_addr = os.environ.get("SMTP_FROM", username)
    from_name = os.environ.get("SMTP_FROM_NAME", "Inksa")

    msg = MIMEMultipart("alternative")
    msg["From"] = formataddr((from_name, from_addr))
    msg["To"] = to
    msg["Subject"] = subject
    if reply_to:
        msg["Reply-To"] = reply_to

    if text:
        msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        with smtplib.SMTP(host, port, timeout=20) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(username, password)
            server.sendmail(from_addr, [to], msg.as_string())
        logger.info("Email enviado para %s (assunto: %s)", to, subject)
        return True
    except Exception:
        logger.exception("Falha ao enviar email para %s", to)
        return False


def render_simple(title: str, body_html: str, cta_text: str | None = None, cta_url: str | None = None) -> str:
    cta_html = ""
    if cta_text and cta_url:
        cta_html = (
            f'<p style="text-align:center;margin:32px 0">'
            f'<a href="{cta_url}" style="background:#FF6B35;color:#fff;padding:14px 28px;'
            f'border-radius:8px;text-decoration:none;font-weight:600">{cta_text}</a></p>'
        )
    return f"""<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="UTF-8"></head>
<body style="margin:0;font-family:'Segoe UI',Arial,sans-serif;background:#f5f5f5;padding:24px">
  <table role="presentation" cellpadding="0" cellspacing="0" style="max-width:560px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden">
    <tr><td style="background:linear-gradient(135deg,#FF6B35,#F7931E);padding:24px;text-align:center">
      <h1 style="margin:0;color:#fff;font-size:22px">Inksa Delivery</h1>
    </td></tr>
    <tr><td style="padding:32px">
      <h2 style="color:#1f2937;font-size:20px;margin:0 0 16px">{title}</h2>
      <div style="color:#4b5563;line-height:1.6;font-size:15px">{body_html}</div>
      {cta_html}
      <p style="color:#9ca3af;font-size:12px;margin-top:32px;text-align:center">
        Esta mensagem foi enviada por <strong>Inksa Delivery</strong>.<br>
        Duvidas? Responda este email ou escreva para suporte@inksadelivery.com.br
      </p>
    </td></tr>
  </table>
</body></html>"""
