# src/logic/payout_processor.py

import logging
from datetime import datetime, timedelta
from ..utils.helpers import get_db_connection

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def get_date_range_for_cycle(cycle_type):
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

def process_payouts_for_cycle(cycle_type, partner_type):
    """
    Processa e gera registos de 'payout' para todos os parceiros de um determinado tipo e ciclo.
    """
    if partner_type not in ['restaurant', 'delivery']:
        raise ValueError("Tipo de parceiro inválido. Use 'restaurant' ou 'delivery'.")

    start_date, end_date = get_date_range_for_cycle(cycle_type)
    conn = get_db_connection()
    if not conn:
        logging.error("Não foi possível conectar à base de dados para processar pagamentos.")
        return {"error": "DB connection failed"}, 0

    payouts_generated_count = 0
    generated_payouts = []

    try:
        with conn.cursor() as cur:
            # 1. Encontrar todos os parceiros elegíveis para este ciclo
            profile_table = f"{partner_type}_profiles"
            cur.execute(f"SELECT id FROM {profile_table} WHERE payout_cycle = %s", (cycle_type,))
            eligible_partners = cur.fetchall()
            
            logging.info(f"Encontrados {len(eligible_partners)} parceiros do tipo '{partner_type}' para o ciclo '{cycle_type}'.")

            # 2. Para cada parceiro, calcular o valor a ser repassado
            for partner in eligible_partners:
                partner_id = partner[0]
                
                # Definir as colunas corretas com base no tipo de parceiro
                amount_column = f"valor_repassado_{partner_type}"
                payout_id_column = f"{partner_type}_payout_id"
                partner_id_column = f"{partner_type}_id"

                # 3. Buscar todos os pedidos pagos, concluídos, e AINDA NÃO REPASSADOS dentro do período
                sql_get_orders = f"""
                    SELECT id, {amount_column}
                    FROM orders
                    WHERE {partner_id_column} = %s
                      AND status = 'Concluído' -- ou o status que considera um pedido finalizado
                      AND status_pagamento = 'approved'
                      AND created_at BETWEEN %s AND %s
                      AND {payout_id_column} IS NULL;
                """
                cur.execute(sql_get_orders, (partner_id, start_date, end_date))
                unpaid_orders = cur.fetchall()

                if not unpaid_orders:
                    continue # Nenhum pedido a pagar para este parceiro neste período

                # 4. Calcular o total e preparar o registo de payout
                total_amount = sum(order[1] for order in unpaid_orders)
                order_ids_included = [order[0] for order in unpaid_orders]
                
                if total_amount <= 0:
                    continue

                payout_data = {
                    "partner_id": partner_id,
                    "partner_type": partner_type,
                    "amount": total_amount,
                    "period_start": start_date,
                    "period_end": end_date,
                    "order_ids_included": order_ids_included
                }
                
                # 5. Inserir o novo registo de 'payout'
                cur.execute(
                    """
                    INSERT INTO payouts (partner_id, partner_type, amount, period_start, period_end, order_ids_included)
                    VALUES (%(partner_id)s, %(partner_type)s, %(amount)s, %(period_start)s, %(period_end)s, %(order_ids_included)s)
                    RETURNING id;
                    """,
                    payout_data
                )
                new_payout_id = cur.fetchone()[0]

                # 6. Atualizar os pedidos para os marcar como 'processados' neste lote de pagamento
                cur.execute(
                    f"UPDATE orders SET {payout_id_column} = %s WHERE id = ANY(%s)",
                    (new_payout_id, order_ids_included)
                )
                
                logging.info(f"Gerado payout {new_payout_id} de R${total_amount:.2f} para o parceiro {partner_id}.")
                payouts_generated_count += 1
                generated_payouts.append({**payout_data, "payout_id": new_payout_id})

            conn.commit()
            logging.info("Processamento de payouts concluído com sucesso.")

    except Exception as e:
        if conn: conn.rollback()
        logging.error(f"Erro CRÍTICO ao processar payouts: {e}", exc_info=True)
        return {"error": str(e)}, 0
    finally:
        if conn: conn.close()
        
    return generated_payouts, payouts_generated_count