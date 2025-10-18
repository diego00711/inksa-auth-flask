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
        start_date = end_date - timedelta(days=30)
    else:
        raise ValueError("Tipo de ciclo inválido. Use 'weekly', 'bi-weekly', ou 'monthly'.")
    
    logging.info(f"📅 Período calculado: {start_date.date()} até {end_date.date()}")
    return start_date, end_date


def _to_decimal(val: Any) -> Decimal:
    """Converte qualquer valor para Decimal de forma segura."""
    if val is None:
        return Decimal('0')
    if isinstance(val, Decimal):
        return val
    try:
        return Decimal(str(val))
    except Exception:
        logging.warning(f"⚠️ Não foi possível converter {val} para Decimal. Usando 0.")
        return Decimal('0')


def process_payouts_for_cycle(
    cycle_type: str,
    partner_type: str
) -> Tuple[Union[List[Dict[str, Any]], Dict[str, str]], int]:
    """
    Processa e gera registros de 'payout' para todos os parceiros de um determinado tipo e ciclo.
    Retorna (generated_payouts, count) em caso de sucesso, ou ({"error": ...}, 0) em caso de erro.
    """
    logging.info(f"🎯 === INICIANDO PROCESSAMENTO DE PAYOUTS ===")
    logging.info(f"📋 Tipo: {partner_type} | Ciclo: {cycle_type}")
    
    if partner_type not in ['restaurant', 'delivery']:
        raise ValueError("Tipo de parceiro inválido. Use 'restaurant' ou 'delivery'.")

    start_date, end_date = get_date_range_for_cycle(cycle_type)
    conn = get_db_connection()
    
    if not conn:
        logging.error("❌ Não foi possível conectar à base de dados!")
        return {"error": "DB connection failed"}, 0

    generated_payouts: List[Dict[str, Any]] = []
    payouts_generated_count = 0

    # ✅ CORREÇÃO: Nomes corretos das colunas
    profile_table = f"{partner_type}_profiles"
    
    # Para delivery: usa 'valor_repassado_entregador', não 'valor_repassado_delivery'
    if partner_type == 'delivery':
        amount_column = "valor_repassado_entregador"
    else:
        amount_column = f"valor_repassado_{partner_type}"
    
    payout_id_column = f"{partner_type}_payout_id"
    partner_id_column = f"{partner_type}_id"
    
    logging.info(f"📊 Configuração:")
    logging.info(f"   - Tabela: {profile_table}")
    logging.info(f"   - Coluna de valor: {amount_column}")
    logging.info(f"   - Coluna de payout: {payout_id_column}")
    logging.info(f"   - Coluna de parceiro: {partner_id_column}")

    try:
        with conn:
            with conn.cursor() as cur:
                # 1) Encontrar todos os parceiros elegíveis para este ciclo
                cur.execute(
                    f"SELECT id FROM {profile_table} WHERE payout_cycle = %s",
                    (cycle_type,)
                )
                eligible_partners = cur.fetchall()
                
                logging.info(f"✅ Encontrados {len(eligible_partners)} parceiros elegíveis")

                if not eligible_partners:
                    logging.warning(f"⚠️ Nenhum parceiro encontrado com ciclo '{cycle_type}'")
                    return [], 0

                # 2) Para cada parceiro, calcular o valor a ser repassado
                for idx, partner in enumerate(eligible_partners, 1):
                    partner_id = partner[0]
                    logging.info(f"🔄 Processando parceiro {idx}/{len(eligible_partners)}: {partner_id}")

                    # 3) Buscar pedidos pagos e entregues ainda não repassados
                    # ✅ CORREÇÃO: status = 'delivered' (não 'Concluído')
                    cur.execute(
                        f"""
                        SELECT id, {amount_column}
                        FROM orders
                        WHERE {partner_id_column} = %s
                          AND status = 'delivered'
                          AND status_pagamento = 'approved'
                          AND created_at BETWEEN %s AND %s
                          AND {payout_id_column} IS NULL
                        FOR UPDATE SKIP LOCKED;
                        """,
                        (partner_id, start_date, end_date)
                    )
                    
                    unpaid_orders = cur.fetchall()
                    
                    if not unpaid_orders:
                        logging.info(f"   ⏭️ Nenhum pedido elegível para este parceiro")
                        continue

                    total_amount = sum(_to_decimal(row[1]) for row in unpaid_orders)
                    
                    if total_amount <= 0:
                        logging.warning(f"   ⚠️ Valor total é zero ou negativo: {total_amount}")
                        continue

                    order_ids_included = [row[0] for row in unpaid_orders]
                    
                    logging.info(f"   💰 Total: R$ {total_amount} ({len(order_ids_included)} pedidos)")

                    # 4) Inserir o registro de payout
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
                            order_ids_included,
                            status,
                            created_at
                        )
                        VALUES (
                            %(partner_id)s,
                            %(partner_type)s,
                            %(amount)s,
                            %(period_start)s,
                            %(period_end)s,
                            %(order_ids_included)s,
                            'pending',
                            NOW()
                        )
                        RETURNING id;
                        """,
                        payout_data
                    )
                    
                    new_payout_id = cur.fetchone()[0]
                    logging.info(f"   ✅ Payout criado: {new_payout_id}")

                    # 5) Vincular pedidos ao payout
                    cur.execute(
                        f"UPDATE orders SET {payout_id_column} = %s WHERE id = ANY(%s)",
                        (new_payout_id, order_ids_included)
                    )
                    
                    logging.info(f"   ✅ {len(order_ids_included)} pedidos vinculados ao payout")

                    payouts_generated_count += 1
                    generated_payouts.append({
                        "payout_id": str(new_payout_id),
                        "partner_id": str(partner_id),
                        "partner_type": partner_type,
                        "cycle_type": cycle_type,
                        "period_start": start_date.isoformat(),
                        "period_end": end_date.isoformat(),
                        "total_amount": str(total_amount),
                        "order_ids": [str(x) for x in order_ids_included],
                        "order_count": len(order_ids_included),
                        "status": "pending",
                    })

                logging.info(f"🎉 Processamento concluído! {payouts_generated_count} payouts gerados.")

        return generated_payouts, payouts_generated_count

    except Exception as e:
        logging.error(f"❌ ERRO CRÍTICO ao processar payouts: {e}", exc_info=True)
        return {"error": str(e)}, 0

    finally:
        try:
            conn.close()
            logging.info("🔒 Conexão com banco fechada")
        except Exception:
            pass
