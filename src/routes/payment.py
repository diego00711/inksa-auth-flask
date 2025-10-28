# src/routes/payment.py - VERSÃO COM LOGS DETALHADOS PARA DIAGNÓSTICO

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
# ✅ Tentar ambos os nomes possíveis da variável
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_SERVICE_KEY")
supabase_client = None

if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    logging.error("ERRO: SUPABASE_URL ou SUPABASE_SERVICE_ROLE_KEY não configurados.")
    logging.error(f"SUPABASE_URL presente: {bool(SUPABASE_URL)}")
    logging.error(f"SUPABASE_SERVICE_ROLE_KEY presente: {bool(os.environ.get('SUPABASE_SERVICE_ROLE_KEY'))}")
    logging.error(f"SUPABASE_SERVICE_KEY presente: {bool(os.environ.get('SUPABASE_SERVICE_KEY'))}")
else:
    try:
        supabase_client: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
        logging.info("Cliente Supabase (payment.py) inicializado com sucesso.")
        logging.info(f"🔑 Usando chave que começa com: {SUPABASE_SERVICE_KEY[:20]}...")
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
        
        # 🆕 PASSO 1: CRIAR O PEDIDO NO BANCO PRIMEIRO!
        import uuid
        from datetime import datetime
        
        pedido_id = dados_pedido.get('pedido_id')
        
        # Se não tem ID, cria um novo
        if not pedido_id:
            pedido_id = str(uuid.uuid4())
            logging.info(f"🆔 Gerando novo ID de pedido: {pedido_id}")
        else:
            logging.info(f"🆔 Usando ID de pedido existente: {pedido_id}")
        
        # Preparar dados do pedido para o banco
        order_data = {
            'id': pedido_id,
            'client_id': dados_pedido.get('client_id'),
            'restaurant_id': dados_pedido.get('restaurant_id'),
            'delivery_id': None,  # ✅ NULL explícito - entregador será atribuído depois
            'status': 'awaiting_payment',  # ✅ Status correto
            'items': dados_pedido.get('itens', []),
            'total_amount_items': dados_pedido.get('total_amount_items', 0),
            'delivery_fee': dados_pedido.get('delivery_fee', 0),
            'total_amount': dados_pedido.get('total_amount', 0),
            'delivery_address': dados_pedido.get('delivery_address', ''),
            'notes': dados_pedido.get('notes', ''),
            'client_latitude': dados_pedido.get('client_latitude'),
            'client_longitude': dados_pedido.get('client_longitude'),
            'delivery_distance_km': dados_pedido.get('delivery_distance_km'),
            'created_at': datetime.utcnow().isoformat(),
            'updated_at': datetime.utcnow().isoformat()
        }
        
        logging.info(f"💾 PASSO 1: Criando pedido {pedido_id} no banco...")
        logging.info(f"🔑 Cliente Supabase configurado: {bool(supabase_client)}")
        
        try:
            # 🔐 Usar o cliente Supabase com service_role (bypassa RLS)
            if not supabase_client:
                raise Exception("Cliente Supabase não inicializado! Verifique as variáveis de ambiente.")
            
            logging.info(f"📤 Enviando dados para o Supabase: {order_data}")
            result = supabase_client.table('orders').insert(order_data).execute()
            
            logging.info(f"📥 Resposta do Supabase: {result}")
            
            if not result.data:
                raise Exception("Nenhum dado retornado após insert - possível erro de RLS")
                
            logging.info(f"✅ Pedido {pedido_id} criado com sucesso no banco!")
            logging.info(f"📊 Dados do pedido inserido: {result.data}")
            
        except Exception as e:
            error_message = str(e)
            logging.error(f"❌ Erro ao criar pedido no banco: {error_message}")
            logging.error(f"❌ Tipo do erro: {type(e)}")
            
            # Mensagem mais específica para erro de RLS
            if '42501' in error_message or 'row-level security' in error_message.lower():
                logging.error("🔒 ERRO DE RLS! A SUPABASE_SERVICE_ROLE_KEY pode não estar configurada corretamente!")
                logging.error("🔧 Verifique se você está usando a SERVICE ROLE KEY (não a anon key)!")
                return jsonify({
                    "erro": "Erro de permissão ao criar pedido.",
                    "detalhes": "Configure a SUPABASE_SERVICE_ROLE_KEY nas variáveis de ambiente."
                }), 500
                
            return jsonify({"erro": "Erro ao criar pedido no banco de dados."}), 500
        
        # ✅ PASSO 2: Processar itens para o Mercado Pago
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
            # Deletar pedido que foi criado
            supabase_client.table('orders').delete().eq('id', pedido_id).execute()
            return jsonify({"erro": "A lista de itens está vazia ou todos os itens têm valor zero."}), 400
        
        logging.info(f"✅ Total de itens válidos: {len(items_mp)}")
        
        # ✅ PASSO 3: Criar preferência no Mercado Pago
        urls_retorno = dados_pedido.get('urls_retorno', {})
        FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:3000")
        notification_url_mp_base = os.environ.get("MERCADO_PAGO_WEBHOOK_URL")
        
        if not notification_url_mp_base:
            logging.error("❌ URL de notificação do Mercado Pago não configurada!")
            # Deletar pedido que foi criado
            supabase_client.table('orders').delete().eq('id', pedido_id).execute()
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
            "external_reference": pedido_id,  # ✅ ID do pedido que JÁ EXISTE no banco!
            "notification_url": f"{notification_url_mp_base}/api/pagamentos/webhook_mp",
            "statement_descriptor": "INKSA DELIVERY",  # ✅ Nome que aparece na fatura do cartão
            "binary_mode": False                       # ✅ Permite pagamentos pendentes (PIX, boleto)
        }
        
        logging.info(f"🚀 PASSO 2: Enviando preferência para Mercado Pago...")
        logging.info(f"📧 Email do cliente: {cliente_email}")
        logging.info(f"🆔 ID do pedido (external_reference): {pedido_id}")
        
        preference_response = sdk.preference().create(preference_data)
        
        if "response" not in preference_response or preference_response.get("status", 200) >= 400:
            erro_detalhes = preference_response.get("response", {}).get("message", "Erro desconhecido do MP.")
            logging.error(f"❌ Mercado Pago recusou a criação: {erro_detalhes}")
            logging.error(f"❌ Resposta completa do MP: {preference_response}")
            # Deletar pedido que foi criado
            supabase_client.table('orders').delete().eq('id', pedido_id).execute()
            return jsonify({
                "erro": "O Mercado Pago recusou a criação do pagamento.", 
                "detalhes": erro_detalhes
            }), 400
        
        preference = preference_response["response"]
        logging.info(f"✅ Preferência criada com sucesso! ID: {preference['id']}")
        logging.info(f"✅ Link de checkout: {preference['init_point']}")
        logging.info(f"✅ Pedido {pedido_id} criado NO BANCO e preferência MP vinculada!")
        
        return jsonify({
            "mensagem": "Preferência de pagamento criada com sucesso!",
            "checkout_link": preference["init_point"],
            "preference_id": preference["id"],
            "pedido_id": pedido_id  # ✅ Retorna o ID do pedido criado
        }), 200
        
    except Exception as e:
        logging.error(f"❌ ERRO CRÍTICO ao criar preferência de pagamento: {e}", exc_info=True)
        return jsonify({"erro": "Erro interno ao processar pagamento."}), 500


# ✅ WEBHOOK COM LOGS DETALHADOS PARA DIAGNÓSTICO
@mp_payment_bp.route('/pagamentos/webhook_mp', methods=['POST'])
def mercadopago_webhook():
    webhook_secret = os.environ.get("MERCADO_PAGO_WEBHOOK_SECRET")
    
    if webhook_secret:
        verify_mp_signature(request, webhook_secret)
    
    logging.info("=" * 80)
    logging.info("✅ === WEBHOOK DO MERCADO PAGO RECEBIDO ===")
    logging.info("=" * 80)
    
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
            
            logging.info("=" * 80)
            logging.info(f"💳 DADOS DO PAGAMENTO:")
            logging.info(f"   ID: {resource_id}")
            logging.info(f"   Status: {status}")
            logging.info(f"   External Reference (ID do Pedido): {external_reference}")
            logging.info(f"   Tipo: {type(external_reference)}")
            logging.info("=" * 80)
            
            if status == 'approved':
                logging.info(f"✅ Pagamento {resource_id} APROVADO! Iniciando atualização do pedido...")
                
                # 🔍 DIAGNÓSTICO: Buscar o pedido ANTES de atualizar (COM RETRY)
                logging.info(f"🔍 PASSO 1: Buscando pedido {external_reference} no Supabase...")
                response_supabase = supabase_client.table('orders').select('*').eq('id', external_reference).execute()
                
                # 🔄 RETRY: Se não encontrar, aguarda e tenta novamente (race condition)
                if not response_supabase.data:
                    logging.warning(f"⚠️ Pedido não encontrado na primeira tentativa. Aguardando 3 segundos...")
                    import time
                    time.sleep(3)
                    logging.info(f"🔄 RETRY: Buscando pedido {external_reference} novamente...")
                    response_supabase = supabase_client.table('orders').select('*').eq('id', external_reference).execute()
                
                logging.info(f"📊 Resposta do Supabase:")
                logging.info(f"   Data: {response_supabase.data}")
                logging.info(f"   Count: {response_supabase.count if hasattr(response_supabase, 'count') else 'N/A'}")
                
                if response_supabase.data:
                    pedido_do_bd = response_supabase.data[0] if isinstance(response_supabase.data, list) else response_supabase.data
                    
                    logging.info(f"✅ Pedido ENCONTRADO no banco!")
                    logging.info(f"   ID do pedido no banco: {pedido_do_bd.get('id')}")
                    logging.info(f"   Status atual no banco: {pedido_do_bd.get('status')}")
                    logging.info(f"   Status pagamento atual: {pedido_do_bd.get('status_pagamento')}")
                    
                    valor_total_itens = float(pedido_do_bd.get('total_amount_items', 0.0))
                    
                    comissao_plataforma = valor_total_itens * current_app.config['PLATFORM_COMMISSION_RATE']
                    valor_para_restaurante = valor_total_itens - comissao_plataforma
                    valor_para_entregador = float(pedido_do_bd.get('delivery_fee', 0.0))
                    
                    # ✅ DADOS QUE SERÃO ATUALIZADOS
                    update_data = {
                        'status': 'pending',  # ✅ ISSO ATIVA O PEDIDO PARA O RESTAURANTE!
                        'status_pagamento': status,
                        'comissao_plataforma': round(comissao_plataforma, 2),
                        'valor_repassado_restaurante': round(valor_para_restaurante, 2),
                        'valor_repassado_entregador': round(valor_para_entregador, 2),
                        'id_transacao_mp': resource_id
                    }
                    
                    logging.info("=" * 80)
                    logging.info(f"🔄 PASSO 2: Atualizando pedido com os seguintes dados:")
                    for key, value in update_data.items():
                        logging.info(f"   {key}: {value}")
                    logging.info("=" * 80)
                    
                    # 🎯 ATUALIZAÇÃO
                    update_response = supabase_client.table('orders').update(update_data).eq('id', external_reference).execute()
                    
                    logging.info(f"📊 Resposta da atualização:")
                    logging.info(f"   Data: {update_response.data}")
                    logging.info(f"   Count: {update_response.count if hasattr(update_response, 'count') else 'N/A'}")
                    
                    # 🔍 VERIFICAÇÃO: Buscar novamente para confirmar
                    logging.info(f"🔍 PASSO 3: Verificando se a atualização funcionou...")
                    verify_response = supabase_client.table('orders').select('status, status_pagamento, id_transacao_mp').eq('id', external_reference).execute()
                    
                    if verify_response.data:
                        dados_verificacao = verify_response.data[0] if isinstance(verify_response.data, list) else verify_response.data
                        logging.info("=" * 80)
                        logging.info(f"✅ VERIFICAÇÃO PÓS-UPDATE:")
                        logging.info(f"   Status no banco AGORA: {dados_verificacao.get('status')}")
                        logging.info(f"   Status pagamento AGORA: {dados_verificacao.get('status_pagamento')}")
                        logging.info(f"   ID transação MP: {dados_verificacao.get('id_transacao_mp')}")
                        logging.info("=" * 80)
                        
                        if dados_verificacao.get('status') == 'pending':
                            logging.info("🎉 SUCESSO TOTAL! Pedido atualizado corretamente!")
                        else:
                            logging.error(f"⚠️ PROBLEMA! Status esperado: 'pending', mas está: {dados_verificacao.get('status')}")
                    else:
                        logging.error("❌ ERRO: Não conseguiu verificar o pedido após update!")
                    
                else:
                    logging.error("=" * 80)
                    logging.error(f"❌ PROBLEMA CRÍTICO: Pedido {external_reference} NÃO ENCONTRADO no Supabase!")
                    logging.error(f"   Tipo do external_reference: {type(external_reference)}")
                    logging.error(f"   Valor do external_reference: '{external_reference}'")
                    logging.error("=" * 80)
                    
                    # 🔍 Tentar buscar pedidos similares
                    logging.info("🔍 Buscando pedidos recentes para diagnóstico...")
                    recent_orders = supabase_client.table('orders').select('id, status, status_pagamento').limit(5).execute()
                    if recent_orders.data:
                        logging.info("📋 Últimos 5 pedidos no banco:")
                        for order in recent_orders.data:
                            logging.info(f"   - ID: {order.get('id')} | Status: {order.get('status')} | Pagamento: {order.get('status_pagamento')}")
            
            elif status in ['pending', 'in_process']:
                logging.info(f"📝 Pagamento {resource_id} com status: {status} - mantendo pedido aguardando")
                
                # 🔄 Verificar se pedido existe (com retry para race condition)
                check_order = supabase_client.table('orders').select('id').eq('id', external_reference).execute()
                if not check_order.data:
                    logging.warning(f"⚠️ Pedido não encontrado. Aguardando 3 segundos...")
                    import time
                    time.sleep(3)
                    logging.info(f"🔄 RETRY: Verificando pedido {external_reference} novamente...")
                    check_order = supabase_client.table('orders').select('id').eq('id', external_reference).execute()
                
                if check_order.data:
                    supabase_client.table('orders').update({
                        'status_pagamento': status, 
                        'id_transacao_mp': resource_id
                    }).eq('id', external_reference).execute()
                    logging.info(f"✅ Status de pagamento do pedido {external_reference} atualizado para: {status}")
                else:
                    logging.error(f"❌ Pedido {external_reference} não encontrado mesmo após retry!")
                
            elif status == 'rejected':
                logging.warning(f"❌ Pagamento {resource_id} REJEITADO")
                
                # 🔄 Verificar se pedido existe (com retry para race condition)
                check_order = supabase_client.table('orders').select('id').eq('id', external_reference).execute()
                if not check_order.data:
                    logging.warning(f"⚠️ Pedido não encontrado. Aguardando 3 segundos...")
                    import time
                    time.sleep(3)
                    logging.info(f"🔄 RETRY: Verificando pedido {external_reference} novamente...")
                    check_order = supabase_client.table('orders').select('id').eq('id', external_reference).execute()
                
                if check_order.data:
                    supabase_client.table('orders').update({
                        'status_pagamento': status,
                        'id_transacao_mp': resource_id
                    }).eq('id', external_reference).execute()
                    logging.info(f"✅ Pedido {external_reference} marcado como pagamento rejeitado")
                else:
                    logging.error(f"❌ Pedido {external_reference} não encontrado mesmo após retry!")

        except Exception as e:
            logging.error("=" * 80)
            logging.error(f"❌ ERRO CRÍTICO ao processar webhook de pagamento: {e}", exc_info=True)
            logging.error("=" * 80)
            return jsonify({"status": "error", "message": "Erro ao processar webhook"}), 500
    
    logging.info("=" * 80)
    logging.info("✅ Webhook processado - retornando 200 OK")
    logging.info("=" * 80)
    return jsonify({"status": "ok"}), 200
