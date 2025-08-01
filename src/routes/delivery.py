# src/routes/delivery.py

import os
import uuid
import traceback
import json
from flask import Blueprint, request, jsonify
import psycopg2
import psycopg2.extras
from datetime import date, timedelta, datetime
from decimal import Decimal
from functools import wraps

# Importa as funções e o cliente supabase do nosso helper centralizado
from ..utils.helpers import get_db_connection, get_user_id_from_token, supabase

delivery_bp = Blueprint('delivery_bp', __name__)

# --- Decorator para Segurança ---
def delivery_token_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        auth_header = request.headers.get('Authorization')
        user_auth_id, user_type, error_response = get_user_id_from_token(auth_header)
        
        if error_response:
            return error_response
        
        if user_type != 'delivery':
            return jsonify({"status": "error", "message": "Acesso não autorizado. Apenas para entregadores."}), 403
        
        conn = get_db_connection()
        if not conn:
            return jsonify({"status": "error", "message": "Erro de conexão com o banco de dados"}), 500
        
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute("SELECT id FROM delivery_profiles WHERE user_id = %s", (user_auth_id,))
                profile = cur.fetchone()
            
            if not profile:
                return jsonify({"status": "error", "message": "Perfil de entregador não encontrado para este usuário"}), 404
            
            profile_id = str(profile['id'])
        except Exception as e:
            traceback.print_exc()
            return jsonify({"status": "error", "message": "Erro interno ao buscar perfil do entregador", "detail": str(e)}), 500
        finally:
            if conn:
                conn.close()
        
        return f(profile_id=profile_id, *args, **kwargs)
    return decorated_function

# --- Funções Auxiliares ---
class CustomJSONEncoder(json.JSONEncoder):
    """Encoder JSON customizado para lidar com tipos de dados como Decimal, UUID e Datetime."""
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        if isinstance(obj, (datetime, date, timedelta)):
            return obj.isoformat()
        if isinstance(obj, uuid.UUID):
            return str(obj)
        return super(CustomJSONEncoder, self).default(obj)

def serialize_data_with_encoder(data):
    """Serializa dados usando o encoder customizado para garantir a conversão correta."""
    return json.loads(json.dumps(data, cls=CustomJSONEncoder))

# --- ROTAS ---

@delivery_bp.route('/profile', methods=['GET', 'PUT'])
@delivery_token_required
def delivery_profile_handler(profile_id):
    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Erro de conexão com o banco de dados"}), 500
    
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            if request.method == 'GET':
                cur.execute("SELECT * FROM delivery_profiles WHERE id = %s", (profile_id,))
                profile_data = cur.fetchone()
                if not profile_data:
                    return jsonify({"status": "error", "message": "Perfil de entregador não encontrado."}), 404
                return jsonify({"status": "success", "data": serialize_data_with_encoder(dict(profile_data))}), 200

            if request.method == 'PUT':
                data = request.get_json()
                if not data:
                    return jsonify({"status": "error", "message": "Nenhum dado fornecido"}), 400
                
                allowed_fields = ['first_name', 'last_name', 'phone', 'cpf', 'birth_date', 'vehicle_type', 'is_available', 'address_street', 'address_number', 'address_complement', 'address_neighborhood', 'address_city', 'address_state', 'address_zipcode', 'latitude', 'longitude']
                update_data = {key: data[key] for key in allowed_fields if key in data}
                
                if not update_data:
                    return jsonify({"status": "error", "message": "Nenhum campo válido para atualização"}), 400
                
                set_clauses = [f'"{field}" = %s' for field in update_data.keys()]
                params = list(update_data.values()) + [profile_id]
                sql = f"UPDATE delivery_profiles SET {', '.join(set_clauses)} WHERE id = %s RETURNING *"
                
                cur.execute(sql, tuple(params))
                updated_profile = cur.fetchone()
                conn.commit()
                
                if not updated_profile:
                    return jsonify({"status": "error", "message": "Perfil não encontrado para atualizar."}), 404
                
                return jsonify({"status": "success", "message": "Perfil atualizado com sucesso.", "data": serialize_data_with_encoder(dict(updated_profile))}), 200
    except Exception as e:
        if conn:
            conn.rollback()
        traceback.print_exc()
        return jsonify({"status": "error", "message": "Erro interno no perfil.", "detail": str(e)}), 500
    finally:
        if conn:
            conn.close()

@delivery_bp.route('/orders', methods=['GET'])
@delivery_token_required
def get_my_orders(profile_id):
    status_filter = request.args.get('status')
    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Erro de conexão com o banco de dados"}), 500
    
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            params = [profile_id]
            sql_query = "SELECT * FROM orders WHERE delivery_id = %s"
            
            if status_filter and status_filter.lower() != 'all':
                sql_query += " AND status = %s"
                params.append(status_filter)
            
            sql_query += " ORDER BY created_at DESC"
            cur.execute(sql_query, tuple(params))
            orders = cur.fetchall()
            
            return jsonify({"status": "success", "data": serialize_data_with_encoder([dict(o) for o in orders])}), 200
    except Exception as e:
        traceback.print_exc()
        return jsonify({"status": "error", "message": "Erro ao buscar meus pedidos.", "detail": str(e)}), 500
    finally:
        if conn:
            conn.close()

@delivery_bp.route('/orders/<uuid:order_id>', methods=['GET'], endpoint='get_single_order_details')
@delivery_token_required
def get_order_details(profile_id, order_id):
    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Erro de conexão com o banco de dados"}), 500
    
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # Query com a condição de JOIN corrigida para 'client_profiles'
            sql_query = """
                SELECT
                    o.id,
                    o.status,
                    o.delivery_address,
                    o.total_amount,
                    o.delivery_fee,
                    o.total_amount_items AS subtotal,
                    o.items,
                    o.created_at,
                    CONCAT(c.first_name, ' ', c.last_name) AS client_name,
                    c.phone AS client_phone,
                    r.restaurant_name AS restaurant_name,
                    r.phone AS restaurant_phone
                FROM
                    orders o
                LEFT JOIN
                    client_profiles c ON o.client_id = c.id
                LEFT JOIN
                    restaurant_profiles r ON o.restaurant_id = r.id
                WHERE
                    o.id = %s AND o.delivery_id = %s;
            """
            cur.execute(sql_query, (str(order_id), str(profile_id)))
            order = cur.fetchone()

            if not order:
                return jsonify({"status": "error", "message": "Pedido não encontrado ou não atribuído a você."}), 404

            order_data = dict(order)

            # Busca os nomes dos itens do menu
            item_ids = [item['menu_item_id'] for item in order_data.get('items', [])]
            if item_ids:
                cur.execute("SELECT id, name FROM menu_items WHERE id = ANY(%s)", (item_ids,))
                menu_items_map = {str(item['id']): item['name'] for item in cur.fetchall()}
                
                for item in order_data['items']:
                    item['name'] = menu_items_map.get(str(item['menu_item_id']), 'Item não encontrado')

            serialized_order = serialize_data_with_encoder(order_data)
            
            return jsonify({"status": "success", "data": serialized_order}), 200

    except psycopg2.Error as e:
        traceback.print_exc()
        return jsonify({"status": "error", "message": "Erro de banco de dados.", "detail": str(e)}), 500
    except Exception as e:
        traceback.print_exc()
        return jsonify({"status": "error", "message": "Erro interno ao buscar detalhes do pedido.", "detail": str(e)}), 500
    finally:
        if conn:
            conn.close()

@delivery_bp.route('/dashboard-stats', methods=['GET'])
@delivery_token_required
def get_dashboard_stats(profile_id):
    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Erro de conexão com o banco de dados"}), 500
    
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            today = date.today()
            cur.execute("""
                SELECT COUNT(*) AS today_deliveries, COALESCE(SUM(delivery_fee), 0) AS today_earnings
                FROM orders WHERE delivery_id = %s AND status = 'Entregue' AND DATE(created_at) = %s
            """, (profile_id, today))
            daily_stats = cur.fetchone()
            
            cur.execute("SELECT rating FROM delivery_profiles WHERE id = %s", (profile_id,))
            profile_stats = cur.fetchone()
            
            stats = {
                "todayDeliveries": daily_stats['today_deliveries'] if daily_stats else 0,
                "todayEarnings": float(daily_stats['today_earnings']) if daily_stats else 0.0,
                "avgRating": float(profile_stats['rating']) if profile_stats and profile_stats['rating'] else 0.0,
            }
            return jsonify({"status": "success", "data": stats}), 200
    except Exception as e:
        traceback.print_exc()
        return jsonify({"status": "error", "message": "Erro ao buscar estatísticas do dashboard.", "detail": str(e)}), 500
    finally:
        if conn:
            conn.close()

@delivery_bp.route('/earnings-history', methods=['GET'])
@delivery_token_required
def get_earnings_history(profile_id):
    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Erro de conexão com o banco de dados"}), 500
    
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                SELECT DATE(created_at) AS earning_date, SUM(delivery_fee) AS total_earned
                FROM orders
                WHERE delivery_id = %s AND status = 'Entregue' AND created_at >= %s
                GROUP BY DATE(created_at)
                ORDER BY earning_date DESC;
            """, (profile_id, date.today() - timedelta(days=7)))
            history = cur.fetchall()
            return jsonify({"status": "success", "data": serialize_data_with_encoder([dict(h) for h in history])}), 200
    except Exception as e:
        traceback.print_exc()
        return jsonify({"status": "error", "message": "Erro ao buscar histórico de ganhos.", "detail": str(e)}), 500
    finally:
        if conn:
            conn.close()
