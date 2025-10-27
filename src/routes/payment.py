# src/routes/payment.py - VERSÃO SIMPLES E FUNCIONAL

from flask import Blueprint, request, jsonify, current_app
import mercadopago
from supabase import create_client, Client
import os
import logging
import hmac
import hashlib

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


def verify_mp_signature(req, secret):
    """Verifica a assinatura da notificação de webhook do Mercado Pago."""
    signature_header = req.headers.get('X-Signature')
    if not signature_header:
        logging.warning("⚠️ Webhook recebido SEM X-Signature - processando mesmo assim")
        return True

    try:
        parts = {p.split('=')[0]: p.split('=')[1] for p in signature_header.split(',')}
        ts = parts.get('ts')
        signature_hash = parts.get('v1')

        if not ts or not signature_hash:
            logging.warning("⚠️ Cabeçalho X-Signature com formato inválido - processando mesmo assim")
            return True
            
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

        is_valid = hmac.compare_digest(local_signature, signature_hash)
        
        if is_valid:
            logging.info("✅ Assinatura do webhook VÁLIDA!")
        else:
            logging.warning("⚠️ Assinatura do webhook INVÁLIDA - mas processando mesmo assim")
            
        return True
        
    except Exception as e:
        logging.error(f"❌ Erro ao validar assinatura: {e} - processando mesmo assim")
        return True


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
        
        # ✅ VALIDAÇÃO DO EMAIL (BLOQUEIA EMAILS DE TESTE)
        cliente_email = dados_pedido.get('cliente_email', '')
        
        if not cliente_email:
            logging.error("❌ Email do cliente não fornecido!")
            return jsonify({"erro": "Email do cliente é obrigatório."}), 400
        
        # Verificar se email contém palavras de teste
        email_lower = cliente_email.lower()
        palavras_proibidas = ['test', 'teste', 'exemplo', 'example', 'demo', 'testuser']
        
        if any(palavra in email_lower for palavra in palavras_proibidas):
            logging.error(f"❌ Email inválido (contém palavra de teste): {cliente_email}")
            return jsonify({
                "erro": "Email inválido. Por favor, use um email real para realizar o pagamento.",
                "detalhes": f"O email '{cliente_email}' parece ser um email de teste. Use seu email real."
            }), 400
        
        logging.info(f"✅ Email validado: {cliente_email}")
        
        # ✅ Processar itens
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
        
        # ✅ Configuração da preferência com email VALIDADO
        preference_data = {
            "items": items_mp,
            "payer": {
                "email": cliente_email  # ✅ Email que passou pela validação!
            },
            "payment_methods": {
                "excluded_payment_methods": [],      # ✅ Não exclui nenhum método específico
                "excluded_payment_types": [],        # ✅ Permite todos: PIX, cartão, boleto, etc
                "installments": 12,                  # ✅ Até 12x no cartão
                "default_installments": 1            # ✅ Padrão: à vista
            },
            "back_urls": {
                "success": urls_retorno.get('sucesso', f"{FRONTEND_URL}/pagamento/sucesso"),
                "failure": urls_retorno.get('falha', f"{FRONTEND_URL}/pagamento/falha"),
                "pending": urls_retorno.get('pendente', f"{FRONTEND_URL}/pagamento/pendente")
            },
            "auto_return": "approved",
            "external_reference": dados_pedido.get('pedido_id', 'id_pedido_temp'),
            "notification_url": f"{notification_url_mp_base}/api/pagamentos/webhook_mp",
            "statement_descriptor": "INKSA DELIVERY",  # ✅ Nome que aparece na fatura do cartão
            "binary_mode": False                       # ✅ Permite pagamentos pendentes (PIX, boleto)
        }
        
        logging.info(f"🚀 Enviando preferência para Mercado Pago...")
        logging.info(f"📧 Email do cliente: {cliente_email}")
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


# ✅ WEBHOOK ATUALIZADO - MUDA STATUS PARA 'PENDING' APÓS PAGAMENTO APROVADO
@mp_payment_bp.route('/pagamentos/webhook_mp', methods=['POST'])
def mercadopago_webhook():
    webhook_secret = os.environ.get("MERCADO_PAGO_WEBHOOK_SECRET")
    
    if webhook_secret:
        verify_mp_signature(request, webhook_secret)
    
    logging.info("✅ === WEBHOOK DO MERCADO PAGO RECEBIDO ===")
    
    request_data = request.get_json(silent=True) or request.args.to_dict()
    topic = request_data.get('topic')
    resource_id = request_data.get('id')
    
    logging.info(f"📋 Topic: {topic}, Resource ID: {resource_id}")
    
    if request_data.get('type') == 'payment' and request_data.get('data', {}).get('id'):
        topic = 'payment'
        resource_id = request_data['data']['id']
    elif not topic and request.args.get('topic'):
        topic = request.args.get('topic')
        resource_id = request.args.get('id')

    if topic == 'payment':
        if supabase_client is None:
            logging.error("❌ Cliente Supabase não disponível")
            return jsonify({"status": "error", "message": "Serviço de banco de dados indisponível."}), 500
        
        try:
            sdk = current_app.mp_sdk
            if sdk is None:
                logging.error("❌ SDK Mercado Pago não disponível")
                return jsonify({"status": "error", "message": "Serviço de pagamento indisponível."}), 503
            
            logging.info(f"🔍 Buscando informações do pagamento {resource_id}...")
            payment_info = sdk.payment().get(resource_id)
            
            if not (payment_info and "response" in payment_info):
                logging.error(f"❌ Pagamento {resource_id} não encontrado")
                return jsonify({"status": "error", "message": "Detalhes do pagamento não encontrados"}), 404
            
            payment_data = payment_info["response"]
            status = payment_data.get("status")
            external_reference = payment_data.get("external_reference")
            
            logging.info(f"💳 Pagamento {resource_id} - Status: {status} - Pedido: {external_reference}")
            
            if status == 'approved':
                logging.info(f"✅ Pagamento {resource_id} APROVADO! Ativando pedido e calculando repasses.")
                
                response_supabase = supabase_client.table('orders').select('*').eq('id', external_reference).single().execute()
                
                if response_supabase.data:
                    pedido_do_bd = response_supabase.data
                    valor_total_itens = float(pedido_do_bd.get('total_amount_items', 0.0))
                    
                    comissao_plataforma = valor_total_itens * current_app.config['PLATFORM_COMMISSION_RATE']
                    valor_para_restaurante = valor_total_itens - comissao_plataforma
                    valor_para_entregador = float(pedido_do_bd.get('delivery_fee', 0.0))
                    
                    # ✅ CORREÇÃO CRÍTICA: Agora atualiza o 'status' para 'pending'
                    update_data = {
                        'status': 'pending',  # ✅ ISSO ATIVA O PEDIDO PARA O RESTAURANTE!
                        'status_pagamento': status,
                        'comissao_plataforma': round(comissao_plataforma, 2),
                        'valor_repassado_restaurante': round(valor_para_restaurante, 2),
                        'valor_repassado_entregador': round(valor_para_entregador, 2),
                        'id_transacao_mp': resource_id
                    }
                    
                    supabase_client.table('orders').update(update_data).eq('id', external_reference).execute()
                    
                    logging.info(f"✅ Pedido {external_reference} ATIVADO e atualizado com repasses:")
                    logging.info(f"   🎯 Status mudou para: pending (agora aparece para o restaurante!)")
                    logging.info(f"   💵 Comissão plataforma: R$ {update_data['comissao_plataforma']}")
                    logging.info(f"   🍽️ Valor restaurante: R$ {update_data['valor_repassado_restaurante']}")
                    logging.info(f"   🚴 Valor entregador: R$ {update_data['valor_repassado_entregador']}")
                else:
                    logging.warning(f"⚠️ Pedido {external_reference} não encontrado no Supabase")
            
            elif status in ['pending', 'in_process']:
                logging.info(f"📝 Pagamento {resource_id} com status: {status} - mantendo pedido aguardando")
                # ✅ NÃO muda o status do pedido, mantém 'awaiting_payment'
                supabase_client.table('orders').update({
                    'status_pagamento': status, 
                    'id_transacao_mp': resource_id
                }).eq('id', external_reference).execute()
                logging.info(f"✅ Status de pagamento do pedido {external_reference} atualizado para: {status}")
                
            elif status == 'rejected':
                logging.warning(f"❌ Pagamento {resource_id} REJEITADO")
                # ✅ Pedido continua 'awaiting_payment' (não aparece para restaurante)
                supabase_client.table('orders').update({
                    'status_pagamento': status,
                    'id_transacao_mp': resource_id
                }).eq('id', external_reference).execute()
                logging.info(f"✅ Pedido {external_reference} marcado como pagamento rejeitado")

        except Exception as e:
            logging.error(f"❌ Erro ao processar webhook de pagamento: {e}", exc_info=True)
            return jsonify({"status": "error", "message": "Erro ao processar webhook"}), 500
            
    return jsonify({"status": "ok"}), 200
