# src/providers/mp_payouts.py
import os
import logging
from typing import Optional
import mercadopago

from .payout_provider import PayoutProvider, PayoutResult, MockPayoutProvider

logger = logging.getLogger(__name__)

MODE = os.environ.get("PAYOUT_PROVIDER", "mock").lower()   # mock | mercadopago
MP_ACCESS_TOKEN = os.environ.get("MERCADO_PAGO_ACCESS_TOKEN", "")

class MercadoPagoPayoutProvider(PayoutProvider):
    """
    ATENÇÃO:
    - O envio automático (transfer/payout) exige escopos específicos na conta.
    - Se sua conta não possuir a feature liberada, use 'mock' até habilitar.
    """

    def __init__(self):
        if not MP_ACCESS_TOKEN:
            raise RuntimeError("MERCADO_PAGO_ACCESS_TOKEN ausente para provider Mercado Pago.")
        self.sdk = mercadopago.SDK(MP_ACCESS_TOKEN)

    def transfer_pix(self, *, amount_cents: int, pix_key: str, description: str) -> PayoutResult:
        # IMPORTANTE: este trecho depende dos endpoints habilitados na sua conta.
        # Há contas que usam 'transfers/payouts' privados. Se não tiver, ficará indisponível.
        #
        # Exemplo ilustrativo (pseudocódigo). Ajuste conforme seus endpoints habilitados:
        #
        # payload = {
        #   "amount": amount_cents/100,
        #   "payment_method": "pix",
        #   "pix": {"key": pix_key, "description": description[:60]}
        # }
        # resp = self.sdk.<algum_recurso>.create(payload)
        # if resp["status"] in (200, 201):
        #     txid = resp["response"].get("id") or resp["response"].get("txid")
        #     return {"ok": True, "txid": txid, "raw": resp}
        #
        # logger.error(f"MP payout failure: {resp}")
        # return {"ok": False, "txid": None, "raw": resp}
        raise NotImplementedError(
            "Sua conta Mercado Pago precisa de API de transfer/payout habilitada. "
            "Mantenha PAYOUT_PROVIDER=mock por enquanto."
        )


def get_payout_provider() -> PayoutProvider:
    if MODE == "mercadopago":
        logger.info("Usando provider Mercado Pago para repasses.")
        return MercadoPagoPayoutProvider()
    logger.info("Usando provider MOCK para repasses.")
    return MockPayoutProvider()
