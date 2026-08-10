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


def compute_cash_breakdown(total_amount, delivery_fee, restaurant_id, existing_commission=None,
                           desconto_parceiro=0.0):
    """Só calcula os números (não toca no banco). Mesmas fórmulas do online.

    desconto_parceiro = cupom da própria loja, que sai do repasse DELA (a
    comissão da plataforma fica intacta). Cupom da Inksa não entra aqui.
    """
    total_amount = float(total_amount or 0)
    delivery_fee = float(delivery_fee or 0)
    desconto_parceiro = float(desconto_parceiro or 0)
    commission = float(existing_commission or 0)
    if not commission:
        # Mesma fonte/taxa dos pedidos online (Configurações > Taxas do admin).
        commission = float(calculate_platform_commission(total_amount - delivery_fee, restaurant_id))

    # Frete do entregador = frete integral menos a taxa de administração do
    # frete (margem_frete da plataforma). O resto do dinheiro vira dívida dele.
    courier_freight = float(calculate_courier_payout(None, delivery_fee=delivery_fee))
    freight_admin = round(delivery_fee - courier_freight, 2)
    restaurant_share = round(total_amount - delivery_fee - commission - desconto_parceiro, 2)
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
                      total_amount, delivery_fee, existing_commission=None,
                      desconto_parceiro=0.0):
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
    b = compute_cash_breakdown(total_amount, delivery_fee, restaurant_id, existing_commission,
                               desconto_parceiro)

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


def settle_cash_own_delivery(cur, order_id, restaurant_id, total_amount, delivery_fee,
                             existing_commission=None, desconto_parceiro=0.0):
    """DINHEIRO + ENTREGA PRÓPRIA: o fluxo invertido.

    Aqui quem recolhe o dinheiro é o motoboy da própria loja — não existe
    entregador Inksa. A loja fica com TUDO (itens + o frete dela), e é ela que
    passa a dever a comissão à plataforma.

    Antes este caso não gravava nada: `settle_cash_order` era chamado só quando
    havia delivery_id, então o pedido ficava sem comissão e sem repasse. A Inksa
    perdia a comissão e o pedido nem aparecia no financeiro do parceiro (a query
    do saldo exige valor_repassado_restaurante NOT NULL).

    Idempotente pelo cash_payment_records (delivery_id fica NULL).
    Retorna (breakdown, was_new).
    """
    total_amount = float(total_amount or 0)
    delivery_fee = float(delivery_fee or 0)
    desconto_parceiro = float(desconto_parceiro or 0)

    commission = float(existing_commission or 0)
    if not commission:
        # Comissão sobre os ITENS — o frete da entrega própria é da loja, a
        # Inksa não entra nele.
        commission = float(calculate_platform_commission(total_amount - delivery_fee, restaurant_id))

    # Cupom da própria loja: ela já deu o desconto no caixa, então a dívida de
    # comissão não muda — mas o valor recolhido em espécie, sim.
    b = {
        "total_amount": round(total_amount, 2),
        "delivery_fee": round(delivery_fee, 2),
        "commission": round(commission, 2),
        # A Inksa não deve nada à loja: o dinheiro já está com ela.
        "restaurant_share": 0.0,
        "courier_freight": 0.0,
        "freight_admin": 0.0,
        # Não há entregador Inksa, então dívida de entregador é zero. A chave
        # PRECISA existir: quem monta a resposta do /complete lê cash_debt, e
        # sem ela dava KeyError DEPOIS do commit — o pedido fechava certinho e
        # o app mostrava "Erro interno do servidor".
        "cash_debt": 0.0,
        # ...é a LOJA que deve a comissão.
        "commission_debt": round(commission, 2),
    }

    cur.execute("SELECT id FROM cash_payment_records WHERE order_id = %s", (str(order_id),))
    if cur.fetchone():
        return b, False

    cur.execute(
        """
        INSERT INTO cash_payment_records
            (order_id, delivery_id, restaurant_id, total_amount, delivery_fee,
             platform_commission, restaurant_share)
        VALUES (%s, NULL, %s, %s, %s, %s, %s)
        """,
        (str(order_id), str(restaurant_id), b["total_amount"], b["delivery_fee"],
         b["commission"], b["restaurant_share"]),
    )

    cur.execute(
        """
        UPDATE restaurant_profiles
           SET commission_debt = COALESCE(commission_debt, 0) + %s,
               total_cash_received = COALESCE(total_cash_received, 0) + %s,
               updated_at = NOW()
         WHERE id = %s
        """,
        (b["commission_debt"], b["total_amount"], str(restaurant_id)),
    )

    cur.execute(
        """
        UPDATE orders
           SET comissao_plataforma = %s,
               valor_repassado_restaurante = 0,
               valor_repassado_entregador = 0,
               margem_frete = 0,
               updated_at = NOW()
         WHERE id = %s
        """,
        (b["commission"], str(order_id)),
    )

    return b, True
