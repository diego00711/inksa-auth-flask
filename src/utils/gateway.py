# src/utils/gateway.py
"""Helpers agnósticos de gateway de pagamento.

O pedido carrega `payment_provider` ('mercadopago' | 'asaas') e
`id_transacao_mp` (id da transação no gateway — o nome da coluna é legado do
MP, mas guarda o id de qualquer provider). Todo estorno passa por aqui para
não espalhar if/else de provider pelos 4 pontos que reembolsam.
"""
import logging
import os

logger = logging.getLogger(__name__)


def payment_provider() -> str:
    """Provider ativo para NOVOS pagamentos online (não afeta pedidos antigos)."""
    return (os.environ.get("PAYMENT_PROVIDER") or "mercadopago").strip().lower()


def refund_order_payment(order: dict, mp_sdk) -> tuple[bool, str]:
    """Estorna o pagamento de um pedido no gateway em que ele foi pago.

    Usa o payment_provider GRAVADO no pedido (não o env atual) — assim pedidos
    do MP continuam estornáveis mesmo depois de migrarmos para o Asaas.
    Retorna (ok, detalhe).
    """
    tx_id = order.get("id_transacao_mp")
    if not tx_id:
        return False, "pedido sem id de transação no gateway"

    provider = (order.get("payment_provider") or "mercadopago").strip().lower()

    if provider == "asaas":
        from . import asaas
        return asaas.refund_payment(str(tx_id))

    # Mercado Pago (default/legado)
    if not mp_sdk:
        return False, "SDK do Mercado Pago indisponível"
    try:
        res = mp_sdk.refund().create(tx_id)
        code = res.get("status", 200) if isinstance(res, dict) else 200
        if code < 400:
            return True, "refunded"
        return False, str(res.get("response") if isinstance(res, dict) else res)
    except Exception as e:
        logger.exception("Falha ao estornar no MP")
        return False, str(e)
