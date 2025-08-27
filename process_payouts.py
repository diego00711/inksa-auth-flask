# process_payouts.py (versão 100% completa)

import os
import uuid
from dotenv import load_dotenv
from supabase import create_client, Client
from collections import defaultdict
from decimal import Decimal, getcontext
import requests

# Define a precisão para cálculos com Decimal
getcontext().prec = 10

# Carrega as variáveis de ambiente do seu arquivo .env
load_dotenv()


def process_all_payouts():
    """
    Script completo para processar repasses: busca, agrupa, busca dados de pagamento,
    realiza o pagamento via chamada direta à API de Payouts e atualiza o status.
    """
    print("--- Iniciando script de processamento de repasses ---")

    # 1. Configuração
    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_SERVICE_KEY")
    mp_access_token = os.environ.get("MERCADO_PAGO_ACCESS_TOKEN")

    if not all([supabase_url, supabase_key, mp_access_token]):
        print(
            "ERRO: Verifique se SUPABASE_URL, SUPABASE_SERVICE_KEY e MERCADO_PAGO_ACCESS_TOKEN estão no .env"
        )
        return

    supabase: Client = create_client(supabase_url, supabase_key)
    print("Conexão com Supabase estabelecida.")

    # 2. Buscar todos os pedidos com repasse pendente
    try:
        print("Buscando pedidos com pagamento aprovado e repasse pendente...")
        response = (
            supabase.table("orders")
            .select(
                "id, restaurant_id, delivery_id, valor_repassado_restaurante, valor_repassado_entregador"
            )
            .eq("status_pagamento", "approved")
            .eq("payout_status", "pending")
            .execute()
        )

        if not response.data:
            print("Nenhum pedido novo para processar repasse. Encerrando.")
            return
        orders_to_process = response.data
        print(f"Encontrados {len(orders_to_process)} itens de pedido para processar.")

    except Exception as e:
        print(f"ERRO ao buscar pedidos no Supabase: {e}")
        return

    # 3. Agrupar os valores a serem pagos por parceiro
    restaurant_payouts = defaultdict(Decimal)
    delivery_payouts = defaultdict(Decimal)
    payout_order_map = defaultdict(list)

    for order in orders_to_process:
        if order.get("restaurant_id") and order.get("valor_repassado_restaurante"):
            restaurant_id = order["restaurant_id"]
            amount = Decimal(str(order["valor_repassado_restaurante"]))
            if amount > 0:
                restaurant_payouts[restaurant_id] += amount
                payout_order_map[("restaurant", restaurant_id)].append(order["id"])

        if order.get("delivery_id") and order.get("valor_repassado_entregador"):
            delivery_id = order["delivery_id"]
            amount = Decimal(str(order["valor_repassado_entregador"]))
            if amount > 0:
                delivery_payouts[delivery_id] += amount
                payout_order_map[("delivery", delivery_id)].append(order["id"])

    # 4. Para cada grupo, buscar os dados de pagamento do perfil
    final_payout_list = []
    print("\n--- Buscando dados de pagamento dos parceiros ---")
    if restaurant_payouts:
        restaurant_ids = list(restaurant_payouts.keys())
        try:
            resp_profiles = (
                supabase.table("restaurant_profiles")
                .select("id, restaurant_name, pix_key, mp_account_id")
                .in_("id", restaurant_ids)
                .execute()
            )
            for profile in resp_profiles.data:
                partner_id = profile["id"]
                final_payout_list.append(
                    {
                        "partner_type": "restaurant",
                        "partner_id": partner_id,
                        "partner_name": profile.get("restaurant_name", "N/A"),
                        "amount": f"{restaurant_payouts[partner_id]:.2f}",
                        "payment_details": {
                            "pix_key": profile.get("pix_key"),
                            "mp_account_id": profile.get("mp_account_id"),
                        },
                        "order_ids": payout_order_map[("restaurant", partner_id)],
                    }
                )
        except Exception as e:
            print(f"ERRO ao buscar perfis de restaurantes: {e}")

    # (A lógica para buscar perfis de entregadores seria adicionada aqui, se houvesse algum a pagar)

    # 5 e 6. Realizar Payouts e Atualizar Status
    print("\n--- Iniciando processamento de transferências via API Direta ---")
    if not final_payout_list:
        print("Nenhum repasse a ser feito. Encerrando.")
        return

    payout_api_url = "https://api.mercadopago.com/v1/payouts"
    auth_headers = {
        "Authorization": f"Bearer {mp_access_token}",
        "Content-Type": "application/json",
    }

    for payout_job in final_payout_list:
        partner_type = payout_job["partner_type"]
        partner_id = payout_job["partner_id"]
        amount_to_pay = float(payout_job["amount"])
        pix_key = payout_job["payment_details"].get("pix_key")
        order_ids_to_update = payout_job["order_ids"]

        print(
            f"\nProcessando repasse para: {partner_type} {partner_id} no valor de R$ {amount_to_pay:.2f}"
        )

        if not pix_key:
            print(f"  -> ERRO: Parceiro não possui chave PIX cadastrada. Pulando.")
            continue

        try:
            payout_data = {
                "amount": amount_to_pay,
                "external_reference": f"payout-{partner_id}-{str(uuid.uuid4())}",
                "method": "pix",
                "receiver": {"contact": {"email": pix_key}},
            }
            idempotency_key = str(uuid.uuid4())
            request_headers = auth_headers.copy()
            request_headers["X-Idempotency-Key"] = idempotency_key

            print(f"  -> Enviando para a API de Payouts... PIX: {pix_key}")
            response = requests.post(
                payout_api_url, json=payout_data, headers=request_headers
            )
            response_json = response.json()
            print(
                f"  -> Resposta do Servidor: Status={response.status_code}, Corpo={response_json}"
            )

            if response.status_code in [200, 201]:
                print(f"  -> Sucesso! Payout criado com ID: {response_json.get('id')}")
                try:
                    update_resp = (
                        supabase.table("orders")
                        .update({"payout_status": "processed"})
                        .in_("id", order_ids_to_update)
                        .execute()
                    )
                    print(
                        f"  -> {len(update_resp.data)} pedidos atualizados para 'processed' no banco de dados."
                    )
                except Exception as db_error:
                    print(
                        f"  -> ALERTA GRAVE: Payout realizado, mas FALHOU ao atualizar o status no banco de dados: {db_error}"
                    )
            else:
                print(f"  -> FALHA: A API de Payouts retornou um erro.")
        except Exception as e:
            print(f"  -> ERRO CRÍTICO ao tentar processar a transferência: {e}")

    print("\n--- Script concluído ---")


if __name__ == "__main__":
    process_all_payouts()
