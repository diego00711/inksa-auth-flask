# src/providers/asaas_payouts.py
"""Provider de PIX-out (repasse) via API de Transferências do Asaas.

Semi-automático: o scheduler gera os registros de payout; o admin clica
"Pagar via Asaas" e este provider dispara o PIX de saída de verdade.

⚠️ Só envia dinheiro com a conta Asaas APROVADA (transferência/saque fica
bloqueado durante a análise) e com SALDO disponível. Enquanto a conta não
aprova, o Asaas recusa a transferência e o fluxo manual assistido continua
como rede de segurança.
"""
import logging
import re
from typing import Optional

from ..utils import asaas as asaas_client
from .payout_provider import PayoutProvider, PayoutResult

logger = logging.getLogger(__name__)

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


def _only_digits(s: str) -> str:
    return "".join(ch for ch in (s or "") if ch.isdigit())


def infer_pix_key_type(pix_key: str) -> str:
    """Melhor esforço pra descobrir o tipo da chave que o Asaas exige.

    Ambiguidade real: 11 dígitos pode ser CPF ou celular — o default aqui é
    CPF (chave mais comum de parceiro). Pra celular, cadastre a chave com +55
    ou informe o tipo explicitamente na hora de pagar. Quando a inferência
    erra, o Asaas recusa e o admin cai no repasse manual assistido.
    """
    key = (pix_key or "").strip()
    if not key:
        return "CPF"
    if "@" in key:
        return "EMAIL"
    if _UUID_RE.match(key):
        return "EVP"
    if key.startswith("+"):
        return "PHONE"
    digits = _only_digits(key)
    if len(digits) == 14:
        return "CNPJ"
    if len(digits) == 13 and digits.startswith("55"):
        return "PHONE"
    if len(digits) == 11:
        return "CPF"          # ambíguo com celular; CPF é o default
    if len(digits) == 10:
        return "PHONE"
    return "EVP"              # sem formato reconhecível → chave aleatória


class AsaasPayoutProvider(PayoutProvider):
    def transfer_pix(self, *, amount_cents: int, pix_key: str, description: str,
                     pix_key_type: Optional[str] = None,
                     external_reference: Optional[str] = None) -> PayoutResult:
        if not asaas_client.is_configured():
            return {"ok": False, "txid": None, "raw": {"error": "ASAAS_API_KEY ausente"}}

        key = (pix_key or "").strip()
        if not key:
            return {"ok": False, "txid": None, "raw": {"error": "chave PIX vazia"}}

        key_type = (pix_key_type or infer_pix_key_type(key)).upper()
        value = round(amount_cents / 100.0, 2)

        ok, detail = asaas_client.create_transfer(
            value=value,
            pix_key=key,
            pix_key_type=key_type,
            description=description or "Repasse Inksa Delivery",
            external_reference=external_reference,
        )
        if not ok:
            logger.error("Asaas transfer falhou (%s): %s", key_type, detail)
            return {"ok": False, "txid": None, "raw": {"error": detail}}

        return {"ok": True, "txid": detail.get("transfer_id"), "raw": detail}
