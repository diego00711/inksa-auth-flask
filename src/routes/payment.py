# src/routes/payment.py

from flask import Blueprint, request, jsonify, current_app
import mercadopago
from supabase import create_client, Client
import os
import logging
import hmac
import hashlib
import time

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

# Função de verificação de assinatura do Webhook (mantida, está ótima)
def verify_mp_signature(req, secret):
    """Verifica a assinatura da notificação de webhook do Mercado Pago."""
    signature_header = req.headers.get('X-Signature')
    if not signature_header:
        logging.warning("Webhook recebido sem o cabeçalho X-Signature.")
        return False

    parts = {p.split('=')[0]: p.split('=')[1] for p in signature_header.split(',')}
    ts = parts.get('ts')
    signature_hash = parts.get('v1')

    if not ts or not signature_hash:
        logging.warning("Cabeçalho X-Signature com formato inválido.")
        return False
        
    notification_id = req.args.get('id')
    if not notification_id:
        json_data = req.get_json(silent=True)
        if json_data and 'data' in json_data and 'id' in json_data['data']:
             notification_id = json_data['data']['id']
        else:
            notification_id = req.args.get('id', 'id_not_found')

    manifest_string = f"id:{notification_id};ts:{ts};"

    local_signature = hmac.new(
        secret.encode(),
        msg=manifest_string.encode(),
        digestmod=hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(local_signature, signature_hash)


@mp_payment_bp.route('/pagamentos/criar_preferencia', methods=['POST'])
def criar_preferencia_mercado_pago():
    logging.info("🎯 === INICIANDO CRIAÇÃO DE PREFERÊNCIA DE PAGAMENTO ===")
    try:
        sdk = current_app.mp_sdk
        if sdk is None:
            logging.error("❌ SDK do Mercado Pago não inicializado!")
            return jsonify({"erro": "Serviço de pagamento indisponível. Credenciais do Mercado Pago ausentes."}), 503
        
        dados_pedido = request.json
        logging.info(f"📦 Dados recebidos: {dados_pedido}")
        
        if not dados_pedido:
            logging.error("❌ Dados do pedido não fornecidos")
            return jsonify({"erro": "Dados do pedido não fornecidos."}), 400
        
        # ✅ CORREÇÃO: Converte strings para números e valida tipos
        items_mp = []
        items_from_request = dados_pedido.get('itens', [])
        
        logging.info(f"📋 Processando {len(items_from_request)} itens...")
        
        for idx, item in enumerate(items_from_request):
            try:
                # Converte valores para os tipos corretos
                preco = float(item.get('unit_price', 0))
                quantidade = int(item.get('quantity', 1))
                titulo = str(item.get('title', f'Item {idx + 1}'))
                
                if preco > 0 and quantidade > 0:
                    # Cria item com tipos corretos (número, não string)
                    item_corrigido = {
                        'title': titulo,
                        'quantity': quantidade,
                        'unit_price': preco  # ✅ Sempre número float
                    }
                    items_mp.append(item_corrigido)
                    logging.info(f"✅ Item {idx + 1} adicionado: {titulo} - R$ {preco} x {quantidade}")
                else:
                    logging.warning(f"⚠️ Item {idx + 1} ignorado (preço ou quantidade inválidos): {item}")
                    
            except (ValueError, TypeError) as e:
                logging.error(f"❌ Erro ao processar item {idx + 1}: {e} - Item: {item}")
                continue
        
        if not items_mp:
            logging.error("❌ Nenhum item válido para processar!")
            return jsonify({"erro": "A lista de itens está vazia ou todos os itens têm valor zero."}), 400
        
        logging.info(f"✅ Total de itens válidos: {len(items_mp)}")
        
        urls_retorno = dados_pedido.get('urls_retorno', {})
        FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:3000")
        notification_url_mp_base = os.environ.get("MERCADO_PAGO_WEBHOOK_URL")
        
        if not notification_url_mp_base:
            logging.error("❌ URL de notificação do Mercado Pago não configurada!")
            return jsonify({"erro": "URL de notificação do Mercado Pago não configurada."}), 500
        
        preference_data = {
            "items": items_mp,
            "payer": {"email": dados_pedido.get('cliente_email', 'comprador@exemplo.com.br')},
            "back_urls": {
                "success": urls_retorno.get('sucesso', f"{FRONTEND_URL}/pagamento/sucesso"),
                "failure": urls_retorno.get('falha', f"{FRONTEND_URL}/pagamento/falha"),
                "pending": urls_retorno.get('pendente', f"{FRONTEND_URL}/pagamento/pendente")
            },
            "auto_return": "approved",
            "external_reference": dados_pedido.get('pedido_id', 'id_pedido_temp'),
            "notification_url": f"{notification_url_mp_base}/api/pagamentos/webhook_mp"
        }
        
        logging.info(f"🚀 Enviando preferência para Mercado Pago...")
        logging.info(f"📋 Preference data: {preference_data}")
        
        preference_response = sdk.preference().create(preference_data)
        
        if "response" not in preference_response or preference_response.get("status", 200) >= 400:
            erro_detalhes = preference_response.get("response", {}).get("message", "Erro desconhecido do MP.")
            logging.error(f"❌ Mercado Pago recusou a criação: {erro_detalhes}")
            logging.error(f"❌ Resposta completa do MP: {preference_response}")
            return jsonify({
                "erro": "O Mercado Pago recusou a criação do pagamento.", 
                "detalhes": erro_detalhes
            }), 400
        
        preference = preference_response["response"]
        logging.info(f"✅ Preferência criada com sucesso! ID: {preference['id']}")
        logging.info(f"✅ Link de checkout: {preference['init_point']}")
        
        return jsonify({
            "mensagem": "Preferência de pagamento criada com sucesso!",
            "checkout_link": preference["init_point"],
            "preference_id": preference["id"]
        }), 200
        
    except Exception as e:
        logging.error(f"❌ ERRO CRÍTICO ao criar preferência de pagamento: {e}", exc_info=True)
        return jsonify({"erro": "Erro interno ao processar pagamento."}), 500


@mp_payment_bp.route('/pagamentos/webhook_mp', methods=['POST'])
def mercadopago_webhook():
    webhook_secret = os.environ.get("MERCADO_PAGO_WEBHOOK_SECRET")
    if webhook_secret and not verify_mp_signature(request, webhook_secret):
        logging.error("FALHA NA VERIFICAÇÃO DA ASSINATURA DO WEBHOOK!")
        return jsonify({"status": "error", "message": "Assinatura inválida."}), 403
    
    logging.info("--- Webhook do Mercado Pago recebido e assinatura verificada! ---")
    
    request_data = request.get_json(silent=True) or request.args.to_dict()
    topic = request_data.get('topic')
    resource_id = request_data.get('id')
    
    if request_data.get('type') == 'payment' and request_data.get('data', {}).get('id'):
        topic = 'payment'
        resource_id = request_data['data']['id']
    elif not topic and request.args.get('topic'):
        topic = request.args.get('topic')
        resource_id = request.args.get('id')

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
            
            if status == 'approved':
                logging.info(f"Pagamento {resource_id} APROVADO. Iniciando cálculos de repasse.")
                response_supabase = supabase_client.table('orders').select('*').eq('id', external_reference).single().execute()
                if response_supabase.data:
                    pedido_do_bd = response_supabase.data
                    valor_total_itens = float(pedido_do_bd.get('total_amount_items', 0.0))
                    
                    comissao_plataforma = valor_total_itens * current_app.config['PLATFORM_COMMISSION_RATE']
                    
                    valor_para_restaurante = valor_total_itens - comissao_plataforma
                    valor_para_entregador = float(pedido_do_bd.get('delivery_fee', 0.0))
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
                supabase_client.table('orders').update({'status_pagamento': status, 'id_transacao_mp': resource_id}).eq('id', external_reference).execute()

        except Exception as e:
            logging.error(f"Erro ao processar webhook de pagamento: {e}", exc_info=True)
            
    return jsonify({"status": "ok"}), 200
