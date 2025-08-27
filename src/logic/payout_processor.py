# src/logic/payout_processor.py

import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Tuple, Union
from ..utils.helpers import get_db_connection

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def get_date_range_for_cycle(cycle_type: str):
    """Calcula a data de início e fim para um ciclo de pagamento."""
    end_date = datetime.now()
    if cycle_type == 'weekly':
        start_date = end_date - timedelta(days=7)
    elif cycle_type == 'bi-weekly':
        start_date = end_date - timedelta(days=15)
    elif cycle_type == 'monthly':
        # Aproximação simples para mensal
        start_date = end_date - timedelta(days=30)
    else:
        raise ValueError("Tipo de ciclo inválido. Use 'weekly', 'bi-weekly', ou 'monthly'.")
    return start_date, end_date


def _to_decimal(val: Any) -> Decimal:
    if val is None:
        return Decimal('0')
    if isinstance(val, Decimal):
        return val
    try:
        return Decimal(str(val))
    except Exception:
        return Decimal('0')

def process_payouts_for_cycle(
    cycle_type: str,
    partner_type: str
) -> Tuple[Union[List[Dict[str, Any]], Dict[str, str]], int]:
    """
    Processa e gera registros de 'payout' para todos os parceiros de um determinado tipo e ciclo.
    Retorna (generated_payouts, count) em caso de sucesso, ou ({"error": ...}, 0) em caso de erro.
    """
    if partner_type not in ['restaurant', 'delivery']:
        raise ValueError("Tipo de parceiro inválido. Use 'restaurant' ou 'delivery'.")

    start_date, end_date = get_date_range_for_cycle(cycle_type)
    conn = get_db_connection()
    if not conn:
        logging.error("Não foi possível conectar à base de dados para processar pagamentos.")
        return {"error": "DB connection failed"}, 0

    generated_payouts: List[Dict[str, Any]] = []
    payouts_generated_count = 0

    profile_table = f"{partner_type}_profiles"
    amount_column = f"valor_repassado_{partner_type}"
    payout_id_column = f"{partner_type}_payout_id"
    partner_id_column = f"{partner_type}_id"

    try:
        # Transação explícita para garantir atomicidade (insert payout + update orders)
        with conn:
            with conn.cursor() as cur:
                # 1) Encontrar todos os parceiros elegíveis para este ciclo
                cur.execute(
                    f"SELECT id FROM {profile_table} WHERE payout_cycle = %s",
                    (cycle_type,)
                )
                eligible_partners = cur.fetchall()
                logging.info(
                    f"Encontrados {len(eligible_partners)} parceiros do tipo '{partner_type}' para o ciclo '{cycle_type}'."
                )

                # 2) Para cada parceiro, calcular o valor a ser repassado e criar payout
                for partner in eligible_partners:
                    partner_id = partner[0]

                    # 3) Buscar todos os pedidos pagos, concluídos e ainda não repassados dentro do período, com locking para evitar corrida
                    cur.execute(
                        f"""
                        SELECT id, {amount_column}
                        FROM orders
                        WHERE {partner_id_column} = %s
                          AND status = 'Concluído' -- status finalizado
                          AND status_pagamento = 'approved'
                          AND created_at BETWEEN %s AND %s
                          AND {payout_id_column} IS NULL
                        FOR UPDATE SKIP LOCKED;
                        """,
                        (partner_id, start_date, end_date)
                    )
                    unpaid_orders = cur.fetchall()
                    if not unpaid_orders:
                        continue  # Nenhum pedido elegível neste período

                    total_amount = sum(_to_decimal(row[1]) for row in unpaid_orders)
                    if total_amount <= 0:
                        continue

                    order_ids_included = [row[0] for row in unpaid_orders]

                    # 4) Inserir o registro de payout mantendo o schema atual
                    payout_data = {
                        "partner_id": partner_id,
                        "partner_type": partner_type,
                        "amount": total_amount,
                        "period_start": start_date,
                        "period_end": end_date,
                        "order_ids_included": order_ids_included,
                    }

                    cur.execute(
                        """
                        INSERT INTO payouts (
                            partner_id,
                            partner_type,
                            amount,
                            period_start,
                            period_end,
                            order_ids_included
                        )
                        VALUES (
                            %(partner_id)s,
                            %(partner_type)s,
                            %(amount)s,
                            %(period_start)s,
                            %(period_end)s,
                            %(order_ids_included)s
                        )
                        RETURNING id;
                        """,
                        payout_data
                    )
                    new_payout_id = cur.fetchone()[0]

                    # 5) Vincular pedidos ao payout
                    cur.execute(
                        f"UPDATE orders SET {payout_id_column} = %s WHERE id = ANY(%s)",
                        (new_payout_id, order_ids_included)
                    )

                    payouts_generated_count += 1
                    generated_payouts.append({
                        "payout_id": new_payout_id,
                        "partner_id": partner_id,
                        "partner_type": partner_type,
                        "cycle_type": cycle_type,
                        "period_start": start_date.isoformat(),
                        "period_end": end_date.isoformat(),
                        "total_amount": str(total_amount),
                        "order_ids": [str(x) for x in order_ids_included],
                        "order_count": len(order_ids_included),
                        "status": "pending",
                    })

                logging.info("Processamento de payouts concluído com sucesso.")

        return generated_payouts, payouts_generated_count

    except Exception as e:
        logging.error(f"Erro ao processar payouts: {e}", exc_info=True)
        return {"error": str(e)}, 0

    finally:
        try:
            conn.close()
        except Exception:
            pass