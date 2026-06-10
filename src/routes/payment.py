# src/routes/payment.py - VERSÃO COM LOGS DETALHADOS PARA DIAGNÓSTICO

from flask import Blueprint, request, jsonify, current_app
import mercadopago
from supabase import create_client, Client
import os
import logging
import hmac
import hashlib
import eventlet

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
    """Verifica a assinatura da notificação de webhook do Mercado Pago.
    Retorna True apenas se a assinatura for válida. Retorna False em qualquer falha.
    """
    signature_header = req.headers.get('X-Signature')
    if not signature_header:
        logging.warning("⚠️ Webhook recebido SEM X-Signature — rejeitando")
        return False

    try:
        parts = {p.split('=')[0]: p.split('=')[1] for p in signature_header.split(',')}
        ts = parts.get('ts')
        signature_hash = parts.get('v1')

        if not ts or not signature_hash:
            logging.warning("⚠️ Cabeçalho X-Signature com formato inválido — rejeitando")
            return False

        notification_id = req.args.get('id')
        if not notification_id:
            json_data = req.get_json(silent=True)
            if json_data and 'data' in json_data and 'id' in json_data['data']:
                notification_id = json_data['data']['id']
            else:
                notification_id = 'id_not_found'

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
            logging.warning("⚠️ Assinatura do webhook INVÁLIDA — rejeitando")

        return is_valid

    except Exception as e:
        logging.error(f"❌ Erro ao validar assinatura: {e} — rejeitando por segurança")
        return False


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

        payment_method = dados_pedido.get('payment_method', 'online')
        change_for = float(dados_pedido.get('change_for') or 0)
        logging.info(f"💳 Método de pagamento: {payment_method} | Troco para: {change_for}")

        # ✅ VALIDAÇÃO DO EMAIL — apenas para pagamentos online (não em dinheiro)
        cliente_email = dados_pedido.get('cliente_email', '')
        if payment_method != 'cash':
            if not cliente_email:
                logging.error("❌ Email do cliente não fornecido!")
                return jsonify({"erro": "Email do cliente é obrigatório."}), 400
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
            'delivery_id': None,
            'status': 'pending' if payment_method == 'cash' else 'awaiting_payment',
            'items': dados_pedido.get('itens', []),
            'total_amount_items': dados_pedido.get('total_amount_items', 0),
            'delivery_fee': dados_pedido.get('delivery_fee', 0),
            'total_amount': dados_pedido.get('total_amount', 0),
            'delivery_address': dados_pedido.get('delivery_address', ''),
            'notes': dados_pedido.get('notes', ''),
            'client_latitude': dados_pedido.get('client_latitude'),
            'client_longitude': dados_pedido.get('client_longitude'),
            'delivery_distance_km': dados_pedido.get('delivery_distance_km'),
            'payment_method': payment_method,
            'change_for': change_for,
            'created_at': datetime.utcnow().isoformat(),
            'updated_at': datetime.utcnow().isoformat()
        }
        if payment_method == 'cash':
            order_data['status_pagamento'] = 'pending_cash'
        
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
        
        # --- VUL-08: Revalidação de cupom no backend ---
        coupon_code = dados_pedido.get('coupon_code', '').strip()
        backend_discount = 0.0
        if coupon_code:
            try:
                coupon_result = supabase_client.table('coupons').select('*').eq('code', coupon_code.upper()).eq('is_active', True).execute()
                if coupon_result.data:
                    coupon = coupon_result.data[0]
                    order_subtotal = float(dados_pedido.get('total_amount_items', 0))
                    min_order = float(coupon.get('min_order_amount') or 0)
                    if order_subtotal >= min_order:
                        if coupon.get('discount_type') == 'percentage':
                            backend_discount = round(order_subtotal * float(coupon.get('discount_value', 0)) / 100, 2)
                        else:
                            backend_discount = round(float(coupon.get('discount_value', 0)), 2)
                        # Cap discount to subtotal
                        backend_discount = min(backend_discount, order_subtotal)
                        logging.info(f"✅ Cupom '{coupon_code}' validado no backend — desconto: R${backend_discount:.2f}")
                    else:
                        logging.warning(f"⚠️ Cupom '{coupon_code}' inválido: pedido mínimo não atingido")
                else:
                    logging.warning(f"⚠️ Cupom '{coupon_code}' não encontrado ou inativo — desconto ignorado")
            except Exception as _coupon_err:
                logging.warning(f"⚠️ Falha ao validar cupom no backend: {_coupon_err} — desconto ignorado")

        # Corrigir total_amount com desconto validado pelo backend
        if backend_discount > 0:
            raw_total = float(dados_pedido.get('total_amount_items', 0)) + float(dados_pedido.get('delivery_fee', 0))
            corrected_total = max(0.0, raw_total - backend_discount)
            order_data['total_amount'] = round(corrected_total, 2)
            logging.info(f"✅ total_amount corrigido para R${corrected_total:.2f} (desconto backend: R${backend_discount:.2f})")
        # --- Fim VUL-08 ---

        # ✅ Pedido em dinheiro: não passa pelo MP
        if payment_method == 'cash':
            logging.info(f"💵 Pedido em dinheiro {pedido_id} — sem processamento MP.")
            return jsonify({
                'mensagem': 'Pedido em dinheiro criado com sucesso!',
                'pedido_id': pedido_id,
                'payment_method': 'cash',
            }), 200

        # ✅ PASSO 2: Processar itens para o Mercado Pago
        items_mp = []
        items_from_request = dados_pedido.get('itens', [])

        logging.info(f"📋 Processando e validando preços de {len(items_from_request)} itens...")

        for idx, item in enumerate(items_from_request):
            try:
                quantidade = int(item.get('quantity', 1))
                titulo = str(item.get('title', f'Item {idx + 1}'))
                menu_item_id = item.get('menu_item_id')

                # Itens SEM menu_item_id (ex: Taxa de Entrega) — confiar no valor enviado
                if not menu_item_id:
                    preco = float(item.get('unit_price', 0))
                    if preco > 0 and quantidade > 0:
                        items_mp.append({'title': titulo, 'quantity': quantidade, 'unit_price': preco})
                    continue

                # Itens COM menu_item_id — validar preço contra o banco
                db_result = supabase_client.table('menu_items').select('price, name').eq('id', menu_item_id).execute()
                if not db_result.data:
                    logging.error(f"❌ Item {menu_item_id} não encontrado no banco — pedido rejeitado")
                    supabase_client.table('orders').delete().eq('id', pedido_id).execute()
                    return jsonify({"erro": f"Item de cardápio inválido: {titulo}"}), 400

                preco_real = float(db_result.data[0]['price'])
                preco_frontend = float(item.get('unit_price', 0))

                if abs(preco_real - preco_frontend) > 0.01:
                    logging.error(f"❌ Preço inválido para '{titulo}': frontend={preco_frontend}, banco={preco_real}")
                    supabase_client.table('orders').delete().eq('id', pedido_id).execute()
                    return jsonify({
                        "erro": "Preço dos itens inválido. Recarregue a página e tente novamente.",
                        "item": titulo
                    }), 400

                if preco_real > 0 and quantidade > 0:
                    items_mp.append({'title': titulo, 'quantity': quantidade, 'unit_price': preco_real})
                    logging.info(f"✅ Item '{titulo}' validado: R${preco_real:.2f} × {quantidade}")

            except (ValueError, TypeError) as e:
                logging.error(f"❌ Erro ao processar item {idx + 1}: {e}")
                continue

        # Aplicar desconto de cupom nos itens do MP (adiciona item negativo)
        if backend_discount > 0:
            items_mp.append({'title': f'Desconto Cupom {coupon_code}', 'quantity': 1, 'unit_price': -backend_discount})
            logging.info(f"✅ Desconto de cupom R${backend_discount:.2f} adicionado aos itens MP")

        if not items_mp or all(i['unit_price'] <= 0 for i in items_mp if i.get('title') != f'Desconto Cupom {coupon_code}'):
            logging.error("❌ Nenhum item válido para processar!")
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


# ─── PAGAMENTO TRANSPARENTE COM CARTÃO (in-app, sem redirecionar) ────────────
def _validar_itens_e_total(items_from_request, delivery_fee, coupon_code, subtotal_items):
    """Revalida precos no banco e calcula o total no servidor (nao confia no front).
    Retorna (total_seguro, subtotal_validado, desconto). Lanca ValueError se invalido.
    """
    subtotal = 0.0
    for item in items_from_request:
        menu_item_id = item.get('menu_item_id')
        quantidade = int(item.get('quantity', 1))
        if quantidade <= 0:
            continue
        if not menu_item_id:
            # Itens sem id (ex.: taxa) sao ignorados no subtotal de produtos
            continue
        db = supabase_client.table('menu_items').select('price').eq('id', menu_item_id).execute()
        if not db.data:
            raise ValueError("Item de cardápio inválido.")
        preco_real = float(db.data[0]['price'])
        subtotal += preco_real * quantidade

    # Desconto de cupom validado no backend
    desconto = 0.0
    if coupon_code:
        cr = supabase_client.table('coupons').select('*').eq('code', coupon_code.upper()).eq('is_active', True).execute()
        if cr.data:
            c = cr.data[0]
            if subtotal >= float(c.get('min_order_amount') or 0):
                if c.get('discount_type') == 'percentage':
                    desconto = round(subtotal * float(c.get('discount_value', 0)) / 100, 2)
                else:
                    desconto = round(float(c.get('discount_value', 0)), 2)
                desconto = min(desconto, subtotal)

    total = max(0.0, round(subtotal + float(delivery_fee or 0) - desconto, 2))
    return total, round(subtotal, 2), desconto


@mp_payment_bp.route('/pagamentos/processar_cartao', methods=['POST'])
def processar_pagamento_cartao():
    """Processa pagamento com cartao via token do MP Bricks (sem redirecionar)."""
    logging.info("💳 === PROCESSANDO PAGAMENTO COM CARTÃO (transparente) ===")
    try:
        sdk = current_app.mp_sdk
        if sdk is None:
            return jsonify({"erro": "Serviço de pagamento indisponível."}), 503
        if not supabase_client:
            return jsonify({"erro": "Serviço de banco indisponível."}), 500

        d = request.json or {}
        token = d.get('token')
        payment_method_id = d.get('payment_method_id')
        installments = int(d.get('installments') or 1)
        issuer_id = d.get('issuer_id')
        cliente_email = (d.get('cliente_email') or d.get('payer_email') or '').strip()
        payer_identification = d.get('payer_identification')  # {type, number} opcional

        if not token or not payment_method_id:
            return jsonify({"erro": "Dados do cartão incompletos."}), 400
        if not cliente_email:
            return jsonify({"erro": "Email do cliente é obrigatório."}), 400

        items_req = d.get('itens', [])
        try:
            total_seguro, subtotal_validado, desconto = _validar_itens_e_total(
                items_req, d.get('delivery_fee', 0), (d.get('coupon_code') or '').strip(), d.get('total_amount_items', 0)
            )
        except ValueError as ve:
            return jsonify({"erro": str(ve)}), 400

        if total_seguro <= 0:
            return jsonify({"erro": "Valor do pedido inválido."}), 400

        # 1) Cria o pedido (aguardando pagamento)
        import uuid
        from datetime import datetime
        pedido_id = d.get('pedido_id') or str(uuid.uuid4())
        order_data = {
            'id': pedido_id,
            'client_id': d.get('client_id'),
            'restaurant_id': d.get('restaurant_id'),
            'delivery_id': None,
            'status': 'awaiting_payment',
            'items': items_req,
            'total_amount_items': subtotal_validado,
            'delivery_fee': d.get('delivery_fee', 0),
            'total_amount': total_seguro,
            'delivery_address': d.get('delivery_address', ''),
            'notes': d.get('notes', ''),
            'client_latitude': d.get('client_latitude'),
            'client_longitude': d.get('client_longitude'),
            'delivery_distance_km': d.get('delivery_distance_km'),
            'payment_method': payment_method_id,
            'created_at': datetime.utcnow().isoformat(),
            'updated_at': datetime.utcnow().isoformat(),
        }
        ins = supabase_client.table('orders').insert(order_data).execute()
        if not ins.data:
            return jsonify({"erro": "Erro ao criar pedido."}), 500

        # 2) Cria o pagamento no MP com o token do cartao
        base = os.environ.get("MERCADO_PAGO_WEBHOOK_URL")
        payment_payload = {
            "transaction_amount": float(total_seguro),
            "token": token,
            "description": "Pedido Inksa Delivery",
            "installments": installments,
            "payment_method_id": payment_method_id,
            "external_reference": pedido_id,
            "statement_descriptor": "INKSA DELIVERY",
            "payer": {"email": cliente_email},
        }
        if issuer_id:
            payment_payload["issuer_id"] = issuer_id
        if base:
            payment_payload["notification_url"] = f"{base}/api/pagamentos/webhook_mp"
        if payer_identification and payer_identification.get('number'):
            payment_payload["payer"]["identification"] = payer_identification

        result = sdk.payment().create(payment_payload)
        resp = result.get("response", {}) if isinstance(result, dict) else {}
        status = resp.get("status")
        payment_id = resp.get("id")
        logging.info(f"💳 Pagamento MP criado: id={payment_id} status={status}")

        if status == 'approved':
            rate = current_app.config.get('PLATFORM_COMMISSION_RATE', 0.15)
            comissao = round(subtotal_validado * rate, 2)
            supabase_client.table('orders').update({
                'status': 'pending',  # ativa o pedido para o restaurante
                'status_pagamento': 'approved',
                'comissao_plataforma': comissao,
                'valor_repassado_restaurante': round(subtotal_validado - comissao, 2),
                'valor_repassado_entregador': round(float(d.get('delivery_fee', 0)), 2),
                'id_transacao_mp': str(payment_id),
            }).eq('id', pedido_id).execute()
            return jsonify({"status": "approved", "pedido_id": pedido_id, "payment_id": payment_id}), 200

        if status in ('in_process', 'pending'):
            supabase_client.table('orders').update({
                'status_pagamento': status, 'id_transacao_mp': str(payment_id),
            }).eq('id', pedido_id).execute()
            return jsonify({"status": status, "pedido_id": pedido_id, "payment_id": payment_id}), 200

        # rejeitado: cancela o pedido
        motivo = resp.get("status_detail", "rejected")
        supabase_client.table('orders').update({
            'status': 'cancelled', 'status_pagamento': 'rejected',
            'id_transacao_mp': str(payment_id) if payment_id else None,
            'cancellation_reason': f'payment_rejected:{motivo}',
        }).eq('id', pedido_id).execute()
        return jsonify({"status": "rejected", "detail": motivo, "pedido_id": pedido_id}), 402

    except Exception as e:
        logging.error(f"❌ Erro ao processar pagamento com cartão: {e}", exc_info=True)
        return jsonify({"erro": "Erro interno ao processar pagamento."}), 500


# ✅ WEBHOOK COM LOGS DETALHADOS PARA DIAGNÓSTICO
@mp_payment_bp.route('/pagamentos/webhook_mp', methods=['POST'])
def mercadopago_webhook():
    webhook_secret = os.environ.get("MERCADO_PAGO_WEBHOOK_SECRET")
    
    if webhook_secret:
        if not verify_mp_signature(request, webhook_secret):
            logging.warning("⚠️ Webhook rejeitado: assinatura inválida")
            return jsonify({'error': 'Assinatura inválida'}), 401

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
                # Verificação de idempotência: evitar processar o mesmo pagamento duas vezes
                try:
                    existing = supabase_client.table('orders').select('id').eq('id_transacao_mp', resource_id).execute()
                    if existing.data:
                        logging.info(f"⚠️ Pagamento {resource_id} já processado anteriormente — ignorando duplicata")
                        return jsonify({'status': 'already_processed'}), 200
                except Exception as _idem_err:
                    logging.warning(f"⚠️ Falha na verificação de idempotência: {_idem_err} — continuando")

                logging.info(f"✅ Pagamento {resource_id} APROVADO! Iniciando atualização do pedido...")

                # 🔍 DIAGNÓSTICO: Buscar o pedido ANTES de atualizar (COM RETRY)
                logging.info(f"🔍 PASSO 1: Buscando pedido {external_reference} no Supabase...")
                response_supabase = supabase_client.table('orders').select('*').eq('id', external_reference).execute()
                
                # 🔄 RETRY: Se não encontrar, aguarda e tenta novamente (race condition)
                if not response_supabase.data:
                    logging.warning(f"⚠️ Pedido não encontrado na primeira tentativa. Aguardando 3 segundos...")
                    import time
                    eventlet.sleep(3)
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

                    valor_para_entregador = float(pedido_do_bd.get('delivery_fee', 0.0))  # entregador recebe 100% do frete
                    
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
                    eventlet.sleep(3)
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
                    eventlet.sleep(3)
                    logging.info(f"🔄 RETRY: Verificando pedido {external_reference} novamente...")
                    check_order = supabase_client.table('orders').select('id').eq('id', external_reference).execute()
                
                if check_order.data:
                    supabase_client.table('orders').update({
                        'status': 'cancelled',
                        'status_pagamento': 'rejected',
                        'id_transacao_mp': resource_id,
                        'cancellation_reason': 'payment_rejected'
                    }).eq('id', external_reference).execute()
                    logging.info(f"✅ Pedido {external_reference} cancelado — pagamento rejeitado")
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
