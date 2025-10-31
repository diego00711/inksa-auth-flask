# src/logic/payout_processor.py
import logging
from datetime import datetime, timedelta, timezone
import psycopg2.extras
import uuid as uuidlib

logger = logging.getLogger(__name__)

def _period_bounds(cycle_type: str):
    """Retorna (period_start, period_end) UTC para o ciclo informado.
       Estratégia simples e estável:
         - weekly: últimos 7 dias até agora
         - bi-weekly: últimos 14 dias
         - monthly: do 1º dia do mês corrente até agora
    """
    now = datetime.now(timezone.utc)
    if cycle_type == "monthly":
        first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return (first, now)
    days = 7 if cycle_type == "weekly" else 14
    start = (now - timedelta(days=days)).replace(microsecond=0)
    return (start, now)

def process_payouts(conn, partner_type: str, cycle_type: str):
    """Gera payouts para 'restaurant' ou 'delivery' no período definido.

    Pré-requisitos no BD:
      - orders.status = 'delivered'
      - orders.status_pagamento = 'approved' (ou como você grava no webhook do MP)
      - orders.valor_repassado_restaurante / valor_repassado_entregador
      - orders.restaurant_payout_id / delivery_payout_id (para marcação)
      - tabela payouts com colunas:
          (id, partner_id, partner_type, amount, period_start, period_end, order_ids_included, status, created_at)
    """
    period_start, period_end = _period_bounds(cycle_type)

    # Mapas fixos (evita concatenar nome de coluna e errar)
    if partner_type == "delivery":
        amount_column = "valor_repassado_entregador"
        payout_id_column = "delivery_payout_id"
        partner_id_column = "delivery_id"
        profile_table = "delivery_profiles"
    else:
        amount_column = "valor_repassado_restaurante"
        payout_id_column = "restaurant_payout_id"
        partner_id_column = "restaurant_id"
        profile_table = "restaurant_profiles"

    created = []

    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        # bloqueia parceiros elegíveis para evitar corrida (se rodar concorrente)
        # seleciona todos parceiros que possuem pedidos elegíveis no período e ainda sem payout
        cur.execute(f"""
            SELECT DISTINCT o.{partner_id_column} AS partner_id
            FROM orders o
            WHERE
              o.{partner_id_column} IS NOT NULL
              AND o.status = 'delivered'
              AND o.status_pagamento = 'approved'
              AND o.{amount_column} IS NOT NULL
              AND o.{amount_column} > 0
              AND o.{payout_id_column} IS NULL
              AND o.updated_at >= %s AND o.updated_at <= %s
            FOR UPDATE SKIP LOCKED
        """, (period_start, period_end))

        partners = [row["partner_id"] for row in cur.fetchall()]
        logger.info("Parceiros elegíveis: %s", partners)

        for partner_id in partners:
            # busca pedidos do parceiro no período que ainda não estão em payout
            cur.execute(f"""
                SELECT id, {amount_column} AS repasse
                FROM orders
                WHERE
                  {partner_id_column} = %s
                  AND status = 'delivered'
                  AND status_pagamento = 'approved'
                  AND {amount_column} IS NOT NULL
                  AND {amount_column} > 0
                  AND {payout_id_column} IS NULL
                  AND updated_at >= %s AND updated_at <= %s
                ORDER BY updated_at ASC
            """, (partner_id, period_start, period_end))

            rows = cur.fetchall()
            if not rows:
                continue

            total = sum(float(r["repasse"] or 0) for r in rows)
            order_ids = [r["id"] for r in rows]

            # cria payout
            payout_id = uuidlib.uuid4()
            cur.execute("""
                INSERT INTO payouts (id, partner_id, partner_type, amount, period_start, period_end, order_ids_included, status, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending', NOW())
                RETURNING id, partner_id, partner_type, amount, period_start, period_end, status
            """, (str(payout_id), str(partner_id), partner_type, total, period_start, period_end, order_ids))

            payout_row = dict(cur.fetchone())

            # marca pedidos com o id do payout
            cur.execute(f"""
                UPDATE orders
                SET {payout_id_column} = %s
                WHERE id = ANY(%s)
            """, (str(payout_id), order_ids))

            created.append({
                "payout_id": str(payout_row["id"]),
                "partner_type": payout_row["partner_type"],
                "partner_id": str(payout_row["partner_id"]),
                "amount": float(payout_row["amount"]),
                "period_start": payout_row["period_start"].isoformat(),
                "period_end": payout_row["period_end"].isoformat(),
                "status": payout_row["status"],
                "orders_count": len(order_ids)
            })

    logger.info("Payouts gerados: %d", len(created))
    return created
