# src/routes/payment.py - VERSÃO COM LOGS DETALHADOS PARA DIAGNÓSTICO

from flask import Blueprint, request, jsonify, current_app
import mercadopago
from supabase import create_client, Client
import os
import re
import logging
import hmac
import hashlib
import gevent
import sentry_sdk
from mercadopago.config import RequestOptions

# Configuração do logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Helpers de cálculo (admin-configuráveis via platform_settings)
from ..utils.platform_settings import calculate_courier_payout, calculate_platform_commission
from ..utils.helpers import get_user_id_from_token, supabase_admin
from .orders import generate_verification_code
from ..utils.coupons import evaluate_coupon, consume_coupon
from ..utils import asaas
from ..utils.gateway import payment_provider
from src.extensions import limiter

# Criação do Blueprint
mp_payment_bp = Blueprint('mp_payment_bp', __name__)

# Cliente Supabase: reusa o supabase_admin compartilhado (helpers), NAO cria um
# proprio. Antes, este arquivo montava seu proprio cliente lendo
# SUPABASE_SERVICE_ROLE_KEY *antes* de SUPABASE_SERVICE_KEY. No Render essas duas
# envs divergem: SUPABASE_SERVICE_KEY tem a service_role correta (o resto do
# backend usa e funciona), mas SUPABASE_SERVICE_ROLE_KEY tem uma chave que NAO
# bypassa RLS. Resultado: o INSERT do pedido batia em "violates row-level
# security policy for table orders" (500) so aqui, no fluxo de pagamento,
# enquanto todo o resto funcionava. Usar o mesmo cliente elimina a divergencia.
supabase_client = supabase_admin
if supabase_client is None:
    logging.error("ERRO: supabase_admin não inicializado (ver SUPABASE_SERVICE_KEY em helpers).")

_SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")


def _checar_area_entrega(restaurant_id, cli_lat, cli_lng):
    """Recusa pedido fora da área da loja. Devolve (fora, mensagem).

    Fail-open em erro de leitura ou coordenada ausente: a função existe pra
    barrar a distância ABSURDA, não pra derrubar checkout por cadastro
    incompleto. Quem trata restaurante sem coordenadas é a calculadora, que já
    cobra taxa base e sinaliza.
    """
    if not restaurant_id or cli_lat is None or cli_lng is None:
        return False, None
    try:
        from ..utils.area_entrega import verificar_area

        r = supabase_client.table('restaurant_profiles').select(
            'latitude, longitude, delivery_type, own_delivery_radius_km, restaurant_name'
        ).eq('id', str(restaurant_id)).limit(1).execute()
        if not r.data:
            return False, None
        loja = r.data[0]
        ok, area = verificar_area(
            loja.get('latitude'), loja.get('longitude'), cli_lat, cli_lng,
            delivery_type=loja.get('delivery_type') or 'platform',
            own_delivery_radius_km=loja.get('own_delivery_radius_km'),
            nome_loja=loja.get('restaurant_name'),
        )
        if not ok:
            logging.warning("Pedido recusado por área: %s km (raio %s km) — loja %s",
                            area["distancia_km"], area["raio_km"], restaurant_id)
            return True, area["message"]
        return False, None
    except Exception:
        logging.warning("Checagem de área falhou — deixando passar", exc_info=True)
        return False, None

# Piso de cobrança do Asaas. É regra DELES, não nossa — vale pra PIX, débito e
# crédito igualmente. Fica em env var caso mudem: ASAAS_VALOR_MINIMO.
ASAAS_VALOR_MINIMO = float(os.environ.get("ASAAS_VALOR_MINIMO", "5.00"))


def _force_service_role():
    """Re-fixa a service_role no PostgREST do cliente admin antes de escrever.

    O cliente supabase-py é compartilhado entre requisições; se outra rota fez
    sign_in de usuário (ex.: /auth/login), a sessão pode 'vazar' e o PostgREST
    passa a mandar o JWT do usuário em vez da service_role. Aí o INSERT do pedido
    bate na RLS: orders.client_id é o id do PERFIL, mas a policy de usuário exige
    auth.uid() = client_id (o auth.uid() é o user_id), então só a service_role
    consegue (policy orders_service_role_all). Isto garante esse bypass.
    """
    try:
        if _SUPABASE_SERVICE_KEY and supabase_client is not None:
            supabase_client.postgrest.auth(_SUPABASE_SERVICE_KEY)
    except Exception as e:
        logging.warning(f"Não foi possível re-fixar a service_role no PostgREST: {e}")


@mp_payment_bp.before_request
def _pin_service_role():
    """Antes de QUALQUER rota de pagamento (checkout, cartão e os webhooks
    MP/Asaas), garante a service_role no PostgREST. Todas escrevem em 'orders'
    e não podem cair na RLS (42501) por sessão poluída — em especial os webhooks,
    que confirmam o pagamento e precisam marcar o pedido como pago."""
    _force_service_role()


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


def _get_authenticated_client_profile_id():
    """Valida o token e resolve o client_profiles.id de quem esta autenticado.
    Retorna (client_profile_id, None) em caso de sucesso, ou (None, response_de_erro).
    Nunca confia em client_id vindo do corpo da requisicao -- previne que qualquer
    pessoa com a URL crie pedidos/cobrancas em nome de outro cliente.
    """
    user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
    if error:
        return None, error
    if user_type != 'client':
        return None, (jsonify({"erro": "Apenas clientes podem criar pedidos."}), 403)

    profile_result = supabase_client.table('client_profiles').select('id').eq('user_id', user_id).execute()
    if not profile_result.data:
        return None, (jsonify({"erro": "Perfil de cliente não encontrado."}), 404)
    return profile_result.data[0]['id'], None


def _restaurant_is_closed(restaurant_id) -> bool:
    """True se o restaurante existe e está FECHADO (bloquear o pedido).

    A tela do cliente só mostra o selo aberto/fechado na listagem — ela não
    impede o checkout, e o restaurante pode ter fechado (manual, por horário
    ou por falta de heartbeat) DEPOIS que o carrinho foi montado. Esta é a
    trava autoritativa no servidor.

    Fail-open: se não der pra checar (erro transitório) devolve False e deixa o
    pedido passar — melhor o restaurante recusar um pedido do que quebrar o
    checkout de todo mundo por um hiccup de banco."""
    if not restaurant_id:
        return False
    try:
        r = (supabase_client.table('restaurant_profiles')
             .select('is_open').eq('id', str(restaurant_id)).single().execute())
        return (r.data or {}).get('is_open') is False
    except Exception as e:
        logging.warning(f"⚠️ Não foi possível checar is_open do restaurante {restaurant_id}: {e}")
        return False


def _excede_limite_de_itens(restaurant_id, itens):
    """(True, mensagem) quando o pedido passa do limite de unidades da loja.

    Pensado pra mercado/supermercado: uma compra grande não cabe numa moto, e
    quem conhece o próprio catálogo é a loja. `max_order_items` NULL = sem
    limite, que é o caso de todo mundo hoje.

    Fail-open igual à trava de loja fechada: se não der pra checar, deixa
    passar — melhor a loja recusar um pedido do que derrubar o checkout de
    todo mundo por um hiccup de banco.
    """
    if not restaurant_id:
        return False, None
    try:
        unidades = sum(int(i.get('quantity') or 0) for i in (itens or []))
        if unidades <= 0:
            return False, None
        r = (supabase_client.table('restaurant_profiles')
             .select('max_order_items, restaurant_name')
             .eq('id', str(restaurant_id)).single().execute())
        limite = (r.data or {}).get('max_order_items')
        if limite and unidades > int(limite):
            nome = (r.data or {}).get('restaurant_name') or 'Esta loja'
            return True, (
                f"{nome} entrega até {int(limite)} itens por pedido "
                f"(o seu tem {unidades}). Divida em dois pedidos, por favor."
            )
        return False, None
    except Exception as e:
        logging.warning(f"⚠️ Não deu pra checar o limite de itens de {restaurant_id}: {e}")
        return False, None


_RESTAURANT_CLOSED_RESPONSE = (
    # "erro" (padrão do payment.py) + "error" (o que o processResponse do app
    # cliente lê) — assim a mensagem amigável aparece de fato no toast.
    {"erro": "O restaurante fechou e não está aceitando pedidos no momento.",
     "error": "O restaurante fechou e não está aceitando pedidos no momento.",
     "error_code": "RESTAURANT_CLOSED"},
    409,
)


@mp_payment_bp.route('/pagamentos/criar_preferencia', methods=['POST'])
@limiter.limit("20 per minute")
def criar_preferencia_mercado_pago():
    logging.info("🎯 === INICIANDO CRIAÇÃO DE PREFERÊNCIA DE PAGAMENTO ===")
    try:
        client_profile_id, auth_error = _get_authenticated_client_profile_id()
        if auth_error:
            return auth_error

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
        
        # 🔒 Trava: não aceita pedido se o restaurante estiver fechado.
        if _restaurant_is_closed(dados_pedido.get('restaurant_id')):
            logging.info(f"⛔ Pedido barrado: restaurante {dados_pedido.get('restaurant_id')} está fechado.")
            _body, _code = _RESTAURANT_CLOSED_RESPONSE
            return jsonify(_body), _code

        # 🔒 Trava: pedido grande demais pro que a loja consegue entregar.
        _excede, _msg = _excede_limite_de_itens(
            dados_pedido.get('restaurant_id'), dados_pedido.get('itens', []))
        if _excede:
            logging.info(f"⛔ Pedido barrado por limite de itens: {_msg}")
            return jsonify({"erro": _msg, "error": _msg,
                            "error_code": "MAX_ITEMS_EXCEEDED"}), 409

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
        
        # Repasse do entregador (LÍQUIDO) e margem de frete JÁ na criação. Antes,
        # o valor_repassado_entregador só era calculado no webhook (online) ou no
        # fechamento — então pedido em dinheiro/pendente ficava com o campo NULL e
        # o app do entregador mostrava o FRETE CHEIO (sem o desconto da plataforma)
        # na tela de pedido disponível. Calculando aqui, "Você recebe" já sai certo.
        _fee_create = float(dados_pedido.get('delivery_fee', 0) or 0)
        try:
            _payout_create = float(calculate_courier_payout(
                dados_pedido.get('delivery_distance_km'), delivery_fee=_fee_create))
        except Exception as _e_payout:
            logging.warning(f"⚠️ calculate_courier_payout na criação falhou ({_e_payout}); usando frete cheio.")
            _payout_create = _fee_create

        # 🔒 ÁREA DE ENTREGA — barreira autoritativa.
        #
        # A calculadora de frete é CONSELHO: quem grava o pedido é quem precisa
        # recusar. Sem esta checagem, dois pedidos de São Paulo pra Nova Iguaçu
        # (331,74 km, frete R$ 501,10) foram criados em 13 e 15/08/2026, com o
        # raio da plataforma em 50 km — um deles gerou cobrança PIX de R$553,10
        # por uma entrega que ninguém tinha como fazer.
        _fora, _msg_area = _checar_area_entrega(
            dados_pedido.get('restaurant_id'),
            dados_pedido.get('client_latitude'),
            dados_pedido.get('client_longitude'),
        )
        if _fora:
            return jsonify({"erro": _msg_area, "error_code": "FORA_DA_AREA"}), 409

        # Preparar dados do pedido para o banco
        order_data = {
            'id': pedido_id,
            'client_id': client_profile_id,
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
            'valor_repassado_entregador': round(_payout_create, 2),
            'margem_frete': round(_fee_create - _payout_create, 2),
            # Sem estes, o pedido online nascia SEM codigos: o entregador nao
            # tinha o que retirar e o cliente nao tinha o que conferir na
            # entrega. So o caminho de pedido em dinheiro (orders.py) gerava.
            'pickup_code': generate_verification_code(),
            'delivery_code': generate_verification_code(),
            'created_at': datetime.utcnow().isoformat(),
            'updated_at': datetime.utcnow().isoformat()
        }
        if payment_method == 'cash':
            order_data['status_pagamento'] = 'pending_cash'
        
        # Garante service_role no PostgREST antes de mexer em 'orders' — evita o
        # 42501 (RLS) caso a sessão do cliente supabase tenha sido poluída por
        # um login de usuário em outra rota deste mesmo worker.
        _force_service_role()

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
        
        # --- VUL-08: Revalidação de cupom no backend (lógica única em utils/coupons) ---
        # Aqui só para pagamento ONLINE. No dinheiro, _validar_itens_e_total (mais
        # abaixo) cuida do cupom com a MESMA lógica — evita aplicar/consumir 2x.
        coupon_code = dados_pedido.get('coupon_code', '').strip()
        backend_discount = 0.0
        applied_coupon_id = None
        desconto_parceiro = 0.0  # parte do desconto que sai do repasse da loja
        if coupon_code and payment_method != 'cash':
            try:
                order_subtotal = float(dados_pedido.get('total_amount_items', 0))
                delivery_fee_c = float(dados_pedido.get('delivery_fee', 0) or 0)
                _rid = dados_pedido.get('restaurant_id')
                # Cupom da loja só vale nela; cupom da plataforma vale em qualquer.
                coupon = _buscar_cupom(coupon_code, _rid)
                evalr = evaluate_coupon(coupon, order_subtotal, delivery_fee_c, restaurant_id=_rid)
                if evalr['valid'] and evalr['discount_amount'] > 0:
                    backend_discount = evalr['discount_amount']
                    desconto_parceiro = float(evalr.get('restaurant_discount') or 0)
                    applied_coupon_id = coupon.get('id')
                    logging.info(
                        f"✅ Cupom '{coupon_code}' válido — desconto R${backend_discount:.2f} "
                        f"(pago por: {evalr.get('paid_by')})")
                else:
                    logging.warning(f"⚠️ Cupom '{coupon_code}' não aplicado: {evalr['message']}")
            except Exception as _coupon_err:
                logging.warning(f"⚠️ Falha ao validar cupom no backend: {_coupon_err} — desconto ignorado")

        # --- Clube Inksa: benefícios do nível do cliente (frete grátis / % desconto) ---
        # Só no pagamento online (cartão/PIX). A plataforma absorve, como um cupom.
        # No dinheiro não aplica (o entregador recolhe em espécie).
        club_discount = 0.0
        club_level_name = None
        if payment_method != 'cash':
            try:
                from ..utils.club import client_checkout_benefits
                _cb = client_checkout_benefits(client_profile_id)
                _sub = float(dados_pedido.get('total_amount_items', 0) or 0)
                _fee = float(dados_pedido.get('delivery_fee', 0) or 0)
                if _cb.get('free_delivery'):
                    club_discount += _fee
                _pct = float(_cb.get('subtotal_discount_pct') or 0)
                if _pct > 0:
                    # TETO em reais: a % cresce com o ticket, a comissão não.
                    # Sem ele, 5% num pedido de R$300 custa R$15 — mais que o
                    # dobro da receita daquele pedido. 0 = sem teto.
                    _desc_pct = round(_sub * _pct / 100.0, 2)
                    _teto = float(_cb.get('max_discount_brl') or 0)
                    if _teto > 0:
                        _desc_pct = min(_desc_pct, _teto)
                    club_discount += _desc_pct
                club_discount = round(club_discount, 2)
                club_level_name = _cb.get('level_name')
                if club_discount > 0:
                    logging.info(f"🏅 Clube {club_level_name}: desconto R${club_discount:.2f}")
            except Exception as _club_err:
                logging.warning(f"⚠️ Falha ao aplicar benefício do clube: {_club_err}")

        # Corrigir total_amount com desconto (cupom + clube)
        total_discount = round(backend_discount + club_discount, 2)
        if total_discount > 0:
            raw_total = float(dados_pedido.get('total_amount_items', 0)) + float(dados_pedido.get('delivery_fee', 0))
            corrected_total = max(0.0, raw_total - total_discount)
            order_data['total_amount'] = round(corrected_total, 2)
            # desconto_parceiro fica gravado no pedido: é dele que os cálculos de
            # repasse descontam (cupom da loja sai do bolso dela, não da comissão).
            supabase_client.table('orders').update({
                'total_amount': order_data['total_amount'],
                'desconto_parceiro': round(desconto_parceiro, 2),
            }).eq('id', pedido_id).execute()
            if applied_coupon_id:
                consume_coupon(applied_coupon_id)
            logging.info(f"✅ total_amount corrigido para R${corrected_total:.2f} (cupom R${backend_discount:.2f} + clube R${club_discount:.2f})")
        # --- Fim VUL-08 / Clube ---

        # ✅ Pedido em dinheiro: não passa pelo MP, mas o valor ainda precisa
        # ser validado contra o cardápio real -- diferente do pagamento
        # online (onde o MP so cobra o que estiver em items_mp, ja
        # validado), aqui o total gravado é exatamente o que o entregador
        # vai cobrar em espécie do cliente, entao nao pode confiar cru no
        # que o app enviou.
        if payment_method == 'cash':
            try:
                total_seguro, subtotal_validado, _desconto_cash, _coupon_id_cash, _desc_parc_cash = _validar_itens_e_total(
                    dados_pedido.get('itens', []),
                    dados_pedido.get('delivery_fee', 0),
                    coupon_code,
                    dados_pedido.get('total_amount_items', 0),
                    restaurant_id=dados_pedido.get('restaurant_id'),
                )
            except ValueError as ve:
                logging.error(f"❌ Pedido em dinheiro {pedido_id} rejeitado: {ve}")
                supabase_client.table('orders').delete().eq('id', pedido_id).execute()
                return jsonify({"erro": str(ve)}), 400
            except Exception as e:
                # Qualquer erro INESPERADO aqui era fatal do jeito errado: o
                # pedido já tinha sido inserido, então o cliente levava um 500
                # com um pedido criado e com os valores que o APP mandou — sem a
                # revalidação do servidor. Agora limpa o pedido, avisa o Sentry e
                # devolve um erro que o app sabe mostrar.
                logging.error(f"❌ Falha ao validar pedido em dinheiro {pedido_id}: {e}", exc_info=True)
                sentry_sdk.capture_exception(e)
                try:
                    supabase_client.table('orders').delete().eq('id', pedido_id).execute()
                except Exception:
                    logging.error(f"❌ Não deu pra remover o pedido órfão {pedido_id}")
                return jsonify({"erro": "Não foi possível confirmar o valor do pedido. Tente novamente."}), 400

            if total_seguro <= 0:
                logging.error(f"❌ Pedido em dinheiro {pedido_id} com total inválido: {total_seguro}")
                supabase_client.table('orders').delete().eq('id', pedido_id).execute()
                return jsonify({"erro": "Valor do pedido inválido."}), 400

            supabase_client.table('orders').update({
                'total_amount_items': subtotal_validado,
                'total_amount': total_seguro,
                # Cupom da própria loja sai do repasse dela (não da comissão).
                'desconto_parceiro': _desc_parc_cash,
            }).eq('id', pedido_id).execute()

            if _coupon_id_cash:
                consume_coupon(_coupon_id_cash)

            logging.info(f"💵 Pedido em dinheiro {pedido_id} — itens validados, total real R${total_seguro:.2f}.")
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

        # Aplicar benefício do Clube nos itens do MP (item negativo separado)
        if club_discount > 0:
            items_mp.append({'title': f'Clube {club_level_name or "Inksa"}', 'quantity': 1, 'unit_price': -club_discount})
            logging.info(f"✅ Benefício do clube R${club_discount:.2f} adicionado aos itens MP")

        if not items_mp or all(i['unit_price'] <= 0 for i in items_mp if i.get('title') != f'Desconto Cupom {coupon_code}'):
            logging.error("❌ Nenhum item válido para processar!")
            supabase_client.table('orders').delete().eq('id', pedido_id).execute()
            return jsonify({"erro": "A lista de itens está vazia ou todos os itens têm valor zero."}), 400

        logging.info(f"✅ Total de itens válidos: {len(items_mp)}")

        # ✅ PASSO 3-ASAAS: provider alternativo (PAYMENT_PROVIDER=asaas)
        # Mesma validação de itens/cupom/clube acima; muda só quem cobra.
        # A invoiceUrl do Asaas é uma página hospedada com PIX + cartão —
        # equivalente direto do init_point do MP (o app só redireciona).
        if payment_provider() == 'asaas':
            if not asaas.is_configured():
                supabase_client.table('orders').delete().eq('id', pedido_id).execute()
                return jsonify({"erro": "Serviço de pagamento indisponível (Asaas não configurado)."}), 503

            # Valor autoritativo = soma dos itens validados (inclui descontos negativos)
            valor_online = round(sum(float(i['unit_price']) * int(i['quantity']) for i in items_mp), 2)
            if valor_online <= 0:
                supabase_client.table('orders').delete().eq('id', pedido_id).execute()
                return jsonify({"erro": "Valor do pedido inválido."}), 400

            # O Asaas recusa cobrança abaixo de R$ 5,00 — PIX, débito e crédito
            # igualmente. Sem esta checagem o cliente recebia "O provedor de
            # pagamento recusou a cobrança", que não diz nada e não dá saída.
            # Pior: o create_checkout_payment ainda tenta CREDIT_CARD quando o
            # PIX é recusado, então parecia falha dos três meios de pagamento.
            if valor_online < ASAAS_VALOR_MINIMO:
                supabase_client.table('orders').delete().eq('id', pedido_id).execute()
                logging.warning(
                    f"⚠️ Pedido {pedido_id} abaixo do mínimo do Asaas: R${valor_online:.2f}")
                return jsonify({"erro": (
                    f"Pagamento online exige pedido de pelo menos "
                    f"R$ {ASAAS_VALOR_MINIMO:.2f}. Adicione mais itens ou "
                    f"escolha pagar em dinheiro na entrega."
                )}), 400

            # Dados do cliente (nome + CPF obrigatório no Asaas)
            prof = supabase_client.table('client_profiles').select('first_name,last_name,cpf').eq('id', client_profile_id).execute()
            pdata = prof.data[0] if prof.data else {}
            nome_cliente = f"{pdata.get('first_name') or ''} {pdata.get('last_name') or ''}".strip() or 'Cliente Inksa'

            # CPF é EXIGÊNCIA REGULATÓRIA do PIX, não escolha do Asaas — mas o
            # cadastro do cliente não pede. Resultado: a maioria dos clientes
            # não conseguia pagar online e recebia a mensagem crua do Asaas,
            # que não diz o que fazer. Agora o app pode mandar o CPF no
            # checkout; se vier, guarda no perfil pra não perguntar de novo.
            cpf_cliente = re.sub(r'\D', '', str(dados_pedido.get('cpf') or '')) or (pdata.get('cpf') or '')
            if not cpf_cliente:
                supabase_client.table('orders').delete().eq('id', pedido_id).execute()
                return jsonify({
                    "erro": "Para pagar online precisamos do seu CPF. "
                            "Você pode informá-lo agora ou pagar em dinheiro na entrega.",
                    "codigo": "cpf_obrigatorio",
                }), 400
            if cpf_cliente and cpf_cliente != (pdata.get('cpf') or ''):
                try:
                    supabase_client.table('client_profiles').update(
                        {'cpf': cpf_cliente}).eq('id', client_profile_id).execute()
                except Exception:
                    logging.warning("Não deu pra salvar o CPF no perfil — segue o pagamento")

            ok_c, customer_or_msg = asaas.get_or_create_customer(
                nome_cliente, cpf_cliente, email=cliente_email,
                external_reference=client_profile_id,
            )
            if not ok_c:
                supabase_client.table('orders').delete().eq('id', pedido_id).execute()
                return jsonify({"erro": customer_or_msg}), 400

            # O método escolhido no app comanda a fatura: pix → só QR PIX;
            # crédito/débito/outros → só cartão. Nunca UNDEFINED (mostraria boleto).
            billing_type = 'PIX' if payment_method == 'pix' else 'CREDIT_CARD'
            # Depois de pagar, a página do Asaas redireciona de volta pro app
            success_url = (dados_pedido.get('urls_retorno') or {}).get('sucesso') \
                or f"{os.environ.get('FRONTEND_URL', 'https://clientes.inksadelivery.com.br')}/pagamento/sucesso"

            ok_p, pay_or_msg = asaas.create_checkout_payment(
                customer_or_msg, valor_online, pedido_id,
                billing_type=billing_type, success_url=success_url,
            )
            if not ok_p:
                logging.error(f"❌ Asaas recusou a criação da cobrança: {pay_or_msg}")
                supabase_client.table('orders').delete().eq('id', pedido_id).execute()
                return jsonify({"erro": "O provedor de pagamento recusou a cobrança.", "detalhes": pay_or_msg}), 400

            supabase_client.table('orders').update({
                'payment_provider': 'asaas',
                'id_transacao_mp': str(pay_or_msg['payment_id']),
            }).eq('id', pedido_id).execute()

            logging.info(f"✅ Cobrança Asaas criada: {pay_or_msg['payment_id']} — pedido {pedido_id}")
            return jsonify({
                "mensagem": "Cobrança criada com sucesso!",
                "checkout_link": pay_or_msg['invoice_url'],
                "preference_id": pay_or_msg['payment_id'],
                "pedido_id": pedido_id,
                "provider": "asaas",
                # Quando é PIX, vai o QR/copia-e-cola pro app mostrar inline; se
                # None (cartão ou PIX indisponível), o app usa o checkout_link.
                "pix": pay_or_msg.get('pix'),
            }), 200

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


@mp_payment_bp.route('/pagamentos/config', methods=['GET'])
def payment_config():
    """Config pública do checkout: qual provider está ativo.
    O app cliente usa isso pra decidir se oferece cartão in-app (MP Bricks)
    ou manda tudo pelo checkout hospedado (Asaas invoiceUrl)."""
    return jsonify({"provider": payment_provider()}), 200


# ─── PAGAMENTO TRANSPARENTE COM CARTÃO (in-app, sem redirecionar) ────────────
def _buscar_cupom(coupon_code, restaurant_id):
    """Acha o cupom que vale nesta loja: o DELA ou o da plataforma.

    Duas consultas simples em vez de um `.or_()` no PostgREST: o filtro
    combinado dependia de sintaxe do cliente e, se estourasse, derrubava a
    criação do pedido inteira (o pedido já estava inserido → o cliente via 500
    com o pedido criado e sem o total recalculado pelo servidor).

    Nunca lança: qualquer falha vira "sem cupom", que no pior caso cobra o valor
    cheio — o oposto de perder o pedido.
    """
    if not coupon_code:
        return None
    code = str(coupon_code).strip().upper()
    if not code:
        return None
    try:
        # O cupom da própria loja tem prioridade sobre o da plataforma.
        if restaurant_id:
            r = (supabase_client.table('coupons').select('*')
                 .eq('code', code).eq('restaurant_id', str(restaurant_id)).execute())
            if r.data:
                return r.data[0]
        r = (supabase_client.table('coupons').select('*')
             .eq('code', code).is_('restaurant_id', 'null').execute())
        return r.data[0] if r.data else None
    except Exception as e:
        logging.warning(f"⚠️ Falha ao buscar cupom '{code}': {e} — seguindo sem desconto")
        return None


def _entrega_propria(restaurant_id) -> bool:
    """True quando a loja entrega com a própria moto (delivery_type='own').

    Muda quem fica com o frete: na entrega própria não existe entregador Inksa,
    então o frete inteiro é da LOJA. Antes nenhum cálculo financeiro olhava esse
    campo — o frete virava "repasse de entregador" de um entregador inexistente
    e o dinheiro ficava parado na plataforma.
    """
    if not restaurant_id:
        return False
    try:
        r = (supabase_client.table('restaurant_profiles')
             .select('delivery_type').eq('id', str(restaurant_id)).single().execute())
        return (r.data or {}).get('delivery_type') == 'own'
    except Exception as e:
        # Falhou a consulta: mantém o comportamento antigo (plataforma) em vez
        # de arriscar dar o frete pro lado errado.
        logging.warning(f"Não deu pra ler delivery_type de {restaurant_id}: {e}")
        return False


def _split_online(valor_itens, delivery_fee, distancia_km, comissao, desconto_parceiro,
                  entrega_propria):
    """(repasse_restaurante, repasse_entregador, margem_frete) do pedido online."""
    valor_itens = float(valor_itens or 0)
    delivery_fee = float(delivery_fee or 0)
    comissao = float(comissao or 0)
    desconto_parceiro = float(desconto_parceiro or 0)

    if entrega_propria:
        # O frete é da loja e entra no repasse dela. Nenhum entregador Inksa
        # participou → sem repasse de frete e sem margem pra plataforma.
        return (round(valor_itens - comissao - desconto_parceiro + delivery_fee, 2), 0.0, 0.0)

    entregador = float(calculate_courier_payout(distancia_km, delivery_fee=delivery_fee))
    return (round(valor_itens - comissao - desconto_parceiro, 2),
            round(entregador, 2),
            round(delivery_fee - entregador, 2))


def _validar_itens_e_total(items_from_request, delivery_fee, coupon_code, subtotal_items,
                           restaurant_id=None):
    """Revalida precos no banco e calcula o total no servidor (nao confia no front).
    Retorna (total_seguro, subtotal_validado, desconto, coupon_id, desconto_parceiro).
    O coupon_id (quando != None) deve ser 'consumido' pelo chamador apos criar o
    pedido. desconto_parceiro = parte do desconto que sai do repasse da loja.
    Lanca ValueError se invalido.
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

    # Desconto de cupom — mesma lógica única do preview e do fluxo online
    desconto = 0.0
    coupon_id = None
    desconto_parceiro = 0.0
    if coupon_code:
        # Cupom da loja só vale nela; cupom da plataforma vale em qualquer uma.
        coupon = _buscar_cupom(coupon_code, restaurant_id)
        evalr = evaluate_coupon(coupon, subtotal, delivery_fee, restaurant_id=restaurant_id)
        if evalr['valid'] and evalr['discount_amount'] > 0:
            desconto = evalr['discount_amount']
            coupon_id = coupon.get('id')
            desconto_parceiro = float(evalr.get('restaurant_discount') or 0)

    total = max(0.0, round(subtotal + float(delivery_fee or 0) - desconto, 2))
    return total, round(subtotal, 2), desconto, coupon_id, round(desconto_parceiro, 2)


@mp_payment_bp.route('/pagamentos/processar_cartao', methods=['POST'])
@limiter.limit("20 per minute")
def processar_pagamento_cartao():
    """Processa pagamento com cartao via token do MP Bricks (sem redirecionar)."""
    logging.info("💳 === PROCESSANDO PAGAMENTO COM CARTÃO (transparente) ===")
    try:
        # Cartão in-app usa o MP Bricks; com outro provider o cartão é pago
        # no checkout hospedado (o app nem deveria chamar esta rota).
        if payment_provider() != 'mercadopago':
            return jsonify({"erro": "Pagamento com cartão dentro do app indisponível — use o pagamento online."}), 400

        if not supabase_client:
            return jsonify({"erro": "Serviço de banco indisponível."}), 500

        client_profile_id, auth_error = _get_authenticated_client_profile_id()
        if auth_error:
            return auth_error

        sdk = current_app.mp_sdk
        if sdk is None:
            return jsonify({"erro": "Serviço de pagamento indisponível."}), 503

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
            total_seguro, subtotal_validado, desconto, coupon_id_card, desc_parc_card = _validar_itens_e_total(
                items_req, d.get('delivery_fee', 0), (d.get('coupon_code') or '').strip(),
                d.get('total_amount_items', 0), restaurant_id=d.get('restaurant_id')
            )
        except ValueError as ve:
            return jsonify({"erro": str(ve)}), 400

        if total_seguro <= 0:
            return jsonify({"erro": "Valor do pedido inválido."}), 400

        # Clube Inksa: benefício do nível do cliente (frete grátis / % desconto)
        try:
            from ..utils.club import client_checkout_benefits
            _cb = client_checkout_benefits(client_profile_id)
            _club_card = 0.0
            if _cb.get('free_delivery'):
                _club_card += float(d.get('delivery_fee', 0) or 0)
            _pct = float(_cb.get('subtotal_discount_pct') or 0)
            if _pct > 0:
                _desc_pct = round(float(subtotal_validado or 0) * _pct / 100.0, 2)
                _teto = float(_cb.get('max_discount_brl') or 0)
                if _teto > 0:
                    _desc_pct = min(_desc_pct, _teto)
                _club_card += _desc_pct
            _club_card = round(_club_card, 2)
            if _club_card > 0:
                total_seguro = round(max(0.0, total_seguro - _club_card), 2)
                logging.info(f"🏅 Clube {_cb.get('level_name')}: desconto R${_club_card:.2f} (cartão)")
        except Exception as _club_err:
            logging.warning(f"⚠️ Falha ao aplicar clube (cartão): {_club_err}")

        if total_seguro <= 0:
            return jsonify({"erro": "Valor do pedido inválido após benefícios."}), 400

        # 🔒 Trava: não aceita pedido se o restaurante estiver fechado.
        if _restaurant_is_closed(d.get('restaurant_id')):
            logging.info(f"⛔ Pedido (cartão) barrado: restaurante {d.get('restaurant_id')} está fechado.")
            _body, _code = _RESTAURANT_CLOSED_RESPONSE
            return jsonify(_body), _code

        _excede, _msg = _excede_limite_de_itens(d.get('restaurant_id'), items_req)
        if _excede:
            logging.info(f"⛔ Pedido (cartão) barrado por limite de itens: {_msg}")
            return jsonify({"erro": _msg, "error": _msg,
                            "error_code": "MAX_ITEMS_EXCEEDED"}), 409

        # 1) Cria o pedido (aguardando pagamento)
        import uuid
        from datetime import datetime
        # 🔒 Mesma barreira do outro caminho de criação (ver _checar_area_entrega).
        _fora, _msg_area = _checar_area_entrega(
            d.get('restaurant_id'), d.get('client_latitude'), d.get('client_longitude'),
        )
        if _fora:
            return jsonify({"erro": _msg_area, "error_code": "FORA_DA_AREA"}), 409

        pedido_id = d.get('pedido_id') or str(uuid.uuid4())
        order_data = {
            'id': pedido_id,
            'client_id': client_profile_id,
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
            'pickup_code': generate_verification_code(),
            'delivery_code': generate_verification_code(),
            'created_at': datetime.utcnow().isoformat(),
            'updated_at': datetime.utcnow().isoformat(),
        }
        ins = supabase_client.table('orders').insert(order_data).execute()
        if not ins.data:
            return jsonify({"erro": "Erro ao criar pedido."}), 500

        if coupon_id_card:
            consume_coupon(coupon_id_card)

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

        # Chave de idempotencia estavel por pedido -- sem isso, o SDK gera uma
        # chave aleatoria a cada chamada (mesmo em retry), entao um duplo-clique
        # ou retry de rede pode gerar DUAS cobrancas reais pro mesmo pedido.
        idempotency_options = RequestOptions(custom_headers={"x-idempotency-key": pedido_id})
        result = sdk.payment().create(payment_payload, idempotency_options)
        resp = result.get("response", {}) if isinstance(result, dict) else {}
        status = resp.get("status")
        payment_id = resp.get("id")
        logging.info(f"💳 Pagamento MP criado: id={payment_id} status={status}")

        if status == 'approved':
            # Comissão e repasse vêm de platform_settings (editáveis no admin)
            comissao = float(calculate_platform_commission(subtotal_validado, d.get('restaurant_id')))
            delivery_fee_charged = float(d.get('delivery_fee', 0) or 0)
            # Na entrega própria o frete inteiro volta pra loja; margem de frete
            # (o que a plataforma retém) só existe com entregador Inksa e pode
            # ser negativa em entregas curtas — é o valor real.
            repasse_rest, courier_payout, margem_frete = _split_online(
                subtotal_validado, delivery_fee_charged, d.get('delivery_distance_km'),
                comissao, desc_parc_card, _entrega_propria(d.get('restaurant_id')))
            supabase_client.table('orders').update({
                'status': 'pending',  # ativa o pedido para o restaurante
                'status_pagamento': 'approved',
                'comissao_plataforma': comissao,
                # Cupom da própria loja sai do repasse DELA — a comissão da
                # plataforma fica intacta. Cupom da Inksa não entra aqui (é
                # absorvido pela comissão, desconto_parceiro = 0).
                'valor_repassado_restaurante': repasse_rest,
                'desconto_parceiro': desc_parc_card,
                'valor_repassado_entregador': courier_payout,
                'margem_frete': margem_frete,
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

    if not webhook_secret:
        logging.critical(
            "MERCADO_PAGO_WEBHOOK_SECRET não configurado — rejeitando webhook "
            "por segurança (fail-closed). Configure a variável no Render."
        )
        return jsonify({'error': 'Webhook não configurado corretamente'}), 503

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
                    sentry_sdk.capture_exception(_idem_err)

                logging.info(f"✅ Pagamento {resource_id} APROVADO! Iniciando atualização do pedido...")

                # 🔍 DIAGNÓSTICO: Buscar o pedido ANTES de atualizar (COM RETRY)
                logging.info(f"🔍 PASSO 1: Buscando pedido {external_reference} no Supabase...")
                response_supabase = supabase_client.table('orders').select('*').eq('id', external_reference).execute()
                
                # 🔄 RETRY: Se não encontrar, aguarda e tenta novamente (race condition)
                if not response_supabase.data:
                    logging.warning(f"⚠️ Pedido não encontrado na primeira tentativa. Aguardando 3 segundos...")
                    import time
                    gevent.sleep(3)
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

                    # Comissão e repasse vêm de platform_settings (editáveis no admin)
                    comissao_plataforma = float(calculate_platform_commission(valor_total_itens, pedido_do_bd.get('restaurant_id')))
                    # Cupom da própria loja já foi gravado no pedido: sai do
                    # repasse dela, não da comissão da plataforma.
                    _desc_parc = float(pedido_do_bd.get('desconto_parceiro') or 0)

                    delivery_fee_charged = float(pedido_do_bd.get('delivery_fee', 0.0) or 0.0)
                    # Margem de frete = frete cobrado − repasse ao entregador.
                    # Pode ser negativa (subsídio). Persistida para congelar o
                    # valor histórico da época do pedido. Na entrega própria o
                    # frete é da loja: vai pro repasse dela, margem zero.
                    valor_para_restaurante, valor_para_entregador, margem_frete = _split_online(
                        valor_total_itens, delivery_fee_charged,
                        pedido_do_bd.get('delivery_distance_km'),
                        comissao_plataforma, _desc_parc,
                        _entrega_propria(pedido_do_bd.get('restaurant_id')))

                    # ✅ DADOS QUE SERÃO ATUALIZADOS
                    update_data = {
                        'status': 'pending',  # ✅ ISSO ATIVA O PEDIDO PARA O RESTAURANTE!
                        'status_pagamento': status,
                        'comissao_plataforma': round(comissao_plataforma, 2),
                        'valor_repassado_restaurante': round(valor_para_restaurante, 2),
                        'valor_repassado_entregador': round(valor_para_entregador, 2),
                        'margem_frete': margem_frete,
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
                    gevent.sleep(3)
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
                    gevent.sleep(3)
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
            sentry_sdk.capture_exception(e)
            return jsonify({"status": "error", "message": "Erro ao processar webhook"}), 500
    
    logging.info("=" * 80)
    logging.info("✅ Webhook processado - retornando 200 OK")
    logging.info("=" * 80)
    return jsonify({"status": "ok"}), 200


# ─── WEBHOOK DO ASAAS ────────────────────────────────────────────────────────
@mp_payment_bp.route('/pagamentos/webhook_asaas', methods=['POST'])
def asaas_webhook():
    """Notificações de cobrança do Asaas.

    Autenticação fail-closed pelo header 'asaas-access-token' (token definido
    por nós no cadastro do webhook, em ASAAS_WEBHOOK_TOKEN). Retorna 200 para
    eventos irrelevantes (senão o Asaas fica reenviando e pausa a fila).
    """
    if not asaas.verify_webhook_token(request):
        logging.warning("⚠️ Webhook Asaas rejeitado: token inválido/ausente")
        return jsonify({'error': 'Token inválido'}), 401

    data = request.get_json(silent=True) or {}
    event = data.get('event')
    payment = data.get('payment') or {}
    payment_id = payment.get('id')
    pedido_id = payment.get('externalReference')

    logging.info(f"📬 Webhook Asaas: event={event} payment={payment_id} pedido={pedido_id}")

    if not pedido_id or supabase_client is None:
        return jsonify({"status": "ignored"}), 200

    try:
        # PIX cai como RECEIVED; cartão confirmado cai como CONFIRMED
        if event in ('PAYMENT_CONFIRMED', 'PAYMENT_RECEIVED'):
            resp = supabase_client.table('orders').select('*').eq('id', pedido_id).execute()
            if not resp.data:
                gevent.sleep(3)  # race: webhook pode chegar antes do commit do pedido
                resp = supabase_client.table('orders').select('*').eq('id', pedido_id).execute()
            if not resp.data:
                logging.error(f"❌ Webhook Asaas: pedido {pedido_id} não encontrado")
                return jsonify({"status": "order_not_found"}), 200

            pedido = resp.data[0]
            if pedido.get('status_pagamento') == 'approved':
                logging.info(f"⚠️ Pedido {pedido_id} já aprovado — webhook duplicado ignorado")
                return jsonify({"status": "already_processed"}), 200

            valor_itens = float(pedido.get('total_amount_items') or 0)
            comissao = float(calculate_platform_commission(valor_itens, pedido.get('restaurant_id')))
            fee = float(pedido.get('delivery_fee') or 0)
            # Cupom da própria loja sai do repasse dela (gravado no pedido).
            _desc_parc = float(pedido.get('desconto_parceiro') or 0)
            # Entrega própria: o frete é da loja, não de entregador nenhum.
            repasse_rest, courier, margem = _split_online(
                valor_itens, fee, pedido.get('delivery_distance_km'), comissao, _desc_parc,
                _entrega_propria(pedido.get('restaurant_id')))
            supabase_client.table('orders').update({
                'status': 'pending',  # ativa o pedido para o restaurante
                'status_pagamento': 'approved',
                'comissao_plataforma': round(comissao, 2),
                'valor_repassado_restaurante': repasse_rest,
                'valor_repassado_entregador': courier,
                'margem_frete': margem,
                'id_transacao_mp': str(payment_id),
                'payment_provider': 'asaas',
            }).eq('id', pedido_id).execute()
            logging.info(f"🎉 Pedido {pedido_id} APROVADO via Asaas ({event})")

        elif event == 'PAYMENT_REFUNDED':
            supabase_client.table('orders').update({
                'status_pagamento': 'refunded',
            }).eq('id', pedido_id).execute()
            logging.info(f"↩️ Pedido {pedido_id} marcado como reembolsado (Asaas)")

        elif event in ('PAYMENT_OVERDUE', 'PAYMENT_DELETED'):
            # Cobrança venceu/foi excluída sem pagamento: cancela só se o
            # pedido ainda estava aguardando pagamento.
            resp = supabase_client.table('orders').select('status').eq('id', pedido_id).execute()
            if resp.data and resp.data[0].get('status') == 'awaiting_payment':
                supabase_client.table('orders').update({
                    'status': 'cancelled',
                    'status_pagamento': 'expired',
                    'cancellation_reason': f'asaas_{event.lower()}',
                }).eq('id', pedido_id).execute()
                logging.info(f"🚫 Pedido {pedido_id} cancelado ({event})")

    except Exception as e:
        logging.error(f"❌ Erro no webhook Asaas: {e}", exc_info=True)
        sentry_sdk.capture_exception(e)
        # 500 faz o Asaas reenviar depois — comportamento desejado em erro nosso
        return jsonify({"status": "error"}), 500

    return jsonify({"status": "ok"}), 200
