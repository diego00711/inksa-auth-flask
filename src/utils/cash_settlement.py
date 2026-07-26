# src/utils/cash_settlement.py
"""Liquidação financeira de pedidos em DINHEIRO — fonte única e idempotente.

O entregador recolhe o valor do pedido em espécie. A partir daí:
  - o repasse ao restaurante e a comissão da plataforma existem (mesmo modelo
    do online), mas o dinheiro está com o entregador;
  - o entregador fica DEVENDO à plataforma tudo que recolheu menos a parte do
    frete que é dele (cash_debt) — isso é abatido depois nos repasses online.

Antes essa liquidação só acontecia quando o entregador clicava "confirmar
recebimento" no app. Se ele finalizasse por outra tela, ou clicasse "não agora",
o pedido ficava com o financeiro NULL e a dívida não era registrada — a
plataforma perdia o registro do dinheiro. Agora a liquidação roda no próprio
fechamento da entrega (complete_order), e este helper garante que rode UMA vez
só (idempotente pelo cash_payment_records).
"""

from ..utils.platform_settings import (
    calculate_platform_commission,
    calculate_courier_payout,
)


def compute_cash_breakdown(total_amount, delivery_fee, restaurant_id, existing_commission=None):
    """Só calcula os números (não toca no banco). Mesmas fórmulas do online."""
    total_amount = float(total_amount or 0)
    delivery_fee = float(delivery_fee or 0)
    commission = float(existing_commission or 0)
    if not commission:
        # Mesma fonte/taxa dos pedidos online (Configurações > Taxas do admin).
        commission = float(calculate_platform_commission(total_amount - delivery_fee, restaurant_id))

    # Frete do entregador = frete integral menos a taxa de administração do
    # frete (margem_frete da plataforma). O resto do dinheiro vira dívida dele.
    courier_freight = float(calculate_courier_payout(None, delivery_fee=delivery_fee))
    freight_admin = round(delivery_fee - courier_freight, 2)
    restaurant_share = round(total_amount - delivery_fee - commission, 2)
    cash_debt = round(total_amount - courier_freight, 2)

    return {
        "total_amount": round(total_amount, 2),
        "delivery_fee": round(delivery_fee, 2),
        "commission": round(commission, 2),
        "restaurant_share": restaurant_share,
        "courier_freight": round(courier_freight, 2),
        "freight_admin": freight_admin,
        "cash_debt": cash_debt,
    }


def settle_cash_order(cur, order_id, delivery_id, restaurant_id,
                      total_amount, delivery_fee, existing_commission=None):
    """Liquida o pedido em dinheiro de forma IDEMPOTENTE usando o cursor `cur`
    (não faz commit — quem chama controla a transação).

    - Grava o split financeiro no próprio pedido (para os relatórios que leem de
      orders): comissao_plataforma, valor_repassado_restaurante,
      valor_repassado_entregador, margem_frete.
    - Registra cash_payment_records e incrementa delivery_profiles.cash_debt —
      MAS só se ainda não houver registro para este pedido (evita duplicar a
      dívida se a confirmação manual também rodar).

    Retorna (breakdown, was_new): breakdown é o resumo pro app mostrar; was_new
    indica se a dívida foi registrada agora (False = já estava liquidado).
    """
    b = compute_cash_breakdown(total_amount, delivery_fee, restaurant_id, existing_commission)

    # Idempotência: já liquidado? Não duplica a dívida nem o registro.
    cur.execute("SELECT id FROM cash_payment_records WHERE order_id = %s", (str(order_id),))
    if cur.fetchone():
        return b, False

    if delivery_id is None:
        # Sem entregador não há a quem atribuir a dívida — não liquida.
        return b, False

    cur.execute(
        """
        INSERT INTO cash_payment_records
            (order_id, delivery_id, restaurant_id, total_amount, delivery_fee,
             platform_commission, restaurant_share)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (str(order_id), str(delivery_id), str(restaurant_id),
         b["total_amount"], b["delivery_fee"], b["commission"], b["restaurant_share"]),
    )

    cur.execute(
        """
        UPDATE delivery_profiles
           SET cash_debt = COALESCE(cash_debt, 0) + %s,
               total_cash_received = COALESCE(total_cash_received, 0) + %s,
               updated_at = NOW()
         WHERE id = %s
        """,
        (b["cash_debt"], b["total_amount"], str(delivery_id)),
    )

    cur.execute(
        """
        UPDATE orders
           SET comissao_plataforma = %s,
               valor_repassado_restaurante = %s,
               valor_repassado_entregador = %s,
               margem_frete = %s,
               updated_at = NOW()
         WHERE id = %s
        """,
        (b["commission"], b["restaurant_share"], b["courier_freight"], b["freight_admin"], str(order_id)),
    )

    return b, True
