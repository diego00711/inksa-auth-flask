# src/utils/whatsapp.py
"""
Cliente da WhatsApp Cloud API (Meta) — só o que o bot do Inksa precisa.

Variáveis de ambiente (Render):
  WHATSAPP_TOKEN            token de acesso permanente do app da Meta
  WHATSAPP_PHONE_NUMBER_ID  id do número na API (NÃO é o telefone)
  WHATSAPP_VERIFY_TOKEN     string que NÓS inventamos; a Meta devolve ela na
                            verificação do webhook (GET)
  WHATSAPP_APP_SECRET       segredo do app; assina cada POST no header
                            X-Hub-Signature-256

Sem WHATSAPP_TOKEN o bot fica DESLIGADO e o webhook responde 200 sem fazer
nada — assim dá pra ter o código no ar antes da conta da Meta existir, sem
risco de erro em produção.
"""
import hashlib
import hmac
import logging
import os

import requests

logger = logging.getLogger(__name__)

_TIMEOUT = 15
_API = "https://graph.facebook.com/v21.0"


def is_configured() -> bool:
    return bool(os.environ.get("WHATSAPP_TOKEN") and os.environ.get("WHATSAPP_PHONE_NUMBER_ID"))


def so_digitos(valor) -> str:
    return "".join(c for c in str(valor or "") if c.isdigit())


def chave_de_busca(telefone) -> str:
    """Últimos 8 dígitos — é o que dá pra comparar com segurança.

    O WhatsApp entrega '5549998292320' (com país). No cadastro o telefone vem
    como '49999679697', às vezes com máscara e espaço ('4999934-6405 '), e
    números antigos podem estar sem o 9 na frente. Comparar tudo dá falso
    negativo; os últimos 8 dígitos são estáveis nos três casos.
    """
    d = so_digitos(telefone)
    return d[-8:] if len(d) >= 8 else d


def verificar_assinatura(req) -> bool:
    """Confere o X-Hub-Signature-256 (fail-closed, igual ao webhook do Asaas).

    Sem o segredo configurado, RECUSA. Um webhook público sem verificação
    deixaria qualquer um mandar mensagem em nome dos nossos clientes.
    """
    segredo = os.environ.get("WHATSAPP_APP_SECRET")
    if not segredo:
        logger.critical("WHATSAPP_APP_SECRET ausente — recusando webhook (fail-closed).")
        return False
    recebida = req.headers.get("X-Hub-Signature-256", "")
    if not recebida.startswith("sha256="):
        return False
    esperada = "sha256=" + hmac.new(
        segredo.encode("utf-8"), req.get_data(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(recebida, esperada)


def enviar_texto(para: str, texto: str) -> bool:
    """Manda uma mensagem de texto. Nunca lança — erro vira log e False."""
    if not is_configured():
        logger.info("WhatsApp não configurado — mensagem não enviada.")
        return False
    numero = os.environ.get("WHATSAPP_PHONE_NUMBER_ID")
    try:
        r = requests.post(
            f"{_API}/{numero}/messages",
            headers={
                "Authorization": f"Bearer {os.environ.get('WHATSAPP_TOKEN')}",
                "Content-Type": "application/json",
            },
            json={
                "messaging_product": "whatsapp",
                "to": so_digitos(para),
                "type": "text",
                # preview_url False: o link do app não vira card gigante e a
                # mensagem fica legível no celular.
                "text": {"preview_url": False, "body": texto[:4000]},
            },
            timeout=_TIMEOUT,
        )
        if r.status_code >= 400:
            logger.warning("WhatsApp %s ao enviar para %s: %s", r.status_code, para, r.text[:300])
            return False
        return True
    except requests.RequestException as e:
        logger.error("Falha ao enviar WhatsApp para %s: %s", para, e)
        return False
