# src/routes/orders.py (VERSÃO FINAL E CORRIGIDA)

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

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s')
logger = logging.getLogger(__name__)

orders_bp = Blueprint('orders', __name__)

DEFAULT_DELIVERY_FEE = 5.0

VALID_STATUSES_INTERNAL = {'pending', 'accepted', 'preparing', 'ready', 'delivering', 'delivered', 'cancelled'}
STATUS_DISPLAY_MAP = {
    'pending': 'Pendente', 'accepted': 'Aceito', 'preparing': 'Preparando',
    'ready': 'Pronto', 'delivering': 'Saiu para Entrega', 'delivered': 'Entregue',
    'cancelled': 'Cancelado'
}

def generate_verification_code(length=4):
    chars = string.ascii_uppercase.replace('I', '').replace('O', '') + string.digits.replace('0', '').replace('1', '')
    return ''.join(random.choice(chars) for _ in range(length))

def is_valid_status_transition(current_status, new_status):
    valid_transitions = {
        'pending': ['accepted', 'cancelled'], 'accepted': ['preparing', 'cancelled'],
        'preparing': ['ready', 'cancelled'], 'ready': ['delivering', 'cancelled'],
        'delivering': ['delivered'], 'delivered': [], 'cancelled': []
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
        if error: return error
        conn = get_db_connection()
        if not conn: return jsonify({"error": "Erro de conexão com o banco de dados"}), 500

        if request.method == 'GET':
            sort_by = request.args.get('sort_by', 'created_at')
            sort_order = request.args.get('sort_order', 'desc')
            status_filter = request.args.get('status')
            query = "SELECT o.*, rp.restaurant_name, rp.logo_url as restaurant_logo, cp.first_name as client_first_name, cp.last_name as client_last_name FROM orders o LEFT JOIN restaurant_profiles rp ON o.restaurant_id = rp.id LEFT JOIN client_profiles cp ON o.client_id = cp.id WHERE 1=1"
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
                orders = [dict(row) for row in cur.fetchall()]
            return jsonify(orders), 200

        elif request.method == 'POST':
            if user_type != 'client': return jsonify({"error": "Apenas clientes podem criar pedidos"}), 403
            data = request.get_json()
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute("SELECT id FROM client_profiles WHERE user_id = %s", (user_auth_id,))
                client_profile = cur.fetchone()
                if not client_profile: return jsonify({"error": "Perfil do cliente não encontrado"}), 404
                total_items = sum(item.get('price', 0) * item.get('quantity', 1) for item in data['items'])
                delivery_fee = data.get('delivery_fee', DEFAULT_DELIVERY_FEE)
                
                # ✅ CORREÇÃO: Garantir que o status inicial seja salvo em INGLÊS.
                order_data = {
                    'id': str(uuid.uuid4()), 'client_id': client_profile['id'], 'restaurant_id': data['restaurant_id'],
                    'items': json.dumps(data['items']), 'delivery_address': json.dumps(data['delivery_address']),
                    'total_amount_items': total_items, 'delivery_fee': delivery_fee, 'total_amount': total_items + delivery_fee,
                    'status': 'pending', # <-- AQUI ESTÁ A MUDANÇA CRUCIAL
                    'pickup_code': generate_verification_code(), 'delivery_code': generate_verification_code()
                }
                
                insert_query = "INSERT INTO orders (id, client_id, restaurant_id, items, delivery_address, total_amount_items, delivery_fee, total_amount, status, pickup_code, delivery_code, delivery_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL) RETURNING *"
                cur.execute(insert_query, list(order_data.values()))
                new_order = dict(cur.fetchone())
                conn.commit()
                new_order.pop('pickup_code', None)
                new_order.pop('delivery_code', None)
                return jsonify(new_order), 201
    except Exception as e:
        logger.error(f"Erro em handle_orders: {e}", exc_info=True)
        if conn: conn.rollback()
        return jsonify({"error": "Erro interno no servidor"}), 500
    finally:
        if conn: conn.close()

@orders_bp.route('/<uuid:order_id>/status', methods=['PUT'])
def update_order_status(order_id):
    conn = None
    try:
        user_auth_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
        if error: return error
        if user_type != 'restaurant': return jsonify({"error": "Apenas restaurantes podem alterar o status"}), 403
        data = request.get_json()
        if not data or 'new_status' not in data: return jsonify({"error": "Campo 'new_status' é obrigatório"}), 400
        new_status_internal = data['new_status']
        if new_status_internal not in VALID_STATUSES_INTERNAL: return jsonify({"error": f"Status inválido: '{new_status_internal}'"}), 400
        if new_status_internal in ['delivering', 'delivered']: return jsonify({"error": "Use o endpoint de código para esta transição."}), 400
        conn = get_db_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT o.status FROM orders o JOIN restaurant_profiles rp ON o.restaurant_id = rp.id WHERE o.id = %s AND rp.user_id = %s", (str(order_id), user_auth_id))
            order = cur.fetchone()
            if not order: return jsonify({"error": "Pedido não encontrado ou não pertence a este restaurante"}), 404
            
            # ✅ CORREÇÃO: Usa o status do banco (que agora estará em inglês) diretamente.
            current_status = order['status'].strip()
            
            if not is_valid_status_transition(current_status, new_status_internal):
                error_message = f"Transição de status de '{current_status}' para '{new_status_internal}' não permitida"
                return jsonify({"error": error_message}), 400
            
            cur.execute("UPDATE orders SET status = %s, updated_at = NOW() WHERE id = %s RETURNING *", (new_status_internal, str(order_id)))
            updated_order = dict(cur.fetchone())
            conn.commit()
            updated_order.pop('pickup_code', None)
            updated_order.pop('delivery_code', None)
            return jsonify(updated_order), 200
    except Exception as e:
        logger.error(f"Erro em update_order_status: {e}", exc_info=True)
        if conn: conn.rollback()
        return jsonify({"error": "Erro interno no servidor"}), 500
    finally:
        if conn: conn.close()

@orders_bp.route('/<uuid:order_id>/pickup', methods=['POST'])
def pickup_order(order_id):
    logger.info(f"=== INÍCIO PICKUP_ORDER para {order_id} ===")
    conn = None
    try:
        user_auth_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
        if error: return error
        if user_type not in ['restaurant', 'delivery']: return jsonify({"error": "Acesso não autorizado para retirada"}), 403
        data = request.get_json()
        if not data or 'pickup_code' not in data: return jsonify({"error": "Código de retirada (pickup_code) é obrigatório"}), 400
        conn = get_db_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT status, pickup_code FROM orders WHERE id = %s", (str(order_id),))
            order = cur.fetchone()
            if not order: return jsonify({"error": "Pedido não encontrado"}), 404
            if order['status'] != 'ready': return jsonify({"error": f"Pedido não está pronto para retirada. Status atual: {STATUS_DISPLAY_MAP.get(order['status'])}"}), 400
            if order['pickup_code'] != data['pickup_code'].upper(): return jsonify({"error": "Código de retirada inválido"}), 403
            cur.execute("UPDATE orders SET status = 'delivering', updated_at = NOW() WHERE id = %s", (str(order_id),))
            conn.commit()
            return jsonify({"status": "success", "message": "Pedido retirado e em rota de entrega."}), 200
    except Exception as e:
        logger.error(f"Erro em pickup_order: {e}", exc_info=True)
        if conn: conn.rollback()
        return jsonify({"error": "Erro interno do servidor"}), 500
    finally:
        if conn: conn.close()

@orders_bp.route('/<uuid:order_id>/complete', methods=['POST'])
def complete_order(order_id):
    logger.info(f"=== INÍCIO COMPLETE_ORDER para {order_id} ===")
    conn = None
    try:
        user_auth_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
        if error: return error
        if user_type not in ['restaurant', 'delivery']: return jsonify({"error": "Acesso não autorizado para completar a entrega"}), 403
        data = request.get_json()
        if not data or 'delivery_code' not in data: return jsonify({"error": "Código de entrega (delivery_code) é obrigatório"}), 400
        conn = get_db_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT status, delivery_code FROM orders WHERE id = %s", (str(order_id),))
            order = cur.fetchone()
            if not order: return jsonify({"error": "Pedido não encontrado"}), 404
            if order['status'] != 'delivering': return jsonify({"error": f"O pedido não está em rota de entrega. Status atual: {STATUS_DISPLAY_MAP.get(order['status'])}"}), 400
            if order['delivery_code'] != data['delivery_code'].upper(): return jsonify({"error": "Código de entrega inválido"}), 403
            cur.execute("UPDATE orders SET status = 'delivered', updated_at = NOW(), completed_at = NOW() WHERE id = %s", (str(order_id),))
            conn.commit()
            return jsonify({"status": "success", "message": "Pedido entregue com sucesso!"}), 200
    except Exception as e:
        logger.error(f"Erro em complete_order: {e}", exc_info=True)
        if conn: conn.rollback()
        return jsonify({"error": "Erro interno do servidor"}), 500
    finally:
        if conn: conn.close()

@orders_bp.route('/valid-statuses', methods=['GET'])
def get_valid_statuses():
    logger.info("=== INÍCIO get_valid_statuses ===")
    try:
        user_auth_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
        if error: return error
        if user_type == 'restaurant': available_statuses = ['Aceito', 'Preparando', 'Pronto', 'Cancelado']
        elif user_type == 'client': available_statuses = ['Cancelado']
        else: available_statuses = []
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
        if error: return error
        conn = get_db_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            if user_type == 'restaurant': cur.execute("SELECT o.* FROM orders o JOIN restaurant_profiles rp ON o.restaurant_id = rp.id WHERE o.id = %s AND rp.user_id = %s", (str(order_id), user_auth_id))
            elif user_type == 'client': cur.execute("SELECT o.* FROM orders o JOIN client_profiles cp ON o.client_id = cp.id WHERE o.id = %s AND cp.user_id = %s", (str(order_id), user_auth_id))
            else: return jsonify({"error": "Acesso não autorizado"}), 403
            order = cur.fetchone()
            if not order: return jsonify({"error": "Pedido não encontrado ou acesso negado"}), 404
            history = [{"status": STATUS_DISPLAY_MAP.get(order['status'], order['status']), "timestamp": order['updated_at'].isoformat(), "changed_by": "system"}]
            return jsonify({"status": "success", "order_id": str(order_id), "history": history}), 200
    except Exception as e:
        logger.error(f"Erro ao obter histórico do pedido: {e}", exc_info=True)
        return jsonify({"error": "Erro interno do servidor"}), 500
    finally:
        if conn: conn.close()

@orders_bp.route('/pending-client-review', methods=['GET'])
def get_pending_client_reviews():
    logger.info("=== INÍCIO get_pending_client_reviews ===")
    conn = None
    try:
        user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
        if error: return error
        if user_type != 'client': return jsonify({'error': 'Acesso negado. Apenas para clientes.'}), 403
        conn = get_db_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT id FROM client_profiles WHERE user_id = %s", (user_id,))
            client_profile = cur.fetchone()
            if not client_profile: return jsonify({'error': 'Perfil de cliente não encontrado.'}), 404
            client_id = client_profile['id']
            sql_query = "SELECT o.id, o.restaurant_id, rp.restaurant_name, o.delivery_id as deliveryman_id, (dp.first_name || ' ' || dp.last_name) as deliveryman_name, o.updated_at as completed_at FROM orders o JOIN restaurant_profiles rp ON o.restaurant_id = rp.id LEFT JOIN delivery_profiles dp ON o.delivery_id = dp.id WHERE o.client_id = %s AND o.status = 'delivered' AND (NOT EXISTS (SELECT 1 FROM restaurant_reviews rr WHERE rr.order_id = o.id AND rr.client_id = %s) OR (o.delivery_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM delivery_reviews dr WHERE dr.order_id = o.id AND dr.client_id = %s))) ORDER BY o.updated_at DESC;"
            cur.execute(sql_query, (client_id, client_id, client_id))
            orders_to_review = [dict(row) for row in cur.fetchall()]
            return jsonify(orders_to_review), 200
    except Exception as e:
        logger.error(f"Erro em get_pending_client_reviews: {e}", exc_info=True)
        return jsonify({'error': 'Erro interno do servidor.'}), 500
    finally:
        if conn: conn.close()

@orders_bp.route('/pending-delivery-review', methods=['GET', 'OPTIONS'])
def get_pending_delivery_review():
    logger.info("=== INÍCIO get_pending_delivery_review ===")
    conn = None
    try:
        user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
        if error: return error
        if user_type != 'delivery': return jsonify({'error': 'Acesso negado. Apenas para entregadores.'}), 403
        conn = get_db_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT id FROM delivery_profiles WHERE user_id = %s", (user_id,))
            delivery_profile = cur.fetchone()
            if not delivery_profile: return jsonify({'error': 'Perfil de entregador não encontrado.'}), 404
            delivery_id = delivery_profile['id']
            sql_query = "SELECT o.id, o.restaurant_id, rp.restaurant_name, o.client_id, (cp.first_name || ' ' || cp.last_name) as client_name, o.updated_at as delivered_at, o.total_amount FROM orders o JOIN restaurant_profiles rp ON o.restaurant_id = rp.id JOIN client_profiles cp ON o.client_id = cp.id WHERE o.delivery_id = %s AND o.status = 'delivered' AND NOT EXISTS (SELECT 1 FROM delivery_reviews dr WHERE dr.order_id = o.id AND dr.delivery_id = %s) ORDER BY o.updated_at DESC;"
            cur.execute(sql_query, (delivery_id, delivery_id))
            orders_to_review = [dict(row) for row in cur.fetchall()]
            return jsonify(orders_to_review), 200
    except Exception as e:
        logger.error(f"Erro em get_pending_delivery_review: {e}", exc_info=True)
        return jsonify({'error': 'Erro interno do servidor.'}), 500
    finally:
        if conn: conn.close()
            
@orders_bp.route('/available', methods=['GET'])
def get_available_orders():
    """
    Retorna todos os pedidos com status 'ready' que ainda não foram
    aceitos por nenhum entregador.
    """
    logger.info("=== INÍCIO get_available_orders ===")
    conn = None
    try:
        # 1. Autenticação: Garante que apenas entregadores acessem
        user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
        if error:
            logger.error(f"Erro de autenticação: {error}")
            return error
        
        if user_type != 'delivery':
            logger.warning(f"Acesso negado para user_type: {user_type}")
            return jsonify({'error': 'Acesso negado. Apenas para entregadores.'}), 403

        logger.info(f"Entregador autenticado: user_id={user_id}")
        
        conn = get_db_connection()
        if not conn:
            logger.error("Falha ao conectar ao banco de dados")
            return jsonify({'error': 'Erro de conexão com banco de dados'}), 500
            
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            
            # 2. Lógica da Query: Busca pedidos prontos e sem entregador
            # ✅ CORREÇÃO: Concatenar campos de endereço do restaurante
            sql_query = """
                SELECT 
                    o.id, 
                    o.restaurant_id,
                    COALESCE(rp.restaurant_name, 'Restaurante') as restaurant_name,
                    CONCAT_WS(', ', 
                        rp.address_street, 
                        rp.address_number, 
                        rp.address_neighborhood,
                        rp.address_city, 
                        rp.address_state
                    ) as restaurant_address,
                    o.delivery_address,
                    COALESCE(o.total_amount, 0) as total_amount,
                    COALESCE(o.delivery_fee, 0) as delivery_fee,
                    o.created_at
                FROM 
                    orders o
                LEFT JOIN 
                    restaurant_profiles rp ON o.restaurant_id = rp.id
                WHERE 
                    o.status = 'ready' 
                    AND o.delivery_id IS NULL
                ORDER BY 
                    o.created_at ASC;
            """
            
            logger.info("Executando query para buscar pedidos disponíveis...")
            cur.execute(sql_query)
            rows = cur.fetchall()
            
            logger.info(f"Query executada. Total de linhas: {len(rows)}")
            
            # ✅ CORREÇÃO: Serialização manual com tratamento de JSON
            available_orders = []
            for row in rows:
                try:
                    # Converte o row para dict
                    order_dict = dict(row)
                    
                    # ✅ Trata o campo delivery_address se for string JSON
                    if isinstance(order_dict.get('delivery_address'), str):
                        try:
                            order_dict['delivery_address'] = json.loads(order_dict['delivery_address'])
                        except (json.JSONDecodeError, TypeError):
                            # Se não for JSON válido, mantém como string
                            pass
                    
                    # ✅ Trata o campo restaurant_address se for string JSON
                    if isinstance(order_dict.get('restaurant_address'), str):
                        try:
                            order_dict['restaurant_address'] = json.loads(order_dict['restaurant_address'])
                        except (json.JSONDecodeError, TypeError):
                            pass
                    
                    # ✅ Converte created_at para string ISO
                    if order_dict.get('created_at'):
                        order_dict['created_at'] = order_dict['created_at'].isoformat()
                    
                    # ✅ Converte UUIDs para string
                    if order_dict.get('id'):
                        order_dict['id'] = str(order_dict['id'])
                    if order_dict.get('restaurant_id'):
                        order_dict['restaurant_id'] = str(order_dict['restaurant_id'])
                    
                    available_orders.append(order_dict)
                    
                except Exception as row_error:
                    logger.error(f"Erro ao processar linha: {row_error}", exc_info=True)
                    continue
            
            logger.info(f"✅ Processados {len(available_orders)} pedidos disponíveis com sucesso")
            
            # 3. Retorno: Retorna a lista de pedidos disponíveis
            return jsonify(available_orders), 200

    except Exception as e:
        logger.error(f"❌ Erro crítico em get_available_orders: {e}", exc_info=True)
        return jsonify({'error': 'Erro interno do servidor ao buscar entregas disponíveis.'}), 500
    finally:
        if conn:
            conn.close()
            logger.info("Conexão com banco fechada em get_available_orders")
