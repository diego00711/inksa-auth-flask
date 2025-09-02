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
from ..utils.helpers import get_db_connection, get_user_id_from_token

# Configuração do logging para melhor depuração
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s'
)
logger = logging.getLogger(__name__)

orders_bp = Blueprint('orders', __name__)

# --- Constantes e Mapeamentos de Status ---

DEFAULT_DELIVERY_FEE = 5.0

VALID_STATUSES = {
    'Pendente': 'pending',
    'Aceito': 'accepted', 
    'Preparando': 'preparing',
    'Pronto': 'ready',
    'Saiu para entrega': 'delivering',
    'Entregue': 'delivered',
    'Cancelado': 'cancelled'
}

STATUS_DISPLAY = {v: k for k, v in VALID_STATUSES.items()}

# --- Funções Auxiliares ---

def generate_verification_code(length=4):
    """Gera um código alfanumérico aleatório de 4 dígitos (ex: 'A4B8')."""
    chars = string.ascii_uppercase.replace('I', '').replace('O', '') + string.digits.replace('0', '').replace('1', '')
    return ''.join(random.choice(chars) for _ in range(length))

def is_valid_status_transition(current_status, new_status):
    """Valida as transições de status permitidas (sem considerar os códigos)."""
    valid_transitions = {
        'pending': ['accepted', 'cancelled'],
        'accepted': ['preparing', 'cancelled'],
        'preparing': ['ready', 'cancelled'],
        'ready': ['delivering', 'cancelled'], # A transição para 'delivering' será controlada pelo endpoint /pickup
        'delivering': ['delivered'],          # A transição para 'delivered' será controlada pelo endpoint /complete
        'delivered': [],
        'cancelled': []
    }
    return new_status in valid_transitions.get(current_status, [])

# --- Rotas da API ---

@orders_bp.route('/', methods=['GET', 'POST'])
def handle_orders():
    """Rota principal para listar (GET) ou criar (POST) pedidos."""
    logger.info("=== INÍCIO handle_orders ===")
    conn = None
    
    try:
        user_auth_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
        if error: 
            logger.warning(f"Erro de autenticação: {error}")
            return error

        conn = get_db_connection()
        if not conn: 
            logger.error("Falha na conexão com o banco de dados")
            return jsonify({"error": "Erro de conexão com o banco de dados"}), 500

        # --- GET: Listar pedidos ---
        if request.method == 'GET':
            logger.info("Processando GET - Listar pedidos")
            
            sort_by = request.args.get('sort_by', 'created_at')
            sort_order = request.args.get('sort_order', 'desc')
            status_filter = request.args.get('status')
            
            valid_sort_columns = {'created_at', 'total_amount', 'status'}
            if sort_by not in valid_sort_columns:
                return jsonify({"error": f"Campo de ordenação inválido. Use: {', '.join(valid_sort_columns)}"}), 400
            if sort_order.upper() not in {'ASC', 'DESC'}:
                return jsonify({"error": "Direção de ordenação inválida. Use 'asc' ou 'desc'"}), 400

            # Oculta códigos de verificação na listagem geral por segurança
            query = "SELECT o.id, o.client_id, o.restaurant_id, o.items, o.delivery_address, o.total_amount_items, o.delivery_fee, o.total_amount, o.status, o.created_at, o.updated_at, rp.restaurant_name, rp.logo_url as restaurant_logo, cp.first_name as client_first_name, cp.last_name as client_last_name FROM orders o LEFT JOIN restaurant_profiles rp ON o.restaurant_id = rp.id LEFT JOIN client_profiles cp ON o.client_id = cp.id WHERE 1=1"
            params = []

            if user_type == 'restaurant':
                with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                    cur.execute("SELECT id FROM restaurant_profiles WHERE user_id = %s", (user_auth_id,))
                    profile = cur.fetchone()
                    if not profile: return jsonify({"error": "Perfil do restaurante não encontrado"}), 404
                    query += " AND o.restaurant_id = %s"
                    params.append(profile['id'])
            elif user_type == 'client':
                query += " AND o.client_id = (SELECT id FROM client_profiles WHERE user_id = %s)"
                params.append(user_auth_id)
            
            if status_filter:
                query += " AND o.status = %s"
                params.append(status_filter)

            query += f" ORDER BY o.{sort_by} {sort_order}"

            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute(query, tuple(params))
                orders = []
                for row in cur.fetchall():
                    order_dict = dict(row)
                    order_dict['status_display'] = STATUS_DISPLAY.get(order_dict.get('status'), 'Desconhecido')
                    orders.append(order_dict)

            logger.info(f"Encontrados {len(orders)} pedidos")
            return jsonify({"status": "success", "data": orders}), 200

        # --- POST: Criar novo pedido ---
        elif request.method == 'POST':
            logger.info("Processando POST - Criar pedido")
            if user_type != 'client':
                return jsonify({"error": "Apenas clientes podem criar pedidos"}), 403
            
            data = request.get_json()
            required_fields = ['restaurant_id', 'items', 'delivery_address']
            if any(field not in data for field in required_fields):
                return jsonify({"error": "Campos obrigatórios ausentes"}), 400

            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute("SELECT id FROM client_profiles WHERE user_id = %s", (user_auth_id,))
                client_profile = cur.fetchone()
                if not client_profile: return jsonify({"error": "Perfil do cliente não encontrado"}), 404

                total_items = sum(item.get('price', 0) * item.get('quantity', 1) for item in data['items'])
                delivery_fee = DEFAULT_DELIVERY_FEE

                order_data = {
                    'id': str(uuid.uuid4()),
                    'client_id': client_profile['id'],
                    'restaurant_id': data['restaurant_id'],
                    'items': json.dumps(data['items']),
                    'delivery_address': json.dumps(data['delivery_address']),
                    'total_amount_items': total_items,
                    'delivery_fee': delivery_fee,
                    'total_amount': total_items + delivery_fee,
                    'status': 'pending',
                    'created_at': datetime.now(),
                    'updated_at': datetime.now(),
                    'pickup_code': generate_verification_code(),
                    'delivery_code': generate_verification_code()
                }
                
                columns = ', '.join(order_data.keys())
                placeholders = ', '.join(['%s'] * len(order_data))
                cur.execute(f"INSERT INTO orders ({columns}) VALUES ({placeholders}) RETURNING *", list(order_data.values()))
                new_order = cur.fetchone()
                conn.commit()

                order_dict = dict(new_order)
                # Ocultar códigos na resposta ao cliente por segurança
                order_dict.pop('pickup_code', None)
                order_dict.pop('delivery_code', None)
                order_dict['status_display'] = STATUS_DISPLAY.get(order_dict['status'], order_dict['status'])
                
                return jsonify({"status": "success", "message": "Pedido criado com sucesso", "data": order_dict}), 201

    except Exception as e:
        logger.error(f"Erro inesperado em handle_orders: {e}", exc_info=True)
        if conn: conn.rollback()
        return jsonify({"error": "Erro interno no servidor"}), 500
    finally:
        if conn:
            conn.close()
            logger.info("Conexão com banco fechada em handle_orders")


@orders_bp.route('/<uuid:order_id>/status', methods=['PUT'])
def update_order_status(order_id):
    """Atualiza o status de um pedido (exceto para 'delivering' e 'delivered', que usam rotas próprias)."""
    logger.info(f"=== INÍCIO UPDATE_ORDER_STATUS para {order_id} ===")
    conn = None
    
    try:
        user_auth_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
        if error: return error

        if user_type != 'restaurant':
            return jsonify({"error": "Apenas restaurantes podem alterar o status de um pedido"}), 403

        data = request.get_json()
        if not data or 'status' not in data:
            return jsonify({"error": "Campo 'status' é obrigatório"}), 400

        new_status_display = data['status']
        if new_status_display not in VALID_STATUSES:
            return jsonify({"error": f"Status inválido. Válidos: {list(VALID_STATUSES.keys())}"}), 400
        
        new_status_internal = VALID_STATUSES[new_status_display]
        
        if new_status_internal in ['delivering', 'delivered']:
            return jsonify({"error": f"Para mudar o status para '{new_status_display}', use o endpoint de verificação de código apropriado."}), 400

        conn = get_db_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT o.status FROM orders o JOIN restaurant_profiles rp ON o.restaurant_id = rp.id WHERE o.id = %s AND rp.user_id = %s", (str(order_id), user_auth_id))
            order = cur.fetchone()
            
            if not order:
                return jsonify({"error": "Pedido não encontrado ou não pertence a este restaurante"}), 404

            current_status = order['status']
            if not is_valid_status_transition(current_status, new_status_internal):
                return jsonify({"error": "Transição de status não permitida"}), 400

            cur.execute("UPDATE orders SET status = %s, updated_at = %s WHERE id = %s RETURNING *", (new_status_internal, datetime.now(), str(order_id)))
            updated_order = cur.fetchone()
            conn.commit()

            order_dict = dict(updated_order)
            order_dict.pop('pickup_code', None)
            order_dict.pop('delivery_code', None)
            order_dict['status_display'] = STATUS_DISPLAY.get(order_dict['status'], order_dict['status'])

            return jsonify({"status": "success", "message": f"Status atualizado para '{new_status_display}'", "data": order_dict}), 200

    except Exception as e:
        logger.error(f"Erro em update_order_status: {e}", exc_info=True)
        if conn: conn.rollback()
        return jsonify({"error": "Erro interno no servidor"}), 500
    finally:
        if conn: conn.close()


@orders_bp.route('/<uuid:order_id>/pickup', methods=['POST'])
def pickup_order(order_id):
    """Endpoint para o entregador usar o código de retirada e mover o status para 'delivering'."""
    logger.info(f"=== INÍCIO PICKUP_ORDER para {order_id} ===")
    conn = None
    try:
        user_auth_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
        if error: return error
        if user_type not in ['restaurant', 'driver']: # Futuramente, pode ser apenas 'driver'
            return jsonify({"error": "Acesso não autorizado para retirada"}), 403

        data = request.get_json()
        if not data or 'pickup_code' not in data:
            return jsonify({"error": "Código de retirada (pickup_code) é obrigatório"}), 400

        conn = get_db_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT status, pickup_code FROM orders WHERE id = %s", (str(order_id),))
            order = cur.fetchone()

            if not order: return jsonify({"error": "Pedido não encontrado"}), 404
            if order['status'] != 'ready': return jsonify({"error": f"Pedido não está pronto para retirada. Status atual: {STATUS_DISPLAY.get(order['status'])}"}), 400
            if order['pickup_code'] != data['pickup_code'].upper(): return jsonify({"error": "Código de retirada inválido"}), 403

            cur.execute("UPDATE orders SET status = 'delivering', updated_at = %s WHERE id = %s", (datetime.now(), str(order_id)))
            conn.commit()
            logger.info(f"Pedido {order_id} retirado com sucesso.")
            return jsonify({"status": "success", "message": "Pedido retirado e em rota de entrega."}), 200

    except Exception as e:
        logger.error(f"Erro em pickup_order: {e}", exc_info=True)
        if conn: conn.rollback()
        return jsonify({"error": "Erro interno do servidor"}), 500
    finally:
        if conn: conn.close()


@orders_bp.route('/<uuid:order_id>/complete', methods=['POST'])
def complete_order(order_id):
    """Endpoint para o entregador usar o código de entrega e mover o status para 'delivered'."""
    logger.info(f"=== INÍCIO COMPLETE_ORDER para {order_id} ===")
    conn = None
    try:
        user_auth_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
        if error: return error
        if user_type not in ['restaurant', 'driver']:
            return jsonify({"error": "Acesso não autorizado para completar a entrega"}), 403

        data = request.get_json()
        if not data or 'delivery_code' not in data:
            return jsonify({"error": "Código de entrega (delivery_code) é obrigatório"}), 400

        conn = get_db_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT status, delivery_code FROM orders WHERE id = %s", (str(order_id),))
            order = cur.fetchone()

            if not order: return jsonify({"error": "Pedido não encontrado"}), 404
            if order['status'] != 'delivering': return jsonify({"error": f"O pedido não está em rota de entrega. Status atual: {STATUS_DISPLAY.get(order['status'])}"}), 400
            if order['delivery_code'] != data['delivery_code'].upper(): return jsonify({"error": "Código de entrega inválido"}), 403

            cur.execute("UPDATE orders SET status = 'delivered', updated_at = %s, completed_at = %s WHERE id = %s", (datetime.now(), datetime.now(), str(order_id)))
            conn.commit()
            logger.info(f"Pedido {order_id} entregue com sucesso.")
            return jsonify({"status": "success", "message": "Pedido entregue com sucesso!"}), 200

    except Exception as e:
        logger.error(f"Erro em complete_order: {e}", exc_info=True)
        if conn: conn.rollback()
        return jsonify({"error": "Erro interno do servidor"}), 500
    finally:
        if conn: conn.close()


@orders_bp.route('/valid-statuses', methods=['GET'])
def get_valid_statuses():
    """Retorna os status válidos para o tipo de usuário."""
    logger.info("=== INÍCIO get_valid_statuses ===")
    try:
        user_auth_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
        if error: return error
        
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
    """Retorna o histórico de status de um pedido."""
    logger.info("=== INÍCIO get_order_status_history ===")
    conn = None
    try:
        user_auth_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
        if error: return error
        
        conn = get_db_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            if user_type == 'restaurant':
                cur.execute("SELECT o.* FROM orders o JOIN restaurant_profiles rp ON o.restaurant_id = rp.id WHERE o.id = %s AND rp.user_id = %s", (str(order_id), user_auth_id))
            elif user_type == 'client':
                cur.execute("SELECT o.* FROM orders o JOIN client_profiles cp ON o.client_id = cp.id WHERE o.id = %s AND cp.user_id = %s", (str(order_id), user_auth_id))
            else:
                return jsonify({"error": "Acesso não autorizado"}), 403
                
            order = cur.fetchone()
            if not order:
                return jsonify({"error": "Pedido não encontrado ou acesso negado"}), 404
            
            # NOTA: Implementação simplificada. Idealmente, haveria uma tabela `order_status_history`.
            history = [{
                "status": STATUS_DISPLAY.get(order['status'], order['status']),
                "timestamp": order['updated_at'].isoformat(),
                "changed_by": "system"
            }]
            
            return jsonify({"status": "success", "order_id": str(order_id), "history": history}), 200
            
    except Exception as e:
        logger.error(f"Erro ao obter histórico do pedido: {e}", exc_info=True)
        return jsonify({"error": "Erro interno do servidor"}), 500
    finally:
        if conn: conn.close()


# =====================================================================
# ✅✅✅ INÍCIO DA NOVA ROTA ADICIONADA ✅✅✅
# =====================================================================

@orders_bp.route('/pending-client-review', methods=['GET'])
def get_pending_client_reviews():
    """
    Retorna os pedidos de um cliente que foram entregues e estão
    pendentes de avaliação (do restaurante ou do entregador).
    """
    logger.info("=== INÍCIO get_pending_client_reviews ===")
    conn = None
    try:
        # 1. Autentica o usuário e garante que é um cliente
        user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
        if error:
            return error
        if user_type != 'client':
            return jsonify({'error': 'Acesso negado. Apenas para clientes.'}), 403

        conn = get_db_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # 2. Busca o ID do perfil do cliente a partir do user_id do token
            cur.execute("SELECT id FROM client_profiles WHERE user_id = %s", (user_id,))
            client_profile = cur.fetchone()
            if not client_profile:
                return jsonify({'error': 'Perfil de cliente não encontrado.'}), 404
            
            client_id = client_profile['id']

            # 3. Query SQL para encontrar pedidos entregues que ainda não foram totalmente avaliados
            # A lógica é:
            # - O pedido pertence ao cliente e foi entregue.
            # - E (pelo menos uma das avaliações está pendente):
            #   - Não existe avaliação do restaurante para este pedido.
            #   - OU (o pedido teve um entregador E não existe avaliação do entregador para este pedido).
            sql_query = """
                SELECT 
                    o.id, 
                    o.restaurant_id,
                    rp.trading_name as restaurant_name,
                    o.delivery_id as deliveryman_id,
                    dp.full_name as deliveryman_name,
                    o.completed_at
                FROM 
                    orders o
                JOIN 
                    restaurant_profiles rp ON o.restaurant_id = rp.id
                LEFT JOIN 
                    delivery_profiles dp ON o.delivery_id = dp.id
                WHERE 
                    o.client_id = %s
                    AND o.status = 'delivered'
                    AND (
                        NOT EXISTS (
                            SELECT 1 FROM restaurant_reviews rr 
                            WHERE rr.order_id = o.id AND rr.client_id = %s
                        )
                        OR 
                        (o.delivery_id IS NOT NULL AND NOT EXISTS (
                            SELECT 1 FROM delivery_reviews dr 
                            WHERE dr.order_id = o.id AND dr.client_id = %s
                        ))
                    )
                ORDER BY o.completed_at DESC;
            """
            
            # Passamos o client_id três vezes, uma para cada placeholder %s na query
            cur.execute(sql_query, (client_id, client_id, client_id))
            
            orders_to_review = [dict(row) for row in cur.fetchall()]
            
            logger.info(f"Encontrados {len(orders_to_review)} pedidos pendentes de avaliação para o cliente {client_id}")
            return jsonify(orders_to_review), 200

    except Exception as e:
        logger.error(f"Erro em get_pending_client_reviews: {e}", exc_info=True)
        return jsonify({'error': 'Erro interno do servidor ao buscar pedidos para avaliação.'}), 500
    finally:
        if conn:
            conn.close()
            logger.info("Conexão com banco fechada em get_pending_client_reviews")

# =====================================================================
# ✅✅✅ FIM DA NOVA ROTA ADICIONADA ✅✅✅
# =====================================================================
