# src/routes/orders.py

import os
import uuid
from datetime import datetime
from flask import Blueprint, request, jsonify
import psycopg2
import psycopg2.extras
import traceback
import json
import logging

# Importa as funções centralizadas do helpers.py
from ..utils.helpers import get_db_connection, get_user_id_from_token

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

try:
    from .delivery_calculator import haversine_distance, FIXED_DELIVERY_FEE, PER_KM_DELIVERY_FEE, FREE_DELIVERY_THRESHOLD_KM
except ImportError:
    logging.warning("Módulo delivery_calculator não encontrado. Usando valores fixos para cálculo de frete.")
    def haversine_distance(lat1, lon1, lat2, lon2): return 5.0
    FIXED_DELIVERY_FEE = 5.0
    PER_KM_DELIVERY_FEE = 1.5
    FREE_DELIVERY_THRESHOLD_KM = 2.0

orders_bp = Blueprint('orders_bp', __name__)

@orders_bp.route('/orders', methods=['GET', 'POST'])
def handle_orders():
    user_auth_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
    if error: return error

    conn = get_db_connection()
    if not conn: return jsonify({"error": "Erro de conexão com o banco de dados"}), 500

    try:
        # --- LÓGICA PARA BUSCAR PEDIDOS (GET) ---
        if request.method == 'GET':
            if user_type not in ['restaurant', 'admin', 'client']:
                return jsonify({"error": "Acesso não autorizado para buscar pedidos."}), 403

            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                orders = []
                if user_type == 'restaurant':
                    sql_query = """
                        SELECT o.*, cp.first_name AS client_first_name, cp.last_name AS client_last_name
                        FROM orders o LEFT JOIN client_profiles cp ON o.client_id = cp.id
                        WHERE o.restaurant_id = %s ORDER BY o.created_at DESC;
                    """
                    cur.execute(sql_query, (user_auth_id,))
                    orders = [dict(row) for row in cur.fetchall()]
                
                elif user_type == 'client':
                    cur.execute("SELECT id FROM client_profiles WHERE user_id = %s", (user_auth_id,))
                    client_profile = cur.fetchone()
                    if not client_profile:
                        return jsonify({"data": []}), 200
                    client_profile_id = client_profile['id']
                    sql_query = """
                        SELECT o.*, rp.restaurant_name, rp.logo_url AS restaurant_logo_url
                        FROM orders o LEFT JOIN restaurant_profiles rp ON o.restaurant_id = rp.id
                        WHERE o.client_id = %s ORDER BY o.created_at DESC;
                    """
                    cur.execute(sql_query, (client_profile_id,))
                    orders = [dict(row) for row in cur.fetchall()]
            
            return jsonify({"status": "success", "data": orders}), 200

        # --- LÓGICA PARA CRIAR UM NOVO PEDIDO (POST) ---
        if request.method == 'POST':
            if user_type != 'client':
                return jsonify({"error": "Apenas clientes podem criar pedidos"}), 403

            data = request.get_json()
            required_fields = [
                'id', 'restaurant_id', 'delivery_id', 'items', 'client_latitude', 
                'client_longitude', 'delivery_address', 'total_amount_items', 'delivery_fee'
            ]
            if not all(field in data for field in required_fields):
                missing_fields = [field for field in required_fields if field not in data]
                return jsonify({"error": f"Campo(s) obrigatório(s) ausente(s): {', '.join(missing_fields)}"}), 400

            with conn:
                with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                    cur.execute("SELECT id, latitude, longitude, delivery_type, delivery_fee FROM restaurant_profiles WHERE id = %s", (data['restaurant_id'],))
                    restaurant_data = cur.fetchone()
                    if not restaurant_data:
                        return jsonify({"error": "Restaurante não encontrado"}), 404

                    cur.execute("SELECT id FROM client_profiles WHERE user_id = %s", (user_auth_id,))
                    client_profile = cur.fetchone()
                    if not client_profile:
                        return jsonify({"error": "O perfil de cliente para este usuário não foi encontrado."}), 404
                    client_profile_id = client_profile['id']

                    cur.execute("SELECT id FROM delivery_profiles WHERE id = %s", (data['delivery_id'],))
                    delivery_profile = cur.fetchone()
                    if not delivery_profile:
                        return jsonify({"error": "Perfil de entregador não encontrado. Verifique se o ID existe."}), 404

                    delivery_type = restaurant_data.get('delivery_type', 'platform')
                    distance_km, _ = 0.0, 0.0

                    if delivery_type == 'platform':
                        if restaurant_data['latitude'] is None or restaurant_data['longitude'] is None:
                            return jsonify({"error": "Coordenadas do restaurante incompletas para cálculo de frete."}), 400
                        distance_km = haversine_distance(float(restaurant_data['latitude']), float(restaurant_data['longitude']), float(data['client_latitude']), float(data['client_longitude']))
                    
                    final_delivery_fee = float(data.get('delivery_fee', 0.0))
                    total_amount_items = float(data.get('total_amount_items', 0.0))
                    total_amount = total_amount_items + final_delivery_fee

                    order_data = {
                        'id': data['id'], 'client_id': client_profile_id, 'restaurant_id': data['restaurant_id'],
                        'delivery_id': data['delivery_id'], 'items': json.dumps(data['items']),
                        'total_amount': round(total_amount, 2), 'status': 'Pendente',
                        'delivery_address': data['delivery_address'],
                        'total_amount_items': round(total_amount_items, 2),
                        'delivery_fee': round(final_delivery_fee, 2),
                        'delivery_distance_km': round(distance_km, 2),
                        'created_at': datetime.now(), 'updated_at': datetime.now(),
                    }

                    columns = ', '.join(order_data.keys())
                    placeholders = ', '.join(['%s'] * len(order_data))
                    cur.execute(
                        f"INSERT INTO orders ({columns}) VALUES ({placeholders}) RETURNING id, total_amount, delivery_fee",
                        tuple(order_data.values())
                    )
                    new_order = cur.fetchone()
                    conn.commit()
                    logging.info(f"Pedido {new_order['id']} inserido com sucesso no Supabase.")

            return jsonify({"status": "success", "message": "Pedido criado com sucesso", "data": dict(new_order)}), 201

    except psycopg2.errors.ForeignKeyViolation as fkv_error:
        logging.error(f"Erro de Chave Estrangeira: {fkv_error.pgerror}", exc_info=True)
        return jsonify({"error": "Erro de dados: Chave estrangeira inválida."}), 400
    except Exception as e:
        traceback.print_exc()
        logging.error(f"Erro ao processar pedido: {e}", exc_info=True)
        return jsonify({"error": "Erro interno ao processar o pedido.", "detail": str(e)}), 500
    finally:
        if conn: conn.close()


# ✅ NOVA ROTA ADICIONADA PARA ATUALIZAR O STATUS DE UM PEDIDO
@orders_bp.route('/orders/<uuid:order_id>/status', methods=['PUT'])
def update_order_status(order_id):
    user_auth_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
    if error: return error

    # Apenas restaurantes podem mudar o status dos pedidos
    if user_type != 'restaurant':
        return jsonify({"error": "Acesso não autorizado para atualizar status do pedido."}), 403

    data = request.get_json()
    new_status = data.get('status')

    if not new_status:
        return jsonify({"error": "Novo status não fornecido."}), 400

    conn = get_db_connection()
    if not conn: return jsonify({"error": "Erro de conexão com o banco de dados"}), 500

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # Atualiza o status do pedido, garantindo que o pedido pertence ao restaurante logado (por segurança)
            cur.execute(
                "UPDATE orders SET status = %s, updated_at = %s WHERE id = %s AND restaurant_id = %s RETURNING *",
                (new_status, datetime.now(), str(order_id), user_auth_id)
            )
            updated_order = cur.fetchone()
            conn.commit()

        if not updated_order:
            return jsonify({"error": "Pedido não encontrado ou não pertence a este restaurante."}), 404

        return jsonify({"status": "success", "message": "Status do pedido atualizado com sucesso.", "data": dict(updated_order)}), 200

    except Exception as e:
        if conn: conn.rollback()
        traceback.print_exc()
        return jsonify({"error": "Erro interno ao atualizar o status do pedido.", "detail": str(e)}), 500
    finally:
        if conn: conn.close()