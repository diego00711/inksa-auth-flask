# src/utils/asaas.py
"""
Cliente HTTP do Asaas (provider de pagamento alternativo ao Mercado Pago).

Variaveis de ambiente:
  PAYMENT_PROVIDER      'mercadopago' (default) | 'asaas' — quem processa os
                        pagamentos ONLINE (criar_preferencia). Dinheiro nao muda.
  ASAAS_API_KEY         chave de API (comeca com $aact_...)
  ASAAS_ENV             'sandbox' (default) | 'production'
  ASAAS_BASE_URL        (opcional) sobrescreve a URL base — util se o host do
                        sandbox mudar sem precisarmos de deploy de codigo
  ASAAS_WEBHOOK_TOKEN   token que NOS definimos ao cadastrar o webhook no painel
                        Asaas; chega no header 'asaas-access-token' de cada
                        notificacao e e validado fail-closed no webhook.

Fluxo online (billingType=UNDEFINED): criamos a cobranca e devolvemos a
invoiceUrl — pagina hospedada do Asaas onde o cliente escolhe PIX ou cartao.
E o equivalente direto do init_point do MP, entao o app cliente continua
apenas redirecionando pro checkout_link.
"""
import logging
import os
from datetime import date

import requests

logger = logging.getLogger(__name__)

_TIMEOUT = 20


def is_configured() -> bool:
    return bool(os.environ.get("ASAAS_API_KEY"))


def _base_url() -> str:
    override = (os.environ.get("ASAAS_BASE_URL") or "").strip().rstrip("/")
    if override:
        return override
    env = (os.environ.get("ASAAS_ENV") or "sandbox").strip().lower()
    if env == "production":
        return "https://api.asaas.com/v3"
    return "https://api-sandbox.asaas.com/v3"


def _headers() -> dict:
    return {
        "access_token": os.environ.get("ASAAS_API_KEY", ""),
        "Content-Type": "application/json",
        "User-Agent": "InksaDelivery/1.0",
    }


def _request(method: str, path: str, json_body: dict | None = None, params: dict | None = None):
    """(ok: bool, data: dict). Nunca lanca — erros viram (False, {...})."""
    url = f"{_base_url()}{path}"
    try:
        resp = requests.request(method, url, headers=_headers(), json=json_body, params=params, timeout=_TIMEOUT)
        try:
            data = resp.json() if resp.text else {}
        except ValueError:
            data = {"raw": resp.text[:500]}
        if resp.status_code >= 400:
            logger.warning("Asaas %s %s -> %s: %s", method, path, resp.status_code, data)
            return False, data
        return True, data
    except requests.RequestException as e:
        logger.error("Asaas %s %s falhou: %s", method, path, e)
        return False, {"errors": [{"description": str(e)}]}


def _error_message(data: dict) -> str:
    try:
        errs = data.get("errors") or []
        if errs:
            return "; ".join(e.get("description", "") for e in errs if isinstance(e, dict)) or "erro Asaas"
    except Exception:
        pass
    return "erro Asaas"


def get_or_create_customer(name: str, cpf: str, email: str | None = None,
                           external_reference: str | None = None):
    """(ok, customer_id_ou_msg). Procura por CPF; cria se nao existir."""
    cpf_digits = "".join(ch for ch in (cpf or "") if ch.isdigit())
    if len(cpf_digits) != 11:
        return False, "CPF do cliente ausente ou inválido. Complete seu CPF no perfil para pagar online."

    ok, data = _request("GET", "/customers", params={"cpfCnpj": cpf_digits, "limit": 1})
    if ok and data.get("data"):
        return True, data["data"][0]["id"]

    body = {"name": (name or "Cliente Inksa").strip()[:100], "cpfCnpj": cpf_digits}
    if email:
        body["email"] = email
    if external_reference:
        body["externalReference"] = str(external_reference)
    ok, data = _request("POST", "/customers", json_body=body)
    if not ok:
        return False, _error_message(data)
    return True, data["id"]


def create_checkout_payment(customer_id: str, value: float, external_reference: str,
                            description: str = "Pedido Inksa Delivery",
                            billing_type: str = "UNDEFINED",
                            success_url: str | None = None):
    """Cria cobranca com pagina hospedada (invoiceUrl).

    billing_type: 'PIX' (fatura mostra so o QR), 'CREDIT_CARD' (so cartao) ou
    'UNDEFINED' (cliente escolhe — inclui boleto). Se o tipo especifico for
    recusado (ex.: PIX antes da conta aprovada/chave criada), cai de volta
    pro UNDEFINED em vez de quebrar o checkout.

    success_url: pra onde a pagina do Asaas redireciona o cliente apos pagar
    (autoRedirect) — devolve o usuario pro app, como as back_urls do MP.
    (ok, {payment_id, invoice_url, status} | msg_de_erro).
    """
    body = {
        "customer": customer_id,
        "billingType": (billing_type or "UNDEFINED").upper(),
        "value": round(float(value), 2),
        "dueDate": date.today().isoformat(),
        "description": description[:200],
        "externalReference": str(external_reference),
        # Desliga as notificações do Asaas (SMS/WhatsApp custam a "taxa de
        # mensageria") — o PRÓPRIO app já avisa o cliente (push + tela de
        # acompanhamento), então essas mensagens só gerariam custo.
        "notificationDisabled": True,
    }
    if success_url:
        body["callback"] = {"successUrl": success_url, "autoRedirect": True}

    ok, data = _request("POST", "/payments", json_body=body)

    # Se o tipo pedido for recusado (ex.: PIX antes da conta aprovada/chave criada),
    # cai pro CARTÃO — NUNCA pro UNDEFINED, que mostraria boleto (sem sentido em
    # delivery). Cartão é o único método online que funciona durante a análise.
    if not ok and body["billingType"] != "CREDIT_CARD":
        logger.warning("Asaas recusou billingType=%s (%s) — caindo pra CREDIT_CARD",
                       body["billingType"], _error_message(data))
        body["billingType"] = "CREDIT_CARD"
        ok, data = _request("POST", "/payments", json_body=body)

    if not ok:
        return False, _error_message(data)
    return True, {
        "payment_id": data.get("id"),
        "invoice_url": data.get("invoiceUrl"),
        "status": data.get("status"),
    }


def create_transfer(value: float, pix_key: str, pix_key_type: str,
                    description: str = "Repasse Inksa Delivery"):
    """Transferência PIX de SAÍDA (repasse pro parceiro).

    ⚠️ Só funciona com a conta Asaas APROVADA (saque/transferência fica
    bloqueado durante a análise) e com SALDO disponível na conta.

    pix_key_type: 'CPF' | 'CNPJ' | 'EMAIL' | 'PHONE' | 'EVP' (chave aleatória).
    (ok, {transfer_id, status} | msg_de_erro).
    """
    body = {
        "value": round(float(value), 2),
        "operationType": "PIX",
        "pixAddressKey": (pix_key or "").strip(),
        "pixAddressKeyType": (pix_key_type or "").strip().upper(),
        "description": (description or "Repasse Inksa Delivery")[:200],
    }
    ok, data = _request("POST", "/transfers", json_body=body)
    if not ok:
        return False, _error_message(data)
    status = (data.get("status") or "").upper()
    # FAILED/CANCELLED = o Asaas aceitou a chamada mas recusou a transferência
    if status in ("FAILED", "CANCELLED"):
        return False, _error_message(data) or f"transferência {status.lower()}"
    return True, {"transfer_id": data.get("id"), "status": status}


def get_payment(payment_id: str):
    return _request("GET", f"/payments/{payment_id}")


def refund_payment(payment_id: str):
    """(ok: bool, detail: str). Estorno integral."""
    ok, data = _request("POST", f"/payments/{payment_id}/refund", json_body={})
    if ok:
        return True, "refunded"
    return False, _error_message(data)


def verify_webhook_token(req) -> bool:
    """Valida o header 'asaas-access-token' contra ASAAS_WEBHOOK_TOKEN (fail-closed)."""
    expected = os.environ.get("ASAAS_WEBHOOK_TOKEN")
    if not expected:
        logger.critical("ASAAS_WEBHOOK_TOKEN não configurado — rejeitando webhook (fail-closed).")
        return False
    received = req.headers.get("asaas-access-token", "")
    import hmac
    return hmac.compare_digest(received, expected)
