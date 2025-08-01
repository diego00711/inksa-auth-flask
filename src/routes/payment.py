# src/routes/payment.py (VERSÃO COM A CORREÇÃO FINAL DA CHAVE 'unit_price')

from flask import Blueprint, request, jsonify, current_app
import mercadopago
from supabase import create_client, Client
import os
import logging

# Configuração do logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Criação do Blueprint
mp_payment_bp = Blueprint('mp_payment_bp', __name__)

# Inicialização do Cliente Supabase
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
supabase_client = None

if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    logging.error("ERRO: SUPABASE_URL ou SUPABASE_SERVICE_KEY não configurados.")
else:
    try:
        supabase_client: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
        logging.info("Cliente Supabase (payment.py) inicializado com sucesso.")
    except Exception as e:
        logging.error(f"ERRO ao inicializar cliente Supabase: {e}")

@mp_payment_bp.route('/pagamentos/criar_preferencia', methods=['POST'])
def criar_preferencia_mercado_pago():
    try:
        sdk = current_app.mp_sdk
        if sdk is None:
            return jsonify({"erro": "Serviço de pagamento indisponível. Credenciais do Mercado Pago ausentes."}), 503

        dados_pedido = request.json
        if not dados_pedido:
            return jsonify({"erro": "Dados do pedido não fornecidos."}), 400

        logging.info(f"Dados recebidos para criar preferência: {dados_pedido}")

        # Lógica de filtro explícita e robusta
        items_mp = []
        items_from_request = dados_pedido.get('itens', [])
        logging.info(f"--- Itens recebidos para filtrar: {items_from_request}")

        for item in items_from_request:
            # ✅ CORREÇÃO FINAL: Usar a chave em inglês 'unit_price' para corresponder ao frontend
            preco = item.get('unit_price')
            
            if preco is not None:
                try:
                    if float(preco) > 0:
                        items_mp.append(item)
                    else:
                        logging.warning(f"Item ignorado por ter preço zero ou negativo: {item}")
                except (ValueError, TypeError):
                    logging.warning(f"Item ignorado por preço inválido (não é um número): {item}")
            else:
                # ✅ CORREÇÃO FINAL: Atualizar a mensagem de erro para 'unit_price'
                logging.warning(f"Item ignorado por não ter a chave 'unit_price': {item}")
        
        logging.info(f"--- Itens que serão enviados ao Mercado Pago (após filtro): {items_mp}")

        if not items_mp:
            return jsonify({"erro": "A lista de itens está vazia ou todos os itens têm valor zero."}), 400

        urls_retorno = dados_pedido.get('urls_retorno', {})
        FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:3000")
        success_url = urls_retorno.get('sucesso', f"{FRONTEND_URL}/pagamento/sucesso")
        failure_url = urls_retorno.get('falha', f"{FRONTEND_URL}/pagamento/falha")
        pending_url = urls_retorno.get('pendente', f"{FRONTEND_URL}/pagamento/pendente")

        notification_url_mp_base = os.environ.get("MERCADO_PAGO_WEBHOOK_URL")
        if not notification_url_mp_base:
            return jsonify({"erro": "URL de notificação do Mercado Pago não configurada."}), 500
        
        notification_url_mp = f"{notification_url_mp_base}/api/pagamentos/webhook_mp"

        preference_data = {
            "items": items_mp,
            "payer": {"email": dados_pedido.get('cliente_email', 'comprador@exemplo.com.br')},
            "back_urls": {
                "success": success_url,
                "failure": failure_url,
                "pending": pending_url
            },
            "auto_return": "approved",
            "external_reference": dados_pedido.get('pedido_id', 'id_pedido_temp'),
            "notification_url": notification_url_mp
        }
        
        logging.info(f"Enviando dados finais para o MP: {preference_data}")
        preference_response = sdk.preference().create(preference_data)

        logging.info(f"Resposta completa do Mercado Pago: {preference_response}")

        if "response" not in preference_response or preference_response.get("status", 200) >= 400:
            logging.error(f"Mercado Pago rejeitou a preferência: {preference_response}")
            error_message = preference_response.get("response", {}).get("message", "Erro desconhecido do MP.")
            return jsonify({
                "erro": "O Mercado Pago recusou a criação do pagamento.", 
                "detalhes": error_message
            }), 400

        preference = preference_response["response"]
        return jsonify({
            "mensagem": "Preferência de pagamento criada com sucesso!",
            "checkout_link": preference["init_point"],
            "preference_id": preference["id"]
        }), 200

    except Exception as e:
        logging.error(f"Erro CRÍTICO ao criar preferência de pagamento: {e}", exc_info=True)
        return jsonify({"erro": "Erro interno ao processar pagamento."}), 500


@mp_payment_bp.route('/pagamentos/webhook_mp', methods=['POST'])
def mercadopago_webhook():
    # SUA LÓGICA DE WEBHOOK COMPLETA E CORRETA PERMANECE AQUI
    # (código omitido para encurtar, mas é o mesmo que já validamos)
    logging.info("--- Webhook do Mercado Pago recebido! ---")
    request_data = request.get_json(silent=True)
    if not request_data:
        request_data = request.args.to_dict()
        logging.warning("Webhook não é JSON ou é um formato antigo. Processando de request.args.")
    else:
        logging.info("Webhook recebido em formato JSON.")
    topic = request_data.get('topic')
    resource_id = request_data.get('id')
    if request_data.get('type') == 'payment' and request_data.get('data', {}).get('id'):
        topic = 'payment'
        resource_id = request_data['data']['id']
    elif request_data.get('topic') == 'payment' and request_data.get('id'):
        topic = 'payment'
        resource_id = request_data['id']
    if not topic and request.args.get('topic'):
        topic = request.args.get('topic')
        resource_id = request.args.get('id')
    logging.info(f"Webhook processando: Topic: {topic}, Resource ID: {resource_id}, Full Data: {request_data}")
    if topic == 'payment':
        if supabase_client is None:
            return jsonify({"status": "error", "message": "Serviço de banco de dados indisponível."}), 500
        try:
            sdk = current_app.mp_sdk
            if sdk is None:
                return jsonify({"status": "error", "message": "Serviço de pagamento indisponível."}), 503
            payment_info = sdk.payment().get(resource_id)
            if not (payment_info and "response" in payment_info):
                return jsonify({"status": "error", "message": "Detalhes do pagamento não encontrados"}), 404
            payment_data = payment_info["response"]
            status = payment_data.get("status")
            external_reference = payment_data.get("external_reference")
            existing_order_query = supabase_client.table('orders').select('status_pagamento, id_transacao_mp').eq('id', external_reference).single().execute()
            existing_order_data = existing_order_query.data
            if existing_order_data:
                if existing_order_data.get('id_transacao_mp') == resource_id and existing_order_data.get('status_pagamento') == status:
                    logging.info(f"Webhook duplicado ignorado para pedido {external_reference}.")
                    return jsonify({"status": "ok", "message": "Webhook já processado"}), 200
                if existing_order_data.get('status_pagamento') == 'approved' and status != 'approved':
                    logging.info(f"Webhook de status inferior ignorado para pedido {external_reference}.")
                    return jsonify({"status": "ok", "message": "Webhook de status inferior ignorado"}), 200
            if status == 'approved':
                logging.info(f"Pagamento {resource_id} APROVADO. Iniciando cálculos de repasse.")
                response_supabase = supabase_client.table('orders').select('*, restaurant_profiles(mp_account_id), delivery_profiles(mp_account_id)').eq('id', external_reference).single().execute()
                if response_supabase.data:
                    pedido_do_bd = response_supabase.data
                    valor_frete = float(pedido_do_bd.get('delivery_fee', 0.0))
                    valor_total_itens = float(pedido_do_bd.get('total_amount_items', 0.0))
                    comissao_plataforma = valor_total_itens * 0.15
                    valor_para_restaurante = valor_total_itens - comissao_plataforma
                    valor_para_entregador = valor_frete
                    update_data = {
                        'status_pagamento': status,
                        'comissao_plataforma': round(comissao_plataforma, 2),
                        'valor_repassado_restaurante': round(valor_para_restaurante, 2),
                        'valor_repassado_entregador': round(valor_para_entregador, 2),
                        'id_transacao_mp': resource_id
                    }
                    supabase_client.table('orders').update(update_data).eq('id', external_reference).execute()
                    logging.info(f"Pedido {external_reference} atualizado no Supabase com status e repasses.")
                else:
                    logging.warning("Não foi possível calcular repasses: Pedido não encontrado no Supabase.")
            elif status in ['pending', 'in_process', 'rejected']:
                logging.info(f"Pagamento {resource_id} com status '{status}'.")
                supabase_client.table('orders').update({'status_pagamento': status, 'id_transacao_mp': resource_id}).eq('id', external_reference).execute()
            else:
                logging.info(f"Status do pagamento {resource_id} desconhecido ou não tratado: {status}")
        except Exception as e:
            logging.error(f"Erro ao processar webhook de pagamento: {e}", exc_info=True)
    else:
        logging.info(f"Webhook de tópico não tratado: {topic}. Retornando OK.")
    return jsonify({"status": "ok"}), 200