# src/routes/orders.py
import uuid
import json
import random
import string
from datetime import datetime
from flask import Blueprint, request, jsonify, current_app
import psycopg2
import psycopg2.extras
import logging
import sentry_sdk
from ..utils.helpers import get_db_connection, get_user_id_from_token, supabase
from src.extensions import limiter

try:
    from .gamification_routes import (
        award_completion_points as _award_completion_points,
        award_first_order_bonus as _award_first_order_bonus,
        award_points_for_action as _award_points_for_action,
    )
except Exception:
    _award_completion_points = None
    _award_first_order_bonus = None
    _award_points_for_action = None

try:
    from ..services.notification_service import send_push_notification as _send_push
except Exception:
    _send_push = None


def _get_fcm_token(cur, table: str, user_id: str):
    """Busca fcm_token de um perfil. Retorna None silenciosamente se falhar."""
    try:
        cur.execute(f"SELECT fcm_token FROM {table} WHERE id = %s", (user_id,))
        row = cur.fetchone()
        return row['fcm_token'] if row else None
    except Exception:
        return None


def _notify(token, title, body, data=None):
    """Dispara push notification de forma defensiva — nunca propaga exceções."""
    if not _send_push or not token:
        return
    try:
        _send_push(token, title, body, data or {})
    except Exception as e:
        logging.getLogger(__name__).warning(f"FCM notificacao silenciada: {e}")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s'
)
logger = logging.getLogger(__name__)

orders_bp = Blueprint('orders', __name__)

DEFAULT_DELIVERY_FEE = 5.0

# Status internos aceitos
VALID_STATUSES_INTERNAL = {
    'awaiting_payment', 'pending', 'accepted', 'preparing', 'ready',
    'accepted_by_delivery', 'delivering', 'delivered', 'cancelled', 'archived'
}

# Mapa de exibição
STATUS_DISPLAY_MAP = {
    'awaiting_payment': 'Aguardando Pagamento',
    'pending': 'Pendente',
    'accepted': 'Aceito',
    'preparing': 'Preparando',
    'ready': 'Pronto',
    'accepted_by_delivery': 'Aguardando Retirada',
    'delivering': 'Saiu para Entrega',
    'delivered': 'Entregue',
    'delivery_failed': 'Entrega não realizada',
    'cancelled': 'Cancelado',
    'archived': 'Arquivado'
}

# Motivos de falha de entrega aceitos (códigos)
DELIVERY_INCIDENT_REASONS = {
    'customer_not_found',   # cliente não localizado / não atende
    'wrong_address',        # endereço errado ou incompleto
    'customer_refused',     # cliente recusou o pedido
    'customer_absent',      # ninguém para receber
    'courier_issue',        # problema com o entregador (acidente, moto, etc.)
    'courier_damaged',      # o entregador derrubou / danificou o pedido
    'wrong_order',          # pedido errado/incompleto
    'payment_issue',        # problema no pagamento (dinheiro)
}

# Desfechos (o que fazer com o pedido). O entregador NÃO escolhe mais — quem
# decide é o bot (danificado/restaurante fechado → descartar) e, quando cabe
# devolução, o RESTAURANTE (quer de volta? sim→devolver, não→descartar). Enquanto
# o restaurante não responde, fica 'awaiting_restaurant' (o bot cai pra dispose
# após ~10min pra não travar o entregador).
DELIVERY_INCIDENT_OUTCOMES = {
    'return_to_restaurant',  # devolver ao restaurante (restaurante quis de volta)
    'dispose',               # descartar (danificado / restaurante não quis / timeout)
    'awaiting_restaurant',   # aguardando o restaurante decidir se quer a devolução
    'keep',                  # entregador liberado / fica com o pedido
}

# Minutos que o restaurante tem pra responder "quer a devolução?" antes de o bot
# cair pro descarte (libera o entregador — comida fria dificilmente serve mesmo).
INCIDENT_RESTAURANT_WAIT_MIN = 10

# Regra de dinheiro por motivo (padrão dos grandes deliverys), baseada na culpa.
# pay_restaurant/pay_courier = continuam recebendo; refund_client = cliente reembolsado.
DELIVERY_INCIDENT_POLICY = {
    # Culpa do cliente: ele NÃO é reembolsado; restaurante e entregador recebem.
    'customer_not_found': {'fault': 'customer',   'pay_restaurant': True,  'pay_courier': True,  'refund_client': False},
    'customer_absent':    {'fault': 'customer',   'pay_restaurant': True,  'pay_courier': True,  'refund_client': False},
    'wrong_address':      {'fault': 'customer',   'pay_restaurant': True,  'pay_courier': True,  'refund_client': False},
    'customer_refused':   {'fault': 'customer',   'pay_restaurant': True,  'pay_courier': True,  'refund_client': False},
    # Culpa do restaurante: cliente reembolsado; restaurante NÃO recebe; entregador recebe.
    'wrong_order':        {'fault': 'restaurant', 'pay_restaurant': False, 'pay_courier': True,  'refund_client': True},
    # Culpa do entregador: cliente reembolsado; entregador NÃO recebe; restaurante recebe.
    # (Se cabe DESCONTAR o entregador pelo prejuízo, quem decide é o admin, caso a
    #  caso, no painel de Ocorrências — não é automático aqui.)
    'courier_issue':      {'fault': 'courier',    'pay_restaurant': True,  'pay_courier': False, 'refund_client': True},
    'courier_damaged':    {'fault': 'courier',    'pay_restaurant': True,  'pay_courier': False, 'refund_client': True},
    # Pagamento (dinheiro): nada foi cobrado pela plataforma.
    'payment_issue':      {'fault': 'none',       'pay_restaurant': False, 'pay_courier': False, 'refund_client': False},
}

def generate_verification_code(length=4):
    chars = string.ascii_uppercase.replace('I', '').replace('O', '')
    chars += string.digits.replace('0', '').replace('1', '')
    return ''.join(random.choice(chars) for _ in range(length))

def is_valid_status_transition(current_status, new_status):
    valid_transitions = {
        'awaiting_payment': ['pending', 'cancelled'],
        'pending': ['accepted', 'cancelled'],
        'accepted': ['preparing', 'cancelled'],
        'preparing': ['ready', 'cancelled'],
        'ready': ['accepted_by_delivery', 'cancelled'],
        'accepted_by_delivery': ['delivering', 'cancelled'],
        'delivering': ['delivered'],
        'delivered': ['archived'],
        'cancelled': ['archived'],
        'archived': []
    }
    return new_status in valid_transitions.get(current_status, [])

@orders_bp.before_request
def handle_options():
    if request.method == "OPTIONS":
        response = jsonify()
        response.headers.add("Access-Control-Allow-Origin", request.headers.get("Origin", "*"))
        response.headers.add("Access-Control-Allow-Headers", "Content-Type,Authorization")
        response.headers.add("Access-Control-Allow-Methods", "GET,PUT,POST,DELETE,OPTIONS")
        response.headers.add("Access-Control-Allow-Credentials", "true")
        return response

@orders_bp.route('/', methods=['GET', 'POST'])
@limiter.limit("30 per minute")
def handle_orders():
    conn = None
    try:
        user_auth_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
        if error:
            return error

        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Erro de conexão com o banco de dados"}), 500

        if request.method == 'GET':
            # Whitelist de ordenação (o valor entra interpolado no ORDER BY —
            # sem isto, sort_by/sort_order abririam SQL injection).
            ALLOWED_SORT = {'created_at', 'updated_at', 'total_amount'}
            sort_by = request.args.get('sort_by', 'created_at')
            if sort_by not in ALLOWED_SORT:
                sort_by = 'created_at'
            sort_order = 'asc' if str(request.args.get('sort_order', 'desc')).lower() == 'asc' else 'desc'
            status_filter = request.args.get('status')
            start_date = (request.args.get('start_date') or '').strip()
            end_date = (request.args.get('end_date') or '').strip()

            query = """
                SELECT o.*,
                       rp.restaurant_name,
                       rp.logo_url as restaurant_logo,
                       cp.first_name as client_first_name,
                       cp.last_name as client_last_name
                FROM orders o
                LEFT JOIN restaurant_profiles rp ON o.restaurant_id = rp.id
                LEFT JOIN client_profiles cp ON o.client_id = cp.id
                WHERE 1=1
            """
            params = []

            if user_type == 'restaurant':
                with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                    cur.execute("SELECT id FROM restaurant_profiles WHERE user_id = %s", (user_auth_id,))
                    profile = cur.fetchone()
                    if not profile:
                        return jsonify({"error": "Perfil do restaurante não encontrado"}), 404
                    query += " AND o.restaurant_id = %s"
                    params.append(profile['id'])
                    # Restaurante NÃO vê pedidos aguardando pagamento
                    query += " AND o.status != 'awaiting_payment'"
                    logger.info("🔒 Filtrando pedidos não pagos para restaurante")
                    # NÃO filtra arquivados aqui: o painel do restaurante esconde
                    # os arquivados das colunas do kanban no front, mas os KPIs
                    # (Pedidos Hoje/Faturamento) precisam contar a venda do dia
                    # mesmo depois de arquivar. Só o cliente esconde os deletados.

            elif user_type == 'client':
                query += " AND o.client_id = (SELECT id FROM client_profiles WHERE user_id = %s)"
                params.append(user_auth_id)
                # Cliente não vê pedidos que ele "excluiu" (arquivou).
                query += " AND o.archived_at IS NULL"

            if status_filter:
                query += " AND o.status = %s"
                params.append(status_filter)

            # Filtro de período (De/Até) do painel — compara pela DATA LOCAL do
            # pedido (created_at é UTC; converte pro fuso de SP pra o dia bater
            # com o que o restaurante vê). Antes esses params eram IGNORADOS, por
            # isso o filtro de data "não fazia nada".
            if start_date:
                query += " AND (o.created_at AT TIME ZONE 'America/Sao_Paulo')::date >= %s"
                params.append(start_date)
            if end_date:
                query += " AND (o.created_at AT TIME ZONE 'America/Sao_Paulo')::date <= %s"
                params.append(end_date)

            query += f" ORDER BY o.{sort_by} {sort_order}"

            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute(query, tuple(params))
                orders = [dict(row) for row in cur.fetchall()]
            return jsonify(orders), 200

        elif request.method == 'POST':
            if user_type != 'client':
                return jsonify({"error": "Apenas clientes podem criar pedidos"}), 403

            data = request.get_json()
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute("SELECT id FROM client_profiles WHERE user_id = %s", (user_auth_id,))
                client_profile = cur.fetchone()
                if not client_profile:
                    return jsonify({"error": "Perfil do cliente não encontrado"}), 404

                # 🔒 Trava: restaurante fechado não recebe pedido (a tela do
                # cliente só mostra o selo; ele pode ter fechado depois que o
                # carrinho foi montado). Fail-open se não achar a linha.
                cur.execute("SELECT is_open FROM restaurant_profiles WHERE id = %s", (data.get('restaurant_id'),))
                _rest = cur.fetchone()
                if _rest is not None and _rest.get('is_open') is False:
                    return jsonify({
                        "error": "O restaurante fechou e não está aceitando pedidos no momento.",
                        "error_code": "RESTAURANT_CLOSED",
                    }), 409

                total_items = sum(item.get('price', 0) * item.get('quantity', 1) for item in data['items'])
                delivery_fee = data.get('delivery_fee', DEFAULT_DELIVERY_FEE)

                order_data = {
                    'id': str(uuid.uuid4()),
                    'client_id': client_profile['id'],
                    'restaurant_id': data['restaurant_id'],
                    'items': json.dumps(data['items']),
                    'delivery_address': json.dumps(data['delivery_address']),
                    'total_amount_items': total_items,
                    'delivery_fee': delivery_fee,
                    'total_amount': total_items + delivery_fee,
                    'status': 'awaiting_payment',
                    'pickup_code': generate_verification_code(),
                    'delivery_code': generate_verification_code()
                }

                logger.info(f"🆕 Criando pedido {order_data['id']} com status: awaiting_payment")

                insert_query = """
                    INSERT INTO orders
                        (id, client_id, restaurant_id, items, delivery_address,
                         total_amount_items, delivery_fee, total_amount, status,
                         pickup_code, delivery_code, delivery_id)
                    VALUES
                        (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL)
                    RETURNING *
                """
                cur.execute(insert_query, list(order_data.values()))
                new_order = dict(cur.fetchone())
                conn.commit()

                # nunca devolve os códigos no payload padrão
                new_order.pop('pickup_code', None)
                new_order.pop('delivery_code', None)

                # FCM: notifica restaurante sobre novo pedido
                try:
                    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as _ncur:
                        rest_token = _get_fcm_token(_ncur, 'restaurant_profiles', new_order['restaurant_id'])
                    _notify(rest_token, "Novo pedido recebido!", "Voce tem um novo pedido para confirmar",
                            {"order_id": new_order['id']})
                except Exception as _e:
                    logger.warning(f"FCM pedido criado: {_e}")

                logger.info(f"✅ Pedido {new_order['id']} criado com sucesso! Aguardando pagamento...")
                return jsonify(new_order), 201

    except Exception as e:
        logger.error(f"Erro em handle_orders: {e}", exc_info=True)
        if conn:
            conn.rollback()
        return jsonify({"error": "Erro interno no servidor"}), 500
    finally:
        if conn:
            conn.close()

@orders_bp.route('/<uuid:order_id>/status', methods=['PUT'])
def update_order_status(order_id):
    conn = None
    try:
        user_auth_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
        if error:
            return error
        if user_type != 'restaurant':
            return jsonify({"error": "Apenas restaurantes podem alterar o status"}), 403

        data = request.get_json()
        if not data or 'new_status' not in data:
            return jsonify({"error": "Campo 'new_status' é obrigatório"}), 400

        new_status_internal = data['new_status']
        if new_status_internal not in VALID_STATUSES_INTERNAL:
            return jsonify({"error": f"Status inválido: '{new_status_internal}'"}), 400

        if new_status_internal in ['delivering', 'delivered']:
            return jsonify({"error": "Use o endpoint de código para esta transição."}), 400

        conn = get_db_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                SELECT o.status, o.status_pagamento, o.total_amount, o.id_transacao_mp, o.payment_provider
                FROM orders o
                JOIN restaurant_profiles rp ON o.restaurant_id = rp.id
                WHERE o.id = %s AND rp.user_id = %s
            """, (str(order_id), user_auth_id))
            order = cur.fetchone()
            if not order:
                return jsonify({"error": "Pedido não encontrado ou não pertence a este restaurante"}), 404

            current_status = order['status'].strip()

            if not is_valid_status_transition(current_status, new_status_internal):
                error_message = f"Transição de status de '{current_status}' para '{new_status_internal}' não permitida"
                return jsonify({"error": error_message}), 400

            # Arquivar NÃO apaga o status de entrega. Antes o arquivamento fazia
            # status='archived', e aí o pedido sumia de TODAS as queries que
            # filtram status='delivered' (financeiro, repasses, analytics) — a
            # receita "entregue e arquivada" simplesmente desaparecia. Agora
            # marca só archived_at; o painel esconde por esse campo, mas o
            # status (delivered/cancelled) e o financeiro permanecem intactos.
            if new_status_internal == 'archived':
                cur.execute(
                    "UPDATE orders SET archived_at = NOW(), updated_at = NOW() WHERE id = %s RETURNING *",
                    (str(order_id),))
                _arch = dict(cur.fetchone())
                conn.commit()
                _arch.pop('pickup_code', None)
                _arch.pop('delivery_code', None)
                return jsonify(_arch), 200

            cur.execute(
                "UPDATE orders SET status = %s, updated_at = NOW() WHERE id = %s RETURNING *",
                (new_status_internal, str(order_id))
            )
            updated_order = dict(cur.fetchone())
            conn.commit()

            # Cancelamento de pedido já pago (online) precisa estornar o cliente
            # automaticamente -- sem isso o pedido fica "pago mas cancelado" e o
            # dinheiro só volta se alguém no admin perceber manualmente.
            if new_status_internal == 'cancelled' and order['status_pagamento'] == 'approved':
                refund_amount = float(order['total_amount'] or 0)
                if refund_amount > 0:
                    try:
                        from ..utils.gateway import refund_order_payment
                        if order['id_transacao_mp']:
                            ok_refund, refund_detail = refund_order_payment(dict(order), current_app.mp_sdk)
                            if ok_refund:
                                with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as _rcur:
                                    _rcur.execute(
                                        "UPDATE orders SET status_pagamento = 'refunded', updated_at = NOW() WHERE id = %s",
                                        (str(order_id),),
                                    )
                                conn.commit()
                                logger.info(f"Reembolso automático OK (cancelamento pelo restaurante): pedido {order_id} R${refund_amount}")
                            else:
                                logger.warning(f"Gateway recusou reembolso do pedido {order_id} (cancelamento restaurante): {refund_detail}")
                                sentry_sdk.capture_message(
                                    f"MP recusou reembolso automático do pedido {order_id} (cancelado pelo restaurante) — requer ação manual do admin.",
                                    level="warning",
                                )
                        else:
                            logger.warning(f"Sem SDK MP/id_transacao_mp para reembolsar pedido {order_id} (cancelamento restaurante)")
                            sentry_sdk.capture_message(
                                f"Pedido {order_id} cancelado pelo restaurante estava pago mas sem id_transacao_mp/SDK disponível — requer ação manual do admin.",
                                level="warning",
                            )
                    except Exception as _re:
                        logger.warning(f"Reembolso automático falhou (cancelamento restaurante, fica pendente p/ admin): {_re}")
                        sentry_sdk.capture_exception(_re)

            # FCM: notificacoes por mudanca de status
            try:
                with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as _ncur:
                    if new_status_internal == 'accepted':
                        # Notifica cliente
                        cli_token = _get_fcm_token(_ncur, 'client_profiles', str(updated_order['client_id']))
                        _notify(cli_token, "Pedido aceito! 🎉", "Seu pedido foi confirmado pelo restaurante",
                                {"order_id": str(order_id), "status": "accepted"})
                    elif new_status_internal == 'ready':
                        # Notifica entregadores disponíveis (broadcast: busca todos com fcm_token)
                        try:
                            _ncur.execute("SELECT fcm_token FROM delivery_profiles WHERE fcm_token IS NOT NULL LIMIT 50")
                            for _drow in _ncur.fetchall():
                                _notify(_drow['fcm_token'], "Entrega disponivel! 🛵",
                                        "Um pedido esta pronto para coleta",
                                        {"order_id": str(order_id), "status": "ready"})
                        except Exception:
                            pass
            except Exception as _e:
                logger.warning(f"FCM update_order_status: {_e}")

            updated_order.pop('pickup_code', None)
            updated_order.pop('delivery_code', None)
            return jsonify(updated_order), 200

    except Exception as e:
        logger.error(f"Erro em update_order_status: {e}", exc_info=True)
        if conn:
            conn.rollback()
        return jsonify({"error": "Erro interno do servidor"}), 500
    finally:
        if conn:
            conn.close()

@orders_bp.route('/<uuid:order_id>/pickup', methods=['POST'])
def pickup_order(order_id):
    logger.info(f"=== INÍCIO PICKUP_ORDER para {order_id} ===")
    conn = None
    try:
        user_auth_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
        if error:
            return error
        if user_type not in ['restaurant', 'delivery']:
            return jsonify({"error": "Acesso não autorizado para retirada"}), 403

        data = request.get_json()
        if not data or 'pickup_code' not in data:
            return jsonify({"error": "Código de retirada (pickup_code) é obrigatório"}), 400

        code = str(data['pickup_code']).strip().upper()

        conn = get_db_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT status, pickup_code, restaurant_id, delivery_id FROM orders WHERE id = %s", (str(order_id),))
            order = cur.fetchone()
            if not order:
                return jsonify({"error": "Pedido não encontrado"}), 404

            # Confere posse do pedido alem do codigo -- o codigo de 4 chars
            # sozinho e forca-bruta-vel e nao deveria ser a unica barreira.
            if user_type == 'restaurant':
                cur.execute("SELECT id FROM restaurant_profiles WHERE user_id = %s", (user_auth_id,))
                prof = cur.fetchone()
                if not prof or str(prof['id']) != str(order['restaurant_id']):
                    return jsonify({"error": "Este pedido não pertence ao seu restaurante"}), 403
            else:
                cur.execute("SELECT id FROM delivery_profiles WHERE user_id = %s", (user_auth_id,))
                prof = cur.fetchone()
                if not prof or order['delivery_id'] is None or str(prof['id']) != str(order['delivery_id']):
                    return jsonify({"error": "Este pedido não está atribuído a você"}), 403

            if order['status'] not in ['ready', 'accepted_by_delivery']:
                return jsonify({
                    "error": f"Pedido não está pronto para retirada. Status atual: {STATUS_DISPLAY_MAP.get(order['status'])}"
                }), 400

            if order['pickup_code'] != code:
                return jsonify({"error": "Código de retirada inválido"}), 403

            cur.execute("UPDATE orders SET status = 'delivering', updated_at = NOW() WHERE id = %s", (str(order_id),))
            # Busca client_id para notificação antes do commit
            cur.execute("SELECT client_id FROM orders WHERE id = %s", (str(order_id),))
            _pickup_row = cur.fetchone()
            conn.commit()
            logger.info(f"✅ Pedido {order_id} confirmado como retirado. Status: delivering")

            # FCM: notifica cliente que pedido foi coletado
            try:
                if _pickup_row and _pickup_row['client_id']:
                    _nc_pickup = get_db_connection()
                    if _nc_pickup:
                        try:
                            with _nc_pickup.cursor(cursor_factory=psycopg2.extras.DictCursor) as _ncur_pickup:
                                cli_token = _get_fcm_token(_ncur_pickup, 'client_profiles', str(_pickup_row['client_id']))
                                _notify(cli_token, "Pedido a caminho! 🛵", "Seu pedido foi retirado e esta sendo entregue",
                                        {"order_id": str(order_id), "status": "delivering"})
                        finally:
                            _nc_pickup.close()
            except Exception as _e:
                logger.warning(f"FCM pickup_order: {_e}")

            return jsonify({"status": "success", "message": "Pedido retirado e em rota de entrega."}), 200

    except Exception as e:
        logger.error(f"Erro em pickup_order: {e}", exc_info=True)
        if conn:
            conn.rollback()
        return jsonify({"error": "Erro interno do servidor"}), 500
    finally:
        if conn:
            conn.close()

@orders_bp.route('/<uuid:order_id>/complete', methods=['POST'])
def complete_order(order_id):
    logger.info(f"=== INÍCIO COMPLETE_ORDER para {order_id} ===")
    conn = None
    try:
        user_auth_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
        if error:
            return error
        if user_type not in ['restaurant', 'delivery']:
            return jsonify({"error": "Acesso não autorizado para completar a entrega"}), 403

        data = request.get_json()
        if not data or 'delivery_code' not in data:
            return jsonify({"error": "Código de entrega (delivery_code) é obrigatório"}), 400

        code = str(data['delivery_code']).strip().upper()

        conn = get_db_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                "SELECT status, delivery_code, restaurant_id, delivery_id, "
                "payment_method, total_amount, delivery_fee, comissao_plataforma "
                "FROM orders WHERE id = %s",
                (str(order_id),))
            order = cur.fetchone()
            if not order:
                return jsonify({"error": "Pedido não encontrado"}), 404

            # Confere posse do pedido alem do codigo -- o codigo de 4 chars
            # sozinho e forca-bruta-vel e nao deveria ser a unica barreira.
            if user_type == 'restaurant':
                cur.execute("SELECT id FROM restaurant_profiles WHERE user_id = %s", (user_auth_id,))
                prof = cur.fetchone()
                if not prof or str(prof['id']) != str(order['restaurant_id']):
                    return jsonify({"error": "Este pedido não pertence ao seu restaurante"}), 403
            else:
                cur.execute("SELECT id FROM delivery_profiles WHERE user_id = %s", (user_auth_id,))
                prof = cur.fetchone()
                if not prof or order['delivery_id'] is None or str(prof['id']) != str(order['delivery_id']):
                    return jsonify({"error": "Este pedido não está atribuído a você"}), 403

            if order['status'] != 'delivering':
                return jsonify({
                    "error": f"O pedido não está em rota de entrega. Status atual: {STATUS_DISPLAY_MAP.get(order['status'])}"
                }), 400

            if order['delivery_code'] != code:
                return jsonify({"error": "Código de entrega inválido"}), 403

            cur.execute(
                "UPDATE orders SET status = 'delivered', updated_at = NOW() WHERE id = %s",
                (str(order_id),)
            )
            # Busca client_id, delivery_id e restaurant_id antes de fechar o cursor
            cur.execute(
                "SELECT client_id, delivery_id, restaurant_id FROM orders WHERE id = %s",
                (str(order_id),)
            )
            completed_order = cur.fetchone()

            # Clube Inksa (entregador): aplica o benefício do nível do entregador
            # ao repasse DESTE pedido (bônus por entrega + % a mais do frete). Só
            # agora, que a entrega tem dono. Seed é 0 -> inerte até o admin configurar.
            try:
                if completed_order and completed_order['delivery_id'] and order['payment_method'] != 'cash':
                    from ..utils.club import delivery_level_benefits
                    _cb = delivery_level_benefits(str(completed_order['delivery_id']))
                    _bonus = float(_cb.get('per_delivery_bonus') or 0)
                    _keep = float(_cb.get('freight_keep_extra_pct') or 0)
                    if _bonus > 0 or _keep > 0:
                        cur.execute(
                            "SELECT COALESCE(delivery_fee,0) AS fee, COALESCE(valor_repassado_entregador,0) AS pay "
                            "FROM orders WHERE id = %s", (str(order_id),))
                        _r = cur.fetchone()
                        _fee = float(_r['fee']); _base = float(_r['pay'])
                        _freight_part = min(_fee, round(_base + _fee * _keep / 100.0, 2))
                        _new_pay = round(_freight_part + _bonus, 2)
                        _new_margem = round(_fee - _freight_part, 2)
                        cur.execute(
                            "UPDATE orders SET valor_repassado_entregador = %s, margem_frete = %s WHERE id = %s",
                            (_new_pay, _new_margem, str(order_id)))
                        logger.info(f"🏅 Clube entregador no pedido {order_id}: repasse {_base}->{_new_pay}")
            except Exception as _club_e:
                logger.warning(f"Falha ao aplicar clube do entregador: {_club_e}")

            # Pedido em DINHEIRO: liquida agora, no fechamento da entrega. O
            # entregador recolheu em espécie ao entregar, então já registramos o
            # split financeiro do pedido E a dívida dele com a plataforma — sem
            # depender de ele clicar "confirmar recebimento" (que podia ser
            # pulado por outra tela ou por "não agora", deixando o financeiro
            # zerado e a dívida perdida). É idempotente: se a confirmação manual
            # rodar depois, não duplica.
            cash_breakdown = None
            try:
                if order['payment_method'] == 'cash' and completed_order and completed_order['delivery_id']:
                    from ..utils.cash_settlement import settle_cash_order
                    cash_breakdown, _was_new = settle_cash_order(
                        cur, order_id,
                        completed_order['delivery_id'], completed_order['restaurant_id'],
                        order['total_amount'], order['delivery_fee'], order.get('comissao_plataforma'))
                    logger.info(f"💵 Pedido dinheiro {order_id} liquidado no fechamento (novo={_was_new})")
            except Exception as _cash_e:
                logger.error(f"Falha ao liquidar pedido em dinheiro {order_id}: {_cash_e}", exc_info=True)

            conn.commit()
            logger.info(f"✅ Pedido {order_id} marcado como entregue!")

            # FCM: notifica cliente que pedido foi entregue
            try:
                if completed_order and completed_order['client_id']:
                    _nc2 = get_db_connection()
                    if _nc2:
                        try:
                            with _nc2.cursor(cursor_factory=psycopg2.extras.DictCursor) as _ncur_del:
                                cli_token = _get_fcm_token(_ncur_del, 'client_profiles', str(completed_order['client_id']))
                                _notify(cli_token, "Pedido entregue! ⭐", "Avalie sua experiencia com o restaurante",
                                        {"order_id": str(order_id), "status": "delivered"})
                        finally:
                            _nc2.close()
            except Exception as _e:
                logger.warning(f"FCM complete_order: {_e}")

            # Concede pontos de gamificação (gracioso: não quebra o fluxo se falhar)
            if _award_completion_points and completed_order:
                try:
                    if completed_order['client_id']:
                        _award_completion_points(
                            str(completed_order['client_id']), 'client', str(order_id)
                        )
                        if _award_first_order_bonus:
                            _award_first_order_bonus(str(completed_order['client_id']), str(order_id))
                    if completed_order['delivery_id']:
                        _award_completion_points(
                            str(completed_order['delivery_id']), 'delivery', str(order_id)
                        )
                    if completed_order['restaurant_id']:
                        _award_completion_points(
                            str(completed_order['restaurant_id']), 'restaurant', str(order_id)
                        )
                except Exception as _gam_err:
                    logger.warning(f"Gamificação: falha ao conceder pontos para pedido {order_id}: {_gam_err}")

            _resp = {"status": "success", "message": "Pedido entregue com sucesso!"}
            if cash_breakdown:
                # Resumo do dinheiro pro app mostrar direto (o entregador recebeu
                # X, a taxa dele é Y, e ele deve Z à plataforma).
                _resp["cash"] = {
                    "voce_recebeu": cash_breakdown["total_amount"],
                    "sua_taxa": cash_breakdown["courier_freight"],
                    "deve_a_plataforma": cash_breakdown["cash_debt"],
                    "comissao": cash_breakdown["commission"],
                    "repasse_restaurante": cash_breakdown["restaurant_share"],
                }
            return jsonify(_resp), 200

    except Exception as e:
        logger.error(f"Erro em complete_order: {e}", exc_info=True)
        if conn:
            conn.rollback()
        return jsonify({"error": "Erro interno do servidor"}), 500
    finally:
        if conn:
            conn.close()

@orders_bp.route('/<uuid:order_id>/report-incident', methods=['POST'])
def report_delivery_incident(order_id):
    """Entregador reporta que não conseguiu concluir a entrega (ex.: cliente não localizado)."""
    logger.info(f"=== INÍCIO report_delivery_incident para {order_id} ===")
    conn = None
    try:
        user_auth_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
        if error:
            return error
        if user_type != 'delivery':
            return jsonify({"error": "Apenas o entregador pode reportar ocorrência de entrega"}), 403

        data = request.get_json() or {}
        reason = str(data.get('reason', '')).strip()
        if reason not in DELIVERY_INCIDENT_REASONS:
            return jsonify({"error": "Motivo da ocorrência inválido"}), 400
        notes = (data.get('notes') or '').strip() or None
        photo_url = (data.get('photo_url') or '').strip() or None
        contact_attempts = data.get('contact_attempts') or {}
        outcome = (data.get('outcome') or '').strip() or None
        if outcome and outcome not in DELIVERY_INCIDENT_OUTCOMES:
            return jsonify({"error": "Desfecho inválido"}), 400

        conn = get_db_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                "SELECT status, delivery_id, client_id, restaurant_id, total_amount, delivery_fee, "
                "status_pagamento, id_transacao_mp, payment_provider "
                "FROM orders WHERE id = %s",
                (str(order_id),),
            )
            order = cur.fetchone()
            if not order:
                return jsonify({"error": "Pedido não encontrado"}), 404

            cur.execute("SELECT id FROM delivery_profiles WHERE user_id = %s", (user_auth_id,))
            _prof = cur.fetchone()
            if not _prof or order['delivery_id'] is None or str(_prof['id']) != str(order['delivery_id']):
                return jsonify({"error": "Este pedido não está atribuído a você"}), 403

            # Só quem está com o pedido (em rota / aguardando retirada) pode reportar
            if order['status'] not in ('delivering', 'accepted_by_delivery', 'ready'):
                return jsonify({
                    "error": f"Não é possível reportar ocorrência no status: {STATUS_DISPLAY_MAP.get(order['status'], order['status'])}"
                }), 400

            # --- BOT decide o rumo (o entregador NÃO escolhe mais o desfecho) ---
            # Danificado por ele exige foto. Danificado OU restaurante fechado →
            # descartar direto. Senão → pergunta ao restaurante se quer a devolução
            # (fica 'awaiting_restaurant'; um job cai pra dispose após ~10min).
            if reason == 'courier_damaged' and not photo_url:
                return jsonify({"error": "Para 'derrubei/danifiquei o pedido', anexe uma foto-comprovante."}), 400

            restaurant_closed = False
            if order['restaurant_id']:
                cur.execute("SELECT is_open FROM restaurant_profiles WHERE id = %s", (str(order['restaurant_id']),))
                _r = cur.fetchone()
                restaurant_closed = bool(_r and _r['is_open'] is False)

            bot_outcome = 'dispose' if (reason == 'courier_damaged' or restaurant_closed) else 'awaiting_restaurant'
            return_code = generate_verification_code() if bot_outcome == 'awaiting_restaurant' else None

            cur.execute(
                "UPDATE orders SET status = 'delivery_failed', cancellation_reason = %s, updated_at = NOW() WHERE id = %s",
                (f"delivery_incident:{reason}", str(order_id)),
            )
            cur.execute(
                """INSERT INTO delivery_incidents
                       (order_id, delivery_id, reason, notes, photo_url, contact_attempts,
                        outcome, auto_decided, return_code)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE, %s) RETURNING id""",
                (str(order_id),
                 str(order['delivery_id']) if order['delivery_id'] else None,
                 reason, notes, photo_url, psycopg2.extras.Json(contact_attempts),
                 bot_outcome, return_code),
            )
            incident_id = cur.fetchone()['id']

            # --- Regra de dinheiro por culpa (padrão dos grandes deliverys) ---
            policy = DELIVERY_INCIDENT_POLICY.get(
                reason, {'fault': 'none', 'pay_restaurant': False, 'pay_courier': False, 'refund_client': False}
            )
            is_online = (order['status_pagamento'] == 'approved')
            # Zera o repasse de quem não deve receber; quem recebe fica com o valor já calculado
            zero_parts = []
            if not policy['pay_restaurant']:
                zero_parts.append("valor_repassado_restaurante = 0")
            if not policy['pay_courier']:
                zero_parts.append("valor_repassado_entregador = 0")
            if zero_parts:
                cur.execute(
                    f"UPDATE orders SET {', '.join(zero_parts)}, updated_at = NOW() WHERE id = %s",
                    (str(order_id),),
                )
            # Reembolso só se houver culpa não-cliente E pagamento online aprovado
            refund_amount = 0
            refund_status = 'not_due'
            if policy['refund_client'] and is_online:
                refund_amount = float(order['total_amount'] or 0)
                refund_status = 'pending' if refund_amount > 0 else 'not_due'
            cur.execute(
                "UPDATE delivery_incidents SET fault = %s, refund_amount = %s, refund_status = %s WHERE id = %s",
                (policy['fault'], refund_amount, refund_status, str(incident_id)),
            )

            conn.commit()
            logger.info(
                f"Ocorrência {incident_id} registrada para pedido {order_id} "
                f"(motivo={reason}, culpa={policy['fault']}, reembolso={refund_amount} {refund_status})"
            )

            # Reembolso AUTOMÁTICO (padrão dos grandes): tenta agora; se o MP falhar,
            # fica 'pending' para o admin processar pelo botão (fallback seguro).
            if refund_status == 'pending':
                try:
                    from ..utils.gateway import refund_order_payment
                    if order['id_transacao_mp']:
                        ok_refund, refund_detail = refund_order_payment(dict(order), current_app.mp_sdk)
                        if ok_refund:
                            cur.execute(
                                "UPDATE delivery_incidents SET refund_status = 'done', "
                                "resolution = CASE WHEN resolution = 'pending' THEN 'refunded' ELSE resolution END, "
                                "resolved_at = NOW() WHERE id = %s",
                                (str(incident_id),),
                            )
                            cur.execute(
                                "UPDATE orders SET status_pagamento = 'refunded', updated_at = NOW() WHERE id = %s",
                                (str(order_id),),
                            )
                            conn.commit()
                            logger.info(f"Reembolso automático OK: pedido {order_id} R${refund_amount}")
                        else:
                            logger.warning(f"Gateway recusou reembolso automático do pedido {order_id}: {refund_detail}")
                            sentry_sdk.capture_message(
                                f"MP recusou reembolso automático do pedido {order_id} — fica pendente para o admin.",
                                level="warning",
                            )
                except Exception as _re:
                    logger.warning(f"Reembolso automático falhou (fica pendente p/ admin): {_re}")
                    sentry_sdk.capture_exception(_re)
                    try:
                        conn.rollback()
                    except Exception:
                        pass

            # FCM: avisa o cliente que houve um problema com a entrega
            try:
                if order['client_id']:
                    _nc = get_db_connection()
                    if _nc:
                        try:
                            with _nc.cursor(cursor_factory=psycopg2.extras.DictCursor) as _ncur:
                                cli_token = _get_fcm_token(_ncur, 'client_profiles', str(order['client_id']))
                                _notify(cli_token, "Problema com sua entrega",
                                        "Tivemos um problema ao entregar seu pedido. Nossa equipe vai te contatar.",
                                        {"order_id": str(order_id), "status": "delivery_failed"})
                        finally:
                            _nc.close()
            except Exception as _e:
                logger.warning(f"FCM report_incident: {_e}")

        instruction = {
            'dispose': 'Pode descartar o pedido. Ocorrência registrada — nossa equipe cuida do resto.',
            'awaiting_restaurant': 'Aguarde: o restaurante vai dizer se quer a devolução. Você será avisado aqui.',
        }.get(bot_outcome, '')
        return jsonify({
            "status": "success",
            "incident_id": str(incident_id),
            "order_status": "delivery_failed",
            "outcome": bot_outcome,          # 'dispose' | 'awaiting_restaurant'
            "return_code": return_code,      # null enquanto não há devolução confirmada
            "instruction": instruction,
        }), 200

    except Exception as e:
        logger.error(f"Erro em report_delivery_incident: {e}", exc_info=True)
        if conn:
            conn.rollback()
        return jsonify({"error": "Erro interno do servidor"}), 500
    finally:
        if conn:
            conn.close()


@orders_bp.route('/<uuid:order_id>/incident/restaurant-decision', methods=['POST'])
def incident_restaurant_decision(order_id):
    """Restaurante responde se QUER a devolução de um pedido com ocorrência.
    body {want_return: bool}. Sim → fica pra devolver (entregador leva com o
    código); Não → descartar. Só o restaurante dono do pedido (ou admin)."""
    user_auth_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
    if error:
        return error
    if user_type not in ('restaurant', 'admin'):
        return jsonify({"error": "Não autorizado"}), 403
    want_return = bool((request.get_json() or {}).get('want_return'))
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Erro de conexão"}), 500
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                """SELECT di.id, di.outcome, o.restaurant_id
                     FROM delivery_incidents di JOIN orders o ON o.id = di.order_id
                    WHERE di.order_id = %s ORDER BY di.created_at DESC LIMIT 1""",
                (str(order_id),))
            inc = cur.fetchone()
            if not inc:
                return jsonify({"error": "Ocorrência não encontrada"}), 404
            if user_type == 'restaurant':
                cur.execute("SELECT id FROM restaurant_profiles WHERE user_id = %s", (user_auth_id,))
                p = cur.fetchone()
                if not p or str(p['id']) != str(inc['restaurant_id']):
                    return jsonify({"error": "Este pedido não é do seu restaurante"}), 403
            if inc['outcome'] != 'awaiting_restaurant':
                return jsonify({"error": "Esta ocorrência já foi decidida"}), 400
            if want_return:
                cur.execute("UPDATE delivery_incidents SET outcome = 'return_to_restaurant' WHERE id = %s", (str(inc['id']),))
                msg = "Devolução solicitada — o entregador leva o pedido e você confirma com o código."
            else:
                cur.execute("UPDATE delivery_incidents SET outcome = 'dispose', return_code = NULL WHERE id = %s", (str(inc['id']),))
                msg = "Ok — o entregador vai descartar o pedido."
            conn.commit()
        return jsonify({"status": "success", "message": msg}), 200
    except Exception:
        logger.exception("Erro em incident_restaurant_decision")
        try: conn.rollback()
        except Exception: pass
        return jsonify({"error": "Erro interno do servidor"}), 500
    finally:
        conn.close()


@orders_bp.route('/<uuid:order_id>/incident/confirm-return', methods=['POST'])
def incident_confirm_return(order_id):
    """Restaurante CONFIRMA que recebeu a devolução, validando o código que o
    entregador mostra. Ao confirmar, se a culpa NÃO for do entregador, credita a
    taxa de retorno (= frete cheio) ao entregador. Admin pode confirmar sem código."""
    user_auth_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
    if error:
        return error
    if user_type not in ('restaurant', 'admin'):
        return jsonify({"error": "Não autorizado"}), 403
    code = str((request.get_json() or {}).get('return_code') or '').strip().upper()
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Erro de conexão"}), 500
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                """SELECT di.id, di.outcome, di.return_code, di.return_confirmed_at, di.fault,
                          di.delivery_id, o.restaurant_id, o.delivery_fee
                     FROM delivery_incidents di JOIN orders o ON o.id = di.order_id
                    WHERE di.order_id = %s ORDER BY di.created_at DESC LIMIT 1""",
                (str(order_id),))
            inc = cur.fetchone()
            if not inc:
                return jsonify({"error": "Ocorrência não encontrada"}), 404
            if user_type == 'restaurant':
                cur.execute("SELECT id FROM restaurant_profiles WHERE user_id = %s", (user_auth_id,))
                p = cur.fetchone()
                if not p or str(p['id']) != str(inc['restaurant_id']):
                    return jsonify({"error": "Este pedido não é do seu restaurante"}), 403
            if inc['outcome'] != 'return_to_restaurant':
                return jsonify({"error": "Não há devolução pendente para este pedido"}), 400
            if inc['return_confirmed_at']:
                return jsonify({"error": "Devolução já confirmada"}), 400
            # Restaurante precisa do código certo; admin pode confirmar sem código.
            if user_type == 'restaurant' and (not code or code != (inc['return_code'] or '').upper()):
                return jsonify({"error": "Código de devolução inválido"}), 403

            cur.execute(
                "UPDATE delivery_incidents SET return_confirmed_at = NOW(), "
                "resolution = CASE WHEN resolution = 'pending' THEN 'returned' ELSE resolution END, "
                "resolved_at = COALESCE(resolved_at, NOW()) WHERE id = %s",
                (str(inc['id']),))
            # Taxa de retorno = frete cheio, só quando NÃO é culpa do entregador.
            return_fee = 0.0
            if inc['fault'] != 'courier' and inc['delivery_id']:
                return_fee = round(float(inc['delivery_fee'] or 0), 2)
                if return_fee > 0:
                    cur.execute(
                        "UPDATE orders SET valor_repassado_entregador = COALESCE(valor_repassado_entregador,0) + %s, updated_at = NOW() WHERE id = %s",
                        (return_fee, str(order_id)))
            conn.commit()
        return jsonify({"status": "success", "message": "Devolução confirmada.", "return_fee": return_fee}), 200
    except Exception:
        logger.exception("Erro em incident_confirm_return")
        try: conn.rollback()
        except Exception: pass
        return jsonify({"error": "Erro interno do servidor"}), 500
    finally:
        conn.close()


@orders_bp.route('/incidents/restaurant', methods=['GET'])
def list_restaurant_incidents():
    """Ocorrências ATIVAS dos pedidos do restaurante logado: as que aguardam ele
    decidir a devolução, ou a devolução ainda não confirmada. NÃO devolve o
    return_code (o entregador é quem mostra; o restaurante digita)."""
    user_auth_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
    if error:
        return error
    if user_type != 'restaurant':
        return jsonify({"error": "Não autorizado"}), 403
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Erro de conexão"}), 500
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT id FROM restaurant_profiles WHERE user_id = %s", (user_auth_id,))
            prof = cur.fetchone()
            if not prof:
                return jsonify({"status": "success", "data": []}), 200
            cur.execute(
                """SELECT di.id, di.order_id, di.reason, di.outcome, di.created_at
                     FROM delivery_incidents di JOIN orders o ON o.id = di.order_id
                    WHERE o.restaurant_id = %s
                      AND di.outcome IN ('awaiting_restaurant', 'return_to_restaurant')
                      AND di.return_confirmed_at IS NULL
                    ORDER BY di.created_at DESC LIMIT 50""",
                (str(prof['id']),))
            rows = cur.fetchall()
        data = [{
            "id": str(r["id"]),
            "order_id": str(r["order_id"]) if r["order_id"] else None,
            "order_ref": (str(r["order_id"])[:8].upper() if r["order_id"] else ""),
            "reason": r["reason"],
            "outcome": r["outcome"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        } for r in rows]
        return jsonify({"status": "success", "data": data}), 200
    except Exception:
        logger.exception("Erro em list_restaurant_incidents")
        return jsonify({"error": "Erro interno do servidor"}), 500
    finally:
        conn.close()


@orders_bp.route('/<uuid:order_id>/incident-photo', methods=['POST'])
def upload_incident_photo(order_id):
    """Entregador envia uma foto-comprovante da ocorrência (ex.: foto do local)."""
    user_auth_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
    if error:
        return error
    if user_type != 'delivery':
        return jsonify({"error": "Apenas o entregador pode enviar a foto"}), 403
    if not supabase:
        return jsonify({"error": "Storage indisponível"}), 503
    if 'file' not in request.files:
        return jsonify({"error": "Nenhum arquivo enviado com o campo 'file'"}), 400
    file = request.files['file']
    if not file or file.filename == '':
        return jsonify({"error": "Arquivo inválido"}), 400
    try:
        import os as _os
        ext = _os.path.splitext(file.filename)[1] or '.jpg'
        unique = f"incident_{order_id}_{uuid.uuid4()}{ext}"
        supabase.storage.from_("incident-photos").upload(
            path=unique,
            file=file.read(),
            file_options={"content-type": file.mimetype or "image/jpeg", "upsert": "true"},
        )
        public_url = supabase.storage.from_("incident-photos").get_public_url(unique)
        return jsonify({"status": "success", "photo_url": public_url}), 200
    except Exception as e:
        logger.error(f"Erro ao enviar foto da ocorrência {order_id}: {e}", exc_info=True)
        return jsonify({"error": "Erro ao enviar a foto"}), 500

@orders_bp.route('/<uuid:order_id>/confirm-return', methods=['POST'])
def confirm_delivery_return(order_id):
    """Entregador confirma que devolveu o pedido ao restaurante (encerra a devolução)."""
    conn = None
    try:
        user_auth_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
        if error:
            return error
        if user_type != 'delivery':
            return jsonify({"error": "Apenas o entregador pode confirmar a devolução"}), 403

        conn = get_db_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT id, delivery_id FROM orders WHERE id = %s", (str(order_id),))
            _order = cur.fetchone()
            if not _order:
                return jsonify({"error": "Pedido não encontrado"}), 404
            cur.execute("SELECT id FROM delivery_profiles WHERE user_id = %s", (user_auth_id,))
            _prof = cur.fetchone()
            if not _prof or _order['delivery_id'] is None or str(_prof['id']) != str(_order['delivery_id']):
                return jsonify({"error": "Este pedido não está atribuído a você"}), 403

            cur.execute(
                """UPDATE delivery_incidents
                      SET resolution = 'returned', resolved_at = NOW(),
                          outcome = COALESCE(outcome, 'return_to_restaurant')
                    WHERE id = (SELECT id FROM delivery_incidents
                                 WHERE order_id = %s
                              ORDER BY created_at DESC LIMIT 1)
                  RETURNING id""",
                (str(order_id),),
            )
            row = cur.fetchone()
            conn.commit()
            if not row:
                return jsonify({"error": "Ocorrência não encontrada para este pedido"}), 404
        return jsonify({"status": "success", "message": "Devolução confirmada"}), 200

    except Exception as e:
        logger.error(f"Erro em confirm_delivery_return: {e}", exc_info=True)
        if conn:
            conn.rollback()
        return jsonify({"error": "Erro interno do servidor"}), 500
    finally:
        if conn:
            conn.close()

@orders_bp.route('/<uuid:order_id>', methods=['GET'])
@limiter.limit("60 per minute")
def get_order(order_id):
    """Detalhe de UM pedido, escopado a quem tem direito de ver.

    Esta rota nao existia: so havia DELETE neste caminho, entao a tela de
    acompanhamento do cliente (GET /api/orders/<id>) tomava 405 e mostrava
    "Pedido nao encontrado" em TODO pedido.

    O lat/lng/endereco do restaurante vem do JOIN (nao existem em orders) —
    e o que desenha o mapa. O pickup_code (segredo entre restaurante e
    entregador) e sempre removido. O delivery_code fica visivel SO pro cliente
    dono do pedido — e ele quem mostra esse codigo ao entregador na entrega, e
    a tela de acompanhamento exibe pra nao precisar sair pra outra tela.
    """
    conn = None
    try:
        user_auth_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
        if error:
            return error

        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Erro de conexão com o banco de dados"}), 500

        query = """
            SELECT o.*,
                   rp.restaurant_name,
                   rp.logo_url  AS restaurant_logo,
                   rp.phone     AS restaurant_phone,
                   rp.latitude  AS restaurant_latitude,
                   rp.longitude AS restaurant_longitude,
                   NULLIF(TRIM(CONCAT_WS(', ',
                     NULLIF(CONCAT_WS(' ', rp.address_street, rp.address_number), ''),
                     rp.address_neighborhood,
                     rp.address_city)), '') AS restaurant_address,
                   cp.first_name AS client_first_name,
                   cp.last_name  AS client_last_name
              FROM orders o
              LEFT JOIN restaurant_profiles rp ON o.restaurant_id = rp.id
              LEFT JOIN client_profiles cp ON o.client_id = cp.id
             WHERE o.id = %s
        """
        params = [str(order_id)]

        # Escopo por dono — sem isto qualquer logado leria o pedido de qualquer
        # outro so trocando o id na URL.
        if user_type == 'client':
            query += " AND o.client_id = (SELECT id FROM client_profiles WHERE user_id = %s)"
            params.append(user_auth_id)
        elif user_type == 'restaurant':
            query += " AND o.restaurant_id = (SELECT id FROM restaurant_profiles WHERE user_id = %s)"
            params.append(user_auth_id)
        elif user_type == 'delivery':
            query += " AND o.delivery_id = (SELECT id FROM delivery_profiles WHERE user_id = %s)"
            params.append(user_auth_id)
        elif user_type != 'admin':
            return jsonify({"error": "Não autorizado"}), 403

        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(query, tuple(params))
            row = cur.fetchone()

        if not row:
            return jsonify({"error": "Pedido não encontrado"}), 404

        order = dict(row)
        order.pop('pickup_code', None)
        # O delivery_code (4 letras) é o que o CLIENTE mostra ao entregador pra
        # confirmar a entrega — então o próprio cliente PRECISA vê-lo na tela de
        # acompanhamento. Continua escondido pros demais (o entregador pega do
        # cliente na hora).
        if user_type != 'client':
            order.pop('delivery_code', None)
        return jsonify({"status": "success", "data": order}), 200

    except Exception as e:
        logger.error(f"Erro em get_order: {e}", exc_info=True)
        return jsonify({"error": "Erro interno do servidor"}), 500
    finally:
        if conn:
            conn.close()


@orders_bp.route('/valid-statuses', methods=['GET'])
def get_valid_statuses():
    logger.info("=== INÍCIO get_valid_statuses ===")
    try:
        user_auth_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
        if error:
            return error
        if user_type == 'restaurant':
            available_statuses = ['Aceito', 'Preparando', 'Pronto', 'Cancelado']
        elif user_type == 'client':
            available_statuses = ['Cancelado']
        else:
            available_statuses = []
        return jsonify({"status": "success", "valid_statuses": available_statuses}), 200
    except Exception as e:
        logger.error(f"Erro ao obter status válidos: {e}", exc_info=True)
        return jsonify({"error": "Erro interno do servidor"}), 500

@orders_bp.route('/<uuid:order_id>/status-history', methods=['GET'])
def get_order_status_history(order_id):
    logger.info("=== INÍCIO get_order_status_history ===")
    conn = None
    try:
        user_auth_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
        if error:
            return error

        conn = get_db_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            if user_type == 'restaurant':
                cur.execute("""
                    SELECT o.* FROM orders o
                    JOIN restaurant_profiles rp ON o.restaurant_id = rp.id
                    WHERE o.id = %s AND rp.user_id = %s
                """, (str(order_id), user_auth_id))
            elif user_type == 'client':
                cur.execute("""
                    SELECT o.* FROM orders o
                    JOIN client_profiles cp ON o.client_id = cp.id
                    WHERE o.id = %s AND cp.user_id = %s
                """, (str(order_id), user_auth_id))
            else:
                return jsonify({"error": "Acesso não autorizado"}), 403

            order = cur.fetchone()
            if not order:
                return jsonify({"error": "Pedido não encontrado ou acesso negado"}), 404

            history = [{
                "status": STATUS_DISPLAY_MAP.get(order['status'], order['status']),
                "timestamp": order['updated_at'].isoformat(),
                "changed_by": "system"
            }]
            return jsonify({"status": "success", "order_id": str(order_id), "history": history}), 200

    except Exception as e:
        logger.error(f"Erro ao obter histórico do pedido: {e}", exc_info=True)
        return jsonify({"error": "Erro interno do servidor"}), 500
    finally:
        if conn:
            conn.close()

@orders_bp.route('/pending-client-review', methods=['GET'])
def get_pending_client_reviews():
    logger.info("=== INÍCIO get_pending_client_reviews ===")
    conn = None
    try:
        user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
        if error:
            return error
        if user_type != 'client':
            return jsonify({'error': 'Acesso negado. Apenas para clientes.'}), 403

        conn = get_db_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT id FROM client_profiles WHERE user_id = %s", (user_id,))
            client_profile = cur.fetchone()
            if not client_profile:
                return jsonify({'error': 'Perfil de cliente não encontrado.'}), 404
            client_id = client_profile['id']

            sql_query = """
                SELECT o.id, o.restaurant_id, rp.restaurant_name, o.delivery_id as deliveryman_id,
                       (dp.first_name || ' ' || dp.last_name) as deliveryman_name,
                       o.updated_at as completed_at
                FROM orders o
                JOIN restaurant_profiles rp ON o.restaurant_id = rp.id
                LEFT JOIN delivery_profiles dp ON o.delivery_id = dp.id
                WHERE o.client_id = %s AND o.status = 'delivered'
                  AND (
                        NOT EXISTS (
                          SELECT 1 FROM restaurant_reviews rr
                          WHERE rr.order_id = o.id AND rr.client_id = %s
                        )
                        OR (
                          o.delivery_id IS NOT NULL AND NOT EXISTS (
                            SELECT 1 FROM delivery_reviews dr
                            WHERE dr.order_id = o.id AND dr.client_id = %s
                          )
                        )
                      )
                ORDER BY o.updated_at DESC;
            """
            cur.execute(sql_query, (client_id, client_id, client_id))
            orders_to_review = [dict(row) for row in cur.fetchall()]
            return jsonify(orders_to_review), 200

    except Exception as e:
        logger.error(f"Erro em get_pending_client_reviews: {e}", exc_info=True)
        return jsonify({'error': 'Erro interno do servidor.'}), 500
    finally:
        if conn:
            conn.close()

@orders_bp.route('/pending-delivery-review', methods=['GET', 'OPTIONS'])
def get_pending_delivery_review():
    logger.info("=== INÍCIO get_pending_delivery_review ===")
    conn = None
    try:
        user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
        if error:
            return error
        if user_type != 'delivery':
            return jsonify({'error': 'Acesso negado. Apenas para entregadores.'}), 403

        conn = get_db_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT id FROM delivery_profiles WHERE user_id = %s", (user_id,))
            delivery_profile = cur.fetchone()
            if not delivery_profile:
                return jsonify({'error': 'Perfil de entregador não encontrado.'}), 404
            delivery_id = delivery_profile['id']

            # Pendente = pedido entregue por este entregador em que ELE ainda não
            # avaliou o CLIENTE. A avaliação entregador->cliente mora em
            # client_reviews (reviewer_type='delivery') — NÃO em delivery_reviews
            # (que é cliente->entregador). Checar delivery_reviews fazia o pedido
            # sumir da lista assim que o CLIENTE avaliava o entregador.
            sql_query = """
                SELECT o.id, o.restaurant_id, rp.restaurant_name, o.client_id,
                       (cp.first_name || ' ' || cp.last_name) as client_name,
                       o.updated_at as delivered_at, o.total_amount
                FROM orders o
                JOIN restaurant_profiles rp ON o.restaurant_id = rp.id
                JOIN client_profiles cp ON o.client_id = cp.id
                WHERE o.delivery_id = %s AND o.status = 'delivered'
                  AND NOT EXISTS (
                    SELECT 1 FROM client_reviews cr
                    WHERE cr.order_id = o.id AND cr.reviewer_type = 'delivery'
                  )
                ORDER BY o.updated_at DESC;
            """
            cur.execute(sql_query, (delivery_id,))
            orders_to_review = [dict(row) for row in cur.fetchall()]
            return jsonify(orders_to_review), 200

    except Exception as e:
        logger.error(f"Erro em get_pending_delivery_review: {e}", exc_info=True)
        return jsonify({'error': 'Erro interno do servidor.'}), 500
    finally:
        if conn:
            conn.close()

@orders_bp.route('/pending-restaurant-review', methods=['GET', 'OPTIONS'])
def get_pending_restaurant_review():
    """Pedidos entregues em que o RESTAURANTE ainda não avaliou o cliente.

    O app do restaurante chamava /pending-client-review (exclusivo de cliente,
    dava 403), então nunca aparecia nada pra avaliar. Aqui o restaurante vê seus
    pedidos entregues e avalia cliente + entregador (o card mostra os dois forms).
    'Pendente' = ainda não existe review do restaurante sobre o cliente
    (client_reviews.reviewer_type='restaurant')."""
    logger.info("=== INÍCIO get_pending_restaurant_review ===")
    conn = None
    try:
        user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
        if error:
            return error
        if user_type != 'restaurant':
            return jsonify({'error': 'Acesso negado. Apenas para restaurantes.'}), 403

        conn = get_db_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT id FROM restaurant_profiles WHERE user_id = %s", (user_id,))
            rp = cur.fetchone()
            if not rp:
                return jsonify({'error': 'Perfil de restaurante não encontrado.'}), 404
            restaurant_id = rp['id']

            sql_query = """
                SELECT o.id, o.client_id,
                       (cp.first_name || ' ' || cp.last_name) as client_name,
                       o.delivery_id as deliveryman_id,
                       (dp.first_name || ' ' || dp.last_name) as deliveryman_name,
                       o.updated_at as completed_at, o.total_amount
                FROM orders o
                JOIN client_profiles cp ON o.client_id = cp.id
                LEFT JOIN delivery_profiles dp ON o.delivery_id = dp.id
                WHERE o.restaurant_id = %s AND o.status = 'delivered'
                  AND NOT EXISTS (
                    SELECT 1 FROM client_reviews cr
                    WHERE cr.order_id = o.id AND cr.reviewer_type = 'restaurant'
                  )
                ORDER BY o.updated_at DESC;
            """
            cur.execute(sql_query, (restaurant_id,))
            orders_to_review = [dict(row) for row in cur.fetchall()]
            return jsonify(orders_to_review), 200

    except Exception as e:
        logger.error(f"Erro em get_pending_restaurant_review: {e}", exc_info=True)
        return jsonify({'error': 'Erro interno do servidor.'}), 500
    finally:
        if conn:
            conn.close()

@orders_bp.route('/available', methods=['GET'])
def get_available_orders():
    """Retorna pedidos disponíveis para o entregador:
       - status 'ready' e delivery_id IS NULL
       - status 'accepted_by_delivery' e delivery_id IS NULL
    """
    logger.info("=== INÍCIO get_available_orders ===")
    conn = None
    try:
        user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
        if error:
            logger.error(f"Erro de autenticação: {error}")
            return error

        if user_type != 'delivery':
            logger.warning(f"Acesso negado para user_type: {user_type}")
            return jsonify({'error': 'Acesso negado. Apenas para entregadores.'}), 403

        conn = get_db_connection()
        if not conn:
            logger.error("Falha ao conectar ao banco de dados")
            return jsonify({'error': 'Erro de conexão com banco de dados'}), 500

        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # Localização do entregador: usa o GPS ao vivo (current_lat/lng) e,
            # se não tiver, o endereço cadastrado (latitude/longitude).
            cur.execute(
                "SELECT first_name, phone, cpf, vehicle_type, vehicle_plate, cnh, "
                "latitude AS addr_lat, longitude AS addr_lng, "
                "COALESCE(current_lat, latitude) AS lat, "
                "COALESCE(current_lng, longitude) AS lng, "
                "COALESCE(is_available, FALSE) AS is_available "
                "FROM delivery_profiles WHERE user_id = %s", (user_id,))
            _dp = cur.fetchone()

            # Entregador OFFLINE não recebe pedido nenhum. Sem isto, o app
            # continuava listando (e alarmando) pedidos com o entregador ON/OFF
            # em OFF — ele via/aceitava entrega estando indisponível.
            if not _dp or not _dp['is_available']:
                logger.info("Entregador OFFLINE (is_available=false) — retornando lista vazia")
                return jsonify([]), 200

            # GATE de cadastro completo NO BACKEND (autoritativo). O app já esconde
            # o botão de ficar online, mas o backend não validava nada — então um
            # entregador incompleto (ou com is_available antigo) ainda recebia
            # pedidos. Pior: SEM COORDENADAS o filtro de raio caía no fail-open e
            # ele via pedidos de QUALQUER cidade. Aqui é a trava de verdade.
            _missing = []
            if not (_dp['first_name'] or '').strip():   _missing.append('nome')
            if not (_dp['phone'] or '').strip():        _missing.append('telefone')
            if not (_dp['cpf'] or '').strip():          _missing.append('cpf')
            if not (_dp['vehicle_type'] or '').strip(): _missing.append('veículo')
            # Sem endereço geocodificado não há como filtrar por raio → bloqueia.
            if _dp['addr_lat'] is None or _dp['addr_lng'] is None:
                _missing.append('endereço')
            # Veículo motorizado exige placa E CNH (carteira de motorista).
            if _dp['vehicle_type'] in ('moto', 'carro'):
                if not (_dp['vehicle_plate'] or '').strip():
                    _missing.append('placa')
                if not (_dp['cnh'] or '').strip():
                    _missing.append('CNH')
            if _missing:
                logger.info("Entregador com cadastro incompleto (%s) — lista vazia", ", ".join(_missing))
                return jsonify([]), 200

            drv_lat = _dp['lat'] if _dp else None
            drv_lng = _dp['lng'] if _dp else None

            # Filtro por RAIO: o entregador só vê pedidos cujo RESTAURANTE está
            # dentro de service_radius_km dele — é o que separa as cidades (cada
            # entregador vê só o que é da sua região). Fail-open: sem coordenadas
            # do entregador (ou restaurante sem coords), não filtra pra não sumir.
            radius_clause = ""
            params = []
            if drv_lat is not None and drv_lng is not None:
                from ..utils.platform_settings import get_settings
                _settings = get_settings()
                # Raio POR TIPO DE VEÍCULO: bike alcança menos que moto/carro.
                # Se o específico estiver 0/vazio (ou for 'outro'), usa o global.
                _radius_key = {
                    'bicicleta': 'delivery_radius_bike_km',
                    'moto':      'delivery_radius_moto_km',
                    'carro':     'delivery_radius_carro_km',
                }.get(_dp['vehicle_type'])
                radius_km = 0.0
                if _radius_key:
                    try:
                        radius_km = float(_settings.get(_radius_key) or 0)
                    except (TypeError, ValueError):
                        radius_km = 0.0
                if radius_km <= 0:
                    radius_km = float(_settings["platform_max_delivery_radius"])
                radius_clause = (
                    " AND (rp.latitude IS NULL OR rp.longitude IS NULL OR "
                    "earth_distance(ll_to_earth(rp.latitude, rp.longitude), "
                    "ll_to_earth(%s, %s)) <= %s)"
                )
                params += [float(drv_lat), float(drv_lng), radius_km * 1000.0]

            sql_query = f"""
                SELECT
                    o.id,
                    o.restaurant_id,
                    COALESCE(rp.restaurant_name, 'Restaurante') AS restaurant_name,
                    CONCAT_WS(', ',
                        rp.address_street,
                        rp.address_number,
                        rp.address_neighborhood,
                        rp.address_city,
                        rp.address_state
                    ) AS restaurant_address,
                    o.delivery_address,
                    COALESCE(o.total_amount, 0) AS total_amount,
                    COALESCE(o.total_amount_items, 0) AS total_amount_items,
                    COALESCE(o.delivery_fee, 0) AS delivery_fee,
                    COALESCE(o.valor_repassado_entregador, 0) AS valor_repassado_entregador,
                    o.items,
                    o.status,
                    o.created_at
                FROM orders o
                LEFT JOIN restaurant_profiles rp ON o.restaurant_id = rp.id
                WHERE
                    (o.status = 'ready' OR o.status = 'accepted_by_delivery')
                    AND o.delivery_id IS NULL
                    {radius_clause}
                ORDER BY o.created_at ASC;
            """
            cur.execute(sql_query, params)
            rows = cur.fetchall()

            available_orders = []
            for row in rows:
                order_dict = dict(row)

                if isinstance(order_dict.get('delivery_address'), str):
                    try:
                        order_dict['delivery_address'] = json.loads(order_dict['delivery_address'])
                    except (json.JSONDecodeError, TypeError):
                        pass

                if order_dict.get('created_at'):
                    order_dict['created_at'] = order_dict['created_at'].isoformat()
                if order_dict.get('id'):
                    order_dict['id'] = str(order_dict['id'])
                if order_dict.get('restaurant_id'):
                    order_dict['restaurant_id'] = str(order_dict['restaurant_id'])
                if order_dict.get('total_amount') is not None:
                    order_dict['total_amount'] = float(order_dict['total_amount'])
                if order_dict.get('total_amount_items') is not None:
                    order_dict['total_amount_items'] = float(order_dict['total_amount_items'])
                if order_dict.get('delivery_fee') is not None:
                    order_dict['delivery_fee'] = float(order_dict['delivery_fee'])
                if order_dict.get('valor_repassado_entregador') is not None:
                    order_dict['valor_repassado_entregador'] = float(order_dict['valor_repassado_entregador'])

                available_orders.append(order_dict)

            logger.info(f"✅ Processados {len(available_orders)} pedidos disponíveis com sucesso")
            return jsonify(available_orders), 200

    except Exception as e:
        logger.error(f"❌ Erro crítico em get_available_orders: {e}", exc_info=True)
        return jsonify({'error': 'Erro interno do servidor ao buscar entregas disponíveis.'}), 500
    finally:
        if conn:
            conn.close()
            logger.info("Conexão com banco fechada em get_available_orders")

@orders_bp.route('/<uuid:order_id>/accept', methods=['POST'])
def accept_order_by_delivery(order_id):
    """Entregador aceita pedido disponível (ready ou accepted_by_delivery)"""
    logger.info(f"=== INÍCIO accept_order_by_delivery para {order_id} ===")
    conn = None
    try:
        user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
        if error:
            logger.error(f"Erro de autenticação: {error}")
            return error

        if user_type != 'delivery':
            logger.warning(f"Acesso negado para user_type: {user_type}")
            return jsonify({'error': 'Apenas entregadores podem aceitar pedidos'}), 403

        conn = get_db_connection()
        if not conn:
            logger.error("Falha ao conectar ao banco de dados")
            return jsonify({'error': 'Erro de conexão com banco de dados'}), 500

        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT id FROM delivery_profiles WHERE user_id = %s", (user_id,))
            delivery_profile = cur.fetchone()
            if not delivery_profile:
                logger.error(f"Perfil de entregador não encontrado para user_id={user_id}")
                return jsonify({'error': 'Perfil de entregador não encontrado'}), 404

            delivery_profile_id = delivery_profile['id']

            cur.execute("""
                SELECT id, status, delivery_id
                FROM orders
                WHERE id = %s
            """, (str(order_id),))
            order = cur.fetchone()
            if not order:
                logger.error(f"Pedido {order_id} não encontrado")
                return jsonify({'error': 'Pedido não encontrado'}), 404

            if order['status'] not in ['ready', 'accepted_by_delivery']:
                logger.warning(f"Pedido {order_id} não está disponível. Status: {order['status']}")
                return jsonify({'error': f'Pedido não está disponível. Status: {order["status"]}'}), 400

            if order['delivery_id'] is not None:
                logger.warning(f"Pedido {order_id} já aceito por outro entregador")
                return jsonify({'error': 'Pedido já foi aceito por outro entregador'}), 409

            # UPDATE atomico: a condicao "delivery_id IS NULL" no proprio WHERE
            # e quem garante a exclusividade, nao a checagem acima (que so evita
            # uma query desnecessaria) -- se dois entregadores chegarem aqui ao
            # mesmo tempo, so um UPDATE afeta uma linha; o outro recebe 0 linhas
            # e 409, em vez de os dois sobrescreverem o delivery_id silenciosamente.
            cur.execute("""
                UPDATE orders
                SET delivery_id = %s,
                    status = 'accepted_by_delivery',
                    updated_at = NOW()
                WHERE id = %s
                  AND delivery_id IS NULL
                  AND status IN ('ready', 'accepted_by_delivery')
                RETURNING *
            """, (delivery_profile_id, str(order_id)))

            updated_row = cur.fetchone()
            if not updated_row:
                conn.rollback()
                logger.warning(f"Pedido {order_id} perdeu a corrida de aceite (já atribuído a outro entregador)")
                return jsonify({'error': 'Pedido já foi aceito por outro entregador'}), 409

            updated_order = dict(updated_row)
            conn.commit()

            # Normaliza tipos para JSON
            for k in ('id', 'restaurant_id', 'delivery_id', 'client_id'):
                if updated_order.get(k):
                    updated_order[k] = str(updated_order[k])
            for t in ('created_at', 'updated_at'):
                if updated_order.get(t):
                    updated_order[t] = updated_order[t].isoformat()

            updated_order.pop('pickup_code', None)
            updated_order.pop('delivery_code', None)

            if _award_points_for_action:
                try:
                    _award_points_for_action(
                        user_id=str(delivery_profile_id),
                        action_key="order_accepted_delivery",
                        order_id=str(order_id),
                        description="Pedido aceito",
                    )
                except Exception as _gam_err:
                    logger.warning(f"Gamificação: falha ao conceder pontos de aceite para {order_id}: {_gam_err}")

            logger.info(f"✅ Pedido {order_id} aceito pelo entregador {delivery_profile_id}")
            return jsonify({
                'status': 'success',
                'message': 'Pedido aceito! Vá ao restaurante para retirar.',
                'order': updated_order
            }), 200

    except Exception as e:
        logger.error(f"❌ Erro crítico em accept_order_by_delivery: {e}", exc_info=True)
        if conn:
            conn.rollback()
        return jsonify({'error': 'Erro interno do servidor ao aceitar pedido'}), 500
    finally:
        if conn:
            conn.close()
            logger.info("Conexão com banco fechada em accept_order_by_delivery")

@orders_bp.route('/<uuid:order_id>/restaurant-accept', methods=['PATCH'])
def restaurant_accept_order(order_id):
    """Restaurante aceita pedido informando tempo estimado de preparo."""
    conn = None
    try:
        user_auth_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
        if error:
            return error
        if user_type != 'restaurant':
            return jsonify({"error": "Apenas restaurantes podem aceitar pedidos"}), 403

        data = request.get_json(silent=True) or {}
        estimated_prep_time = data.get('estimated_time')  # minutos (int)
        if estimated_prep_time is not None:
            try:
                estimated_prep_time = int(estimated_prep_time)
            except (ValueError, TypeError):
                estimated_prep_time = None

        conn = get_db_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                SELECT o.id, o.status, o.client_id
                FROM orders o
                JOIN restaurant_profiles rp ON o.restaurant_id = rp.id
                WHERE o.id = %s AND rp.user_id = %s
            """, (str(order_id), user_auth_id))
            order = cur.fetchone()
            if not order:
                return jsonify({"error": "Pedido não encontrado ou não pertence a este restaurante"}), 404

            if order['status'] != 'pending':
                return jsonify({"error": f"Pedido não está pendente (status atual: {order['status']})"}), 400

            # UPDATE atomico: o "AND status = 'pending'" no WHERE e quem garante
            # que dois cliques/requisicoes concorrentes nao processem o aceite
            # duas vezes -- a checagem acima so evita o UPDATE desnecessario.
            cur.execute("""
                UPDATE orders
                SET status = 'accepted',
                    accepted_at = NOW(),
                    estimated_prep_time = %s,
                    updated_at = NOW()
                WHERE id = %s AND status = 'pending'
                RETURNING *
            """, (estimated_prep_time, str(order_id)))
            updated_row = cur.fetchone()
            if not updated_row:
                conn.rollback()
                return jsonify({"error": "Pedido não está mais pendente"}), 409
            updated = dict(updated_row)
            conn.commit()

            updated.pop('pickup_code', None)
            updated.pop('delivery_code', None)

            try:
                with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as _ncur:
                    cli_token = _get_fcm_token(_ncur, 'client_profiles', str(updated.get('client_id', '')))
                    prep_msg = f" Tempo estimado: {estimated_prep_time} min." if estimated_prep_time else ""
                    _notify(cli_token, "Pedido aceito! 🎉",
                            f"O restaurante confirmou seu pedido.{prep_msg}",
                            {"order_id": str(order_id), "status": "accepted"})
            except Exception as _e:
                logger.warning(f"FCM restaurant_accept_order: {_e}")

            if _award_points_for_action and updated.get('restaurant_id'):
                try:
                    _award_points_for_action(
                        user_id=str(updated['restaurant_id']),
                        action_key="order_accepted_restaurant",
                        order_id=str(order_id),
                        description="Pedido aceito",
                    )
                except Exception as _gam_err:
                    logger.warning(f"Gamificação: falha ao conceder pontos de aceite (restaurante) para {order_id}: {_gam_err}")

            return jsonify(updated), 200
    except Exception as e:
        logger.error(f"Erro em restaurant_accept_order: {e}", exc_info=True)
        if conn:
            conn.rollback()
        return jsonify({"error": "Erro interno do servidor"}), 500
    finally:
        if conn:
            conn.close()


@orders_bp.route('/<uuid:order_id>/cancel-by-client', methods=['POST'])
def cancel_order_by_client(order_id):
    """Cliente cancela o proprio pedido enquanto o restaurante ainda nao aceitou.

    So permitido em 'awaiting_payment' (pagamento nao concluido) ou 'pending'
    (aguardando o restaurante aceitar). Depois que o restaurante aceita, o
    cliente precisa falar com o suporte -- o restaurante ja pode ter comprado
    insumos / comecado o preparo. Se o pedido ja estava pago online, dispara
    estorno automatico via Mercado Pago (mesmo padrao do cancelamento pelo
    restaurante e do incidente de entrega)."""
    conn = None
    try:
        user_auth_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
        if error:
            return error
        if user_type != 'client':
            return jsonify({"error": "Apenas o cliente pode cancelar o proprio pedido"}), 403

        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Erro de conexão com banco de dados"}), 500

        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                "SELECT id, status, client_id, restaurant_id, status_pagamento, "
                "total_amount, id_transacao_mp, payment_provider FROM orders WHERE id = %s",
                (str(order_id),),
            )
            order = cur.fetchone()
            if not order:
                return jsonify({"error": "Pedido não encontrado"}), 404

            # Confere posse do pedido
            cur.execute("SELECT id FROM client_profiles WHERE user_id = %s", (user_auth_id,))
            prof = cur.fetchone()
            if not prof or str(prof['id']) != str(order['client_id']):
                return jsonify({"error": "Este pedido não pertence a você"}), 403

            if order['status'] not in ('awaiting_payment', 'pending'):
                return jsonify({
                    "error": "Este pedido não pode mais ser cancelado por aqui. "
                             "Fale com o suporte.",
                    "status_atual": STATUS_DISPLAY_MAP.get(order['status'], order['status']),
                }), 400

            # UPDATE atomico: so cancela se ainda estiver num estado cancelavel
            cur.execute("""
                UPDATE orders
                   SET status = 'cancelled',
                       cancellation_reason = 'cancelled_by_client',
                       updated_at = NOW()
                 WHERE id = %s AND status IN ('awaiting_payment', 'pending')
                RETURNING id
            """, (str(order_id),))
            if not cur.fetchone():
                conn.rollback()
                return jsonify({"error": "Pedido não pode mais ser cancelado"}), 409
            conn.commit()

            # Estorno automatico se ja estava pago online
            if order['status_pagamento'] == 'approved':
                refund_amount = float(order['total_amount'] or 0)
                if refund_amount > 0:
                    try:
                        from ..utils.gateway import refund_order_payment
                        if order['id_transacao_mp']:
                            ok_refund, refund_detail = refund_order_payment(dict(order), current_app.mp_sdk)
                            if ok_refund:
                                with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as _rcur:
                                    _rcur.execute(
                                        "UPDATE orders SET status_pagamento = 'refunded', updated_at = NOW() WHERE id = %s",
                                        (str(order_id),),
                                    )
                                conn.commit()
                                logger.info(f"Reembolso automático OK (cancelamento pelo cliente): pedido {order_id} R${refund_amount}")
                            else:
                                logger.warning(f"Gateway recusou reembolso do pedido {order_id} (cancelamento cliente): {refund_detail}")
                                sentry_sdk.capture_message(
                                    f"MP recusou reembolso automático do pedido {order_id} (cancelado pelo cliente) — requer ação manual do admin.",
                                    level="warning",
                                )
                        else:
                            logger.warning(f"Sem SDK MP/id_transacao_mp para reembolsar pedido {order_id} (cancelamento cliente)")
                            sentry_sdk.capture_message(
                                f"Pedido {order_id} cancelado pelo cliente estava pago mas sem id_transacao_mp/SDK disponível — requer ação manual do admin.",
                                level="warning",
                            )
                    except Exception as _re:
                        logger.warning(f"Reembolso automático falhou (cancelamento cliente, fica pendente p/ admin): {_re}")
                        sentry_sdk.capture_exception(_re)

            # FCM: avisa o restaurante que o cliente cancelou
            try:
                if order['restaurant_id']:
                    _nc = get_db_connection()
                    if _nc:
                        try:
                            with _nc.cursor(cursor_factory=psycopg2.extras.DictCursor) as _ncur:
                                rest_token = _get_fcm_token(_ncur, 'restaurant_profiles', str(order['restaurant_id']))
                                _notify(rest_token, "Pedido cancelado",
                                        "O cliente cancelou o pedido antes da confirmação.",
                                        {"order_id": str(order_id), "status": "cancelled"})
                        finally:
                            _nc.close()
            except Exception as _e:
                logger.warning(f"FCM cancel_order_by_client: {_e}")

            return jsonify({"status": "success", "message": "Pedido cancelado com sucesso.",
                            "order_status": "cancelled"}), 200

    except Exception as e:
        logger.error(f"Erro em cancel_order_by_client: {e}", exc_info=True)
        if conn:
            conn.rollback()
        return jsonify({"error": "Erro interno do servidor"}), 500
    finally:
        if conn:
            conn.close()


# === MANTIDO: expor o pickup_code com permissão adequada
@orders_bp.route('/<uuid:order_id>/pickup-code', methods=['GET'])
def get_pickup_code_for_delivery_or_restaurant(order_id):
    conn = None
    try:
        user_auth_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
        if error:
            return error

        conn = get_db_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:

            if user_type == 'client':
                cur.execute("""
                    SELECT o.pickup_code
                    FROM orders o
                    JOIN client_profiles cp ON o.client_id = cp.id
                    WHERE o.id = %s AND cp.user_id = %s
                """, (str(order_id), user_auth_id))
                row = cur.fetchone()
                if not row:
                    return jsonify({"error": "Pedido não encontrado"}), 404
                return jsonify({"pickup_code": row['pickup_code']}), 200

            if user_type == 'restaurant':
                cur.execute("""
                    SELECT o.pickup_code
                    FROM orders o
                    JOIN restaurant_profiles rp ON o.restaurant_id = rp.id
                    WHERE o.id = %s AND rp.user_id = %s
                """, (str(order_id), user_auth_id))
                row = cur.fetchone()
                if not row:
                    return jsonify({"error": "Pedido não encontrado ou não pertence a este restaurante"}), 404
                return jsonify({"pickup_code": row['pickup_code']}), 200

            if user_type == 'delivery':
                cur.execute("SELECT id FROM delivery_profiles WHERE user_id = %s", (user_auth_id,))
                dprof = cur.fetchone()
                if not dprof:
                    return jsonify({"error": "Perfil de entregador não encontrado"}), 404

                cur.execute("""
                    SELECT pickup_code
                    FROM orders
                    WHERE id = %s AND delivery_id = %s
                """, (str(order_id), dprof['id']))
                row = cur.fetchone()
                if not row:
                    return jsonify({"error": "Pedido não encontrado ou não atribuído a este entregador"}), 404
                return jsonify({"pickup_code": row['pickup_code']}), 200

            return jsonify({"error": "Acesso não autorizado"}), 403

    except Exception as e:
        logger.error(f"Erro em get_pickup_code_for_delivery_or_restaurant: {e}", exc_info=True)
        if conn:
            conn.rollback()
        return jsonify({"error": "Erro interno do servidor"}), 500
    finally:
        if conn:
            conn.close()

# === NOVO: rota de compatibilidade usada pelo app do cliente
# GET /api/orders/<order_id>/codes
@orders_bp.route('/<uuid:order_id>/codes', methods=['GET'])
def get_order_codes_compatible(order_id):
    conn = None
    try:
        user_auth_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
        if error:
            return error

        conn = get_db_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            if user_type == 'client':
                cur.execute("""
                    SELECT o.delivery_code, o.status
                    FROM orders o
                    JOIN client_profiles cp ON o.client_id = cp.id
                    WHERE o.id = %s AND cp.user_id = %s
                """, (str(order_id), user_auth_id))
                row = cur.fetchone()
                if not row:
                    return jsonify({"error": "Pedido não encontrado"}), 404
                return jsonify({
                    "order_id": str(order_id),
                    "status": row['status'],
                    "delivery_code": row['delivery_code']
                }), 200

            if user_type == 'restaurant':
                cur.execute("""
                    SELECT o.pickup_code, o.status
                    FROM orders o
                    JOIN restaurant_profiles rp ON o.restaurant_id = rp.id
                    WHERE o.id = %s AND rp.user_id = %s
                """, (str(order_id), user_auth_id))
                row = cur.fetchone()
                if not row:
                    return jsonify({"error": "Pedido não encontrado ou não pertence a este restaurante"}), 404
                return jsonify({
                    "order_id": str(order_id),
                    "status": row['status'],
                    "pickup_code": row['pickup_code']
                }), 200

            if user_type == 'delivery':
                cur.execute("SELECT id FROM delivery_profiles WHERE user_id = %s", (user_auth_id,))
                dprof = cur.fetchone()
                if not dprof:
                    return jsonify({"error": "Perfil de entregador não encontrado"}), 404

                cur.execute("""
                    SELECT pickup_code, status
                    FROM orders
                    WHERE id = %s AND delivery_id = %s
                """, (str(order_id), dprof['id']))
                row = cur.fetchone()
                if not row:
                    return jsonify({"error": "Pedido não encontrado ou não atribuído a este entregador"}), 404
                return jsonify({
                    "order_id": str(order_id),
                    "status": row['status'],
                    "pickup_code": row['pickup_code']
                }), 200

            return jsonify({"error": "Acesso não autorizado"}), 403

    except Exception as e:
        logger.error(f"Erro em get_order_codes_compatible: {e}", exc_info=True)
        if conn:
            conn.rollback()
        return jsonify({"error": "Erro interno do servidor"}), 500
    finally:
        if conn:
            conn.close()

# === NOVO: DELETE /api/orders/<order_id>  -> arquiva/exclui pedido
# Regra:
# - CLIENTE pode arquivar pedidos que são seus e estejam em
#   'awaiting_payment', 'cancelled', 'delivered', 'archived'
#   (pedidos em andamento não podem ser excluídos pelo cliente)
# - RESTAURANTE pode arquivar pedidos seus que estejam 'delivered' ou 'cancelled'
# - Entregador não exclui
@orders_bp.route('/<uuid:order_id>', methods=['DELETE'])
def archive_order(order_id):
    logger.info(f"=== INÍCIO archive_order para {order_id} ===")
    conn = None
    try:
        user_auth_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
        if error:
            return error

        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Erro de conexão com o banco de dados"}), 500

        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            if user_type == 'client':
                cur.execute("""
                    SELECT o.status
                    FROM orders o
                    JOIN client_profiles cp ON o.client_id = cp.id
                    WHERE o.id = %s AND cp.user_id = %s
                """, (str(order_id), user_auth_id))
                row = cur.fetchone()
                if not row:
                    return jsonify({"error": "Pedido não encontrado"}), 404

                allowed = {'awaiting_payment', 'cancelled', 'delivered', 'archived'}
                if row['status'] not in allowed:
                    return jsonify({"error": "Este pedido ainda está em andamento e não pode ser excluído."}), 400

                cur.execute("""
                    UPDATE orders
                    SET archived_at = NOW(), updated_at = NOW()
                    WHERE id = %s
                    RETURNING id, status, archived_at
                """, (str(order_id),))
                result = cur.fetchone()
                conn.commit()
                return jsonify({"status": "success", "order_id": str(result['id']), "new_status": result['status'], "archived": True}), 200

            elif user_type == 'restaurant':
                cur.execute("""
                    SELECT o.status
                    FROM orders o
                    JOIN restaurant_profiles rp ON o.restaurant_id = rp.id
                    WHERE o.id = %s AND rp.user_id = %s
                """, (str(order_id), user_auth_id))
                row = cur.fetchone()
                if not row:
                    return jsonify({"error": "Pedido não encontrado ou não pertence a este restaurante"}), 404

                if row['status'] not in {'delivered', 'cancelled', 'archived'}:
                    return jsonify({"error": "Somente pedidos finalizados podem ser arquivados pelo restaurante."}), 400

                cur.execute("""
                    UPDATE orders
                    SET archived_at = NOW(), updated_at = NOW()
                    WHERE id = %s
                    RETURNING id, status, archived_at
                """, (str(order_id),))
                result = cur.fetchone()
                conn.commit()
                return jsonify({"status": "success", "order_id": str(result['id']), "new_status": result['status'], "archived": True}), 200

            else:
                return jsonify({"error": "Acesso negado"}), 403

    except Exception as e:
        logger.error(f"Erro em archive_order: {e}", exc_info=True)
        if conn:
            conn.rollback()
        return jsonify({"error": "Erro interno do servidor"}), 500
    finally:
        if conn:
            conn.close()
