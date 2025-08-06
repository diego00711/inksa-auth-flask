# src/routes/orders.py
import uuid
import json
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

# Mapeamento de status (Frontend em Português -> Backend em Inglês)
# Centraliza a "tradução" para manter o código consistente.
VALID_STATUSES = {
    'Pendente': 'pending',
    'Aceito': 'accepted', 
    'Preparando': 'preparing',
    'Pronto': 'ready',
    'Saiu para entrega': 'delivering',
    'Entregue': 'delivered',
    'Cancelado': 'cancelled'
}

# Mapeamento reverso para exibição (Backend em Inglês -> Frontend em Português)
# Usado para enviar dados de volta ao frontend no formato que ele espera.
STATUS_DISPLAY = {v: k for k, v in VALID_STATUSES.items()}


def is_valid_status_transition(current_status, new_status):
    """
    ✅ FUNÇÃO CENTRAL PARA A CORREÇÃO: Valida as transições de status permitidas.
    Define o fluxo de trabalho dos pedidos e resolve o erro original.
    """
    valid_transitions = {
        'pending': ['accepted', 'cancelled'],      # De Pendente, pode Aceitar ou Cancelar.
        'accepted': ['preparing', 'cancelled'],    # De Aceito, pode Preparar ou Cancelar.
        'preparing': ['ready', 'cancelled'],       # De Preparando, pode marcar como Pronto ou Cancelar.
        'ready': ['delivering', 'cancelled'],      # De Pronto, pode sair para Entrega ou Cancelar.
        'delivering': ['delivered'],               # De Em Entrega, só pode marcar como Entregue.
        'delivered': [],                           # Estado final, não pode ser alterado.
        'cancelled': []                            # Estado final, não pode ser alterado.
    }
    
    # Retorna True se a transição `new_status` estiver na lista de transições
    # permitidas para o `current_status`.
    return new_status in valid_transitions.get(current_status, [])


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
                cur.execute(query, params)
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
                    'delivery_address': data['delivery_address'],
                    'total_amount_items': total_items,
                    'delivery_fee': delivery_fee,
                    'total_amount': total_items + delivery_fee,
                    'status': 'pending',
                    'created_at': datetime.now(),
                    'updated_at': datetime.now()
                }
                
                columns = ', '.join(order_data.keys())
                placeholders = ', '.join(['%s'] * len(order_data))
                cur.execute(f"INSERT INTO orders ({columns}) VALUES ({placeholders}) RETURNING *", list(order_data.values()))
                new_order = cur.fetchone()
                conn.commit()

                order_dict = dict(new_order)
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
    """Atualiza o status de um pedido específico."""
    logger.info("=== INÍCIO UPDATE_ORDER_STATUS ===")
    logger.info(f"Order ID recebido: {order_id}")
    conn = None
    
    try:
        user_auth_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
        if error:
            return error

        if user_type != 'restaurant':
            logger.warning(f"Acesso negado para tipo de usuário: {user_type}")
            return jsonify({"error": "Apenas restaurantes podem alterar o status de um pedido"}), 403

        data = request.get_json()
        if not data or 'status' not in data:
            logger.error(f"Payload JSON inválido ou campo 'status' ausente. Recebido: {data}")
            return jsonify({"error": "Campo 'status' é obrigatório no corpo da requisição"}), 400

        new_status_display = data['status']
        if new_status_display not in VALID_STATUSES:
            logger.error(f"Status inválido recebido: '{new_status_display}'")
            return jsonify({"error": f"Status inválido. Válidos: {list(VALID_STATUSES.keys())}"}), 400
        
        new_status_internal = VALID_STATUSES[new_status_display]
        logger.info(f"Status solicitado: '{new_status_display}' -> Convertido para: '{new_status_internal}'")

        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Erro de conexão com o banco de dados"}), 500

        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                SELECT o.id, o.status, o.restaurant_id 
                FROM orders o
                JOIN restaurant_profiles rp ON o.restaurant_id = rp.id
                WHERE o.id = %s AND rp.user_id = %s
            """, (str(order_id), user_auth_id))
            order = cur.fetchone()
            
            if not order:
                logger.warning(f"Pedido {order_id} não encontrado ou não pertence ao restaurante do usuário {user_auth_id}")
                return jsonify({"error": "Pedido não encontrado ou não pertence a este restaurante"}), 404

            current_status = order['status']
            logger.info(f"Status atual do pedido: '{current_status}'")

            if not is_valid_status_transition(current_status, new_status_internal):
                logger.warning(f"Transição de status inválida: de '{current_status}' para '{new_status_internal}'")
                return jsonify({
                    "error": "Transição de status não permitida",
                    "from": STATUS_DISPLAY.get(current_status, current_status),
                    "to": new_status_display
                }), 400

            logger.info(f"Atualizando status de '{current_status}' para '{new_status_internal}'")
            cur.execute(
                "UPDATE orders SET status = %s, updated_at = %s WHERE id = %s RETURNING *",
                (new_status_internal, datetime.now(), str(order_id))
            )
            updated_order = cur.fetchone()
            conn.commit()

            order_dict = dict(updated_order)
            order_dict['status_display'] = STATUS_DISPLAY.get(order_dict['status'], order_dict['status'])

            logger.info(f"Status do pedido {order_id} atualizado com sucesso.")
            return jsonify({
                "status": "success",
                "message": f"Status atualizado para '{new_status_display}'",
                "data": order_dict
            }), 200

    except Exception as e:
        logger.error(f"Erro inesperado em update_order_status: {e}", exc_info=True)
        if conn: conn.rollback()
        return jsonify({"error": "Erro interno no servidor"}), 500
    finally:
        if conn:
            conn.close()
            logger.info("Conexão com banco fechada em update_order_status")
        logger.info("=== FIM UPDATE_ORDER_STATUS ===")


@orders_bp.route('/valid-statuses', methods=['GET'])
def get_valid_statuses():
    """✅ NOVO ENDPOINT: Retorna os status válidos para o tipo de usuário."""
    logger.info("=== INÍCIO get_valid_statuses ===")
    try:
        user_auth_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
        if error:
            return error
        
        # Define quais status cada tipo de usuário pode ver/usar
        if user_type == 'restaurant':
            available_statuses = list(VALID_STATUSES.keys())
        elif user_type == 'client':
            available_statuses = ['Cancelado'] # Exemplo: cliente só pode cancelar
        else:
            available_statuses = []
        
        return jsonify({
            "status": "success",
            "valid_statuses": available_statuses
        }), 200
        
    except Exception as e:
        logger.error(f"Erro ao obter status válidos: {e}", exc_info=True)
        return jsonify({"error": "Erro interno do servidor"}), 500


@orders_bp.route('/<uuid:order_id>/status-history', methods=['GET'])
def get_order_status_history(order_id):
    """✅ NOVO ENDPOINT: Retorna o histórico de status de um pedido."""
    logger.info("=== INÍCIO get_order_status_history ===")
    conn = None
    try:
        user_auth_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
        if error:
            return error
        
        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Erro de conexão com o banco de dados"}), 500
            
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # Verifica se o usuário (restaurante ou cliente) tem permissão para ver o pedido
            if user_type == 'restaurant':
                cur.execute("SELECT o.* FROM orders o JOIN restaurant_profiles rp ON o.restaurant_id = rp.id WHERE o.id = %s AND rp.user_id = %s", (str(order_id), user_auth_id))
            elif user_type == 'client':
                cur.execute("SELECT o.* FROM orders o JOIN client_profiles cp ON o.client_id = cp.id WHERE o.id = %s AND cp.user_id = %s", (str(order_id), user_auth_id))
            else:
                return jsonify({"error": "Acesso não autorizado"}), 403
                
            order = cur.fetchone()
            if not order:
                return jsonify({"error": "Pedido não encontrado ou acesso negado"}), 404
            
            # NOTA: Esta é uma implementação simplificada.
            # Uma implementação completa teria uma tabela `order_status_history`
            # para registrar cada mudança de status.
            history = [{
                "status": STATUS_DISPLAY.get(order['status'], order['status']),
                "timestamp": order['updated_at'].isoformat(),
                "changed_by": "system" # Simplificado
            }]
            
            return jsonify({
                "status": "success",
                "order_id": str(order_id),
                "history": history
            }), 200
            
    except Exception as e:
        logger.error(f"Erro ao obter histórico do pedido: {e}", exc_info=True)
        return jsonify({"error": "Erro interno do servidor"}), 500
    finally:
        if conn:
            conn.close()
            logger.info("Conexão com banco fechada em get_order_status_history")

