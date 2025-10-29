# src/routes/orders.py
import uuid
import json
import random
import string
from datetime import datetime
from flask import Blueprint, request, jsonify
import psycopg2
import psycopg2.extras
import logging
from ..utils.helpers import get_db_connection, get_user_id_from_token

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
    'cancelled': 'Cancelado',
    'archived': 'Arquivado'
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
            sort_by = request.args.get('sort_by', 'created_at')
            sort_order = request.args.get('sort_order', 'desc')
            status_filter = request.args.get('status')

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

            elif user_type == 'client':
                query += " AND o.client_id = (SELECT id FROM client_profiles WHERE user_id = %s)"
                params.append(user_auth_id)

            if status_filter:
                query += " AND o.status = %s"
                params.append(status_filter)

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
                SELECT o.status
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

            cur.execute(
                "UPDATE orders SET status = %s, updated_at = NOW() WHERE id = %s RETURNING *",
                (new_status_internal, str(order_id))
            )
            updated_order = dict(cur.fetchone())
            conn.commit()

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
            cur.execute("SELECT status, pickup_code FROM orders WHERE id = %s", (str(order_id),))
            order = cur.fetchone()
            if not order:
                return jsonify({"error": "Pedido não encontrado"}), 404

            if order['status'] not in ['ready', 'accepted_by_delivery']:
                return jsonify({
                    "error": f"Pedido não está pronto para retirada. Status atual: {STATUS_DISPLAY_MAP.get(order['status'])}"
                }), 400

            if order['pickup_code'] != code:
                return jsonify({"error": "Código de retirada inválido"}), 403

            cur.execute("UPDATE orders SET status = 'delivering', updated_at = NOW() WHERE id = %s", (str(order_id),))
            conn.commit()
            logger.info(f"✅ Pedido {order_id} confirmado como retirado. Status: delivering")
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
            cur.execute("SELECT status, delivery_code FROM orders WHERE id = %s", (str(order_id),))
            order = cur.fetchone()
            if not order:
                return jsonify({"error": "Pedido não encontrado"}), 404

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
            conn.commit()
            logger.info(f"✅ Pedido {order_id} marcado como entregue!")
            return jsonify({"status": "success", "message": "Pedido entregue com sucesso!"}), 200

    except Exception as e:
        logger.error(f"Erro em complete_order: {e}", exc_info=True)
        if conn:
            conn.rollback()
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

            sql_query = """
                SELECT o.id, o.restaurant_id, rp.restaurant_name, o.client_id,
                       (cp.first_name || ' ' || cp.last_name) as client_name,
                       o.updated_at as delivered_at, o.total_amount
                FROM orders o
                JOIN restaurant_profiles rp ON o.restaurant_id = rp.id
                JOIN client_profiles cp ON o.client_id = cp.id
                WHERE o.delivery_id = %s AND o.status = 'delivered'
                  AND NOT EXISTS (
                    SELECT 1 FROM delivery_reviews dr
                    WHERE dr.order_id = o.id AND dr.delivery_id = %s
                  )
                ORDER BY o.updated_at DESC;
            """
            cur.execute(sql_query, (delivery_id, delivery_id))
            orders_to_review = [dict(row) for row in cur.fetchall()]
            return jsonify(orders_to_review), 200

    except Exception as e:
        logger.error(f"Erro em get_pending_delivery_review: {e}", exc_info=True)
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
            sql_query = """
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
                    COALESCE(o.delivery_fee, 0) AS delivery_fee,
                    o.status,
                    o.created_at
                FROM orders o
                LEFT JOIN restaurant_profiles rp ON o.restaurant_id = rp.id
                WHERE 
                    (o.status = 'ready' OR o.status = 'accepted_by_delivery')
                    AND o.delivery_id IS NULL
                ORDER BY o.created_at ASC;
            """
            cur.execute(sql_query)
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
                if order_dict.get('delivery_fee') is not None:
                    order_dict['delivery_fee'] = float(order_dict['delivery_fee'])

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

            cur.execute("""
                UPDATE orders
                SET delivery_id = %s,
                    status = 'accepted_by_delivery',
                    updated_at = NOW()
                WHERE id = %s
                RETURNING *
            """, (delivery_profile_id, str(order_id)))

            updated_order = dict(cur.fetchone())
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

# === NOVO: expor o código de retirada com permissão adequada
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
