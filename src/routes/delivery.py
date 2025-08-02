# src/routes/delivery.py

import os
import uuid
import traceback
import json
from flask import Blueprint, request, jsonify, g
import psycopg2
import psycopg2.extras
from datetime import date, timedelta, datetime, time
from decimal import Decimal
from functools import wraps
from flask_cors import cross_origin

# Importa as funções e o cliente supabase do nosso helper centralizado
from ..utils.helpers import get_db_connection, get_user_id_from_token, supabase

# Define o Blueprint para as rotas de delivery
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
            
            g.profile_id = str(profile['id'])
            return f(*args, **kwargs)

        except psycopg2.Error as e:
            traceback.print_exc()
            return jsonify({"status": "error", "message": "Erro de banco de dados", "detail": str(e)}), 500
        finally:
            if conn:
                conn.close()
    
    return decorated_function

# --- Encoder JSON Customizado ---
class CustomJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        if isinstance(obj, (datetime, date, timedelta, time)):
            return obj.isoformat()
        if isinstance(obj, uuid.UUID):
            return str(obj)
        return super().default(obj)

def serialize_data_with_encoder(data):
    return json.loads(json.dumps(data, cls=CustomJSONEncoder))

# --- Rotas ---

@delivery_bp.route('/profile', methods=['GET', 'PUT'])
@delivery_token_required
def delivery_profile_handler():
    profile_id = g.profile_id
    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Erro de conexão com o banco de dados"}), 500
    
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            if request.method == 'GET':
                cur.execute("SELECT * FROM delivery_profiles WHERE id = %s", (profile_id,))
                profile_data = cur.fetchone()
                if not profile_data:
                    return jsonify({"status": "error", "message": "Perfil não encontrado"}), 404
                return jsonify({"status": "success", "data": serialize_data_with_encoder(dict(profile_data))}), 200

            elif request.method == 'PUT':
                data = request.get_json()
                if not data:
                    return jsonify({"status": "error", "message": "Dados não fornecidos"}), 400
                
                allowed_fields = ['first_name', 'last_name', 'phone', 'vehicle_type', 'is_available']
                update_data = {k: v for k, v in data.items() if k in allowed_fields}
                
                if not update_data:
                    return jsonify({"status": "error", "message": "Nenhum campo válido para atualização"}), 400
                
                set_clauses = [f'"{k}" = %s' for k in update_data.keys()]
                params = list(update_data.values()) + [profile_id]
                
                cur.execute(
                    f"UPDATE delivery_profiles SET {', '.join(set_clauses)} WHERE id = %s RETURNING *",
                    params
                )
                updated_profile = cur.fetchone()
                conn.commit()
                
                return jsonify({
                    "status": "success",
                    "data": serialize_data_with_encoder(dict(updated_profile))
                }), 200

    except psycopg2.Error as e:
        conn.rollback()
        return jsonify({"status": "error", "message": "Erro de banco de dados", "detail": str(e)}), 500
    finally:
        if conn:
            conn.close()

@delivery_bp.route('/orders', methods=['GET'])
@delivery_token_required
def get_my_orders():
    profile_id = g.profile_id
    status_filter = request.args.get('status', 'all').lower()
    
    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Erro de conexão com o banco de dados"}), 500
    
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            base_query = """
                SELECT o.*, 
                       cp.first_name || ' ' || cp.last_name AS client_name,
                       rp.restaurant_name
                FROM orders o
                LEFT JOIN client_profiles cp ON o.client_id = cp.id
                LEFT JOIN restaurant_profiles rp ON o.restaurant_id = rp.id
                WHERE o.delivery_id = %s
            """
            params = [profile_id]
            
            if status_filter != 'all':
                base_query += " AND o.status = %s"
                params.append(status_filter.capitalize())
            
            base_query += " ORDER BY o.created_at DESC"
            cur.execute(base_query, tuple(params))
            orders = cur.fetchall()
            
            return jsonify({
                "status": "success",
                "data": serialize_data_with_encoder([dict(o) for o in orders])
            }), 200
            
    except psycopg2.Error as e:
        return jsonify({"status": "error", "message": "Erro de banco de dados", "detail": str(e)}), 500
    finally:
        if conn:
            conn.close()

@delivery_bp.route('/dashboard-stats', methods=['GET'])
@cross_origin(origins=["http://localhost:5173", "http://127.0.0.1:5173"])
@delivery_token_required
def get_dashboard_stats():
    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Erro de conexão com o banco de dados"}), 500
    
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            profile_id = g.profile_id
            today = date.today()
            
            # Estatísticas principais em uma única query
            cur.execute("""
                SELECT 
                    COUNT(CASE WHEN o.status = 'Entregue' AND DATE(o.created_at) = %s THEN 1 END) AS today_deliveries,
                    COALESCE(SUM(CASE WHEN o.status = 'Entregue' AND DATE(o.created_at) = %s THEN o.delivery_fee END), 0) AS today_earnings,
                    dp.rating,
                    dp.total_deliveries,
                    (
                        SELECT json_agg(json_build_object(
                            'id', o.id,
                            'status', o.status,
                            'client_name', cp.first_name || ' ' || cp.last_name,
                            'restaurant_name', rp.restaurant_name,
                            'delivery_address', o.delivery_address,
                            'delivery_fee', o.delivery_fee,
                            'created_at', o.created_at
                        ))
                        FROM orders o
                        LEFT JOIN client_profiles cp ON o.client_id = cp.id
                        LEFT JOIN restaurant_profiles rp ON o.restaurant_id = rp.id
                        WHERE o.delivery_id = %s 
                          AND o.status IN ('Pendente', 'Aceito', 'Para Entrega')
                    ) AS active_orders
                FROM delivery_profiles dp
                LEFT JOIN orders o ON dp.id = o.delivery_id
                WHERE dp.id = %s
                GROUP BY dp.id, dp.rating, dp.total_deliveries
            """, (today, today, profile_id, profile_id))
            
            stats = cur.fetchone()
            
            if not stats:
                return jsonify({"status": "error", "message": "Perfil não encontrado"}), 404
            
            return jsonify({
                "status": "success",
                "data": {
                    "todayDeliveries": stats['today_deliveries'] or 0,
                    "todayEarnings": float(stats['today_earnings']) if stats['today_earnings'] is not None else 0.0,
                    "avgRating": float(stats['rating']) if stats['rating'] is not None else 0.0,
                    "totalDeliveries": stats['total_deliveries'] or 0,
                    "activeOrders": stats['active_orders'] or []
                }
            }), 200
            
    except psycopg2.Error as e:
        return jsonify({"status": "error", "message": "Erro de banco de dados", "detail": str(e)}), 500
    finally:
        if conn:
            conn.close()

@delivery_bp.route('/earnings-history', methods=['GET'])
@delivery_token_required
def get_earnings_history():
    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Erro de conexão com o banco de dados"}), 500
    
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                SELECT 
                    DATE(created_at) AS date,
                    SUM(delivery_fee) AS earnings,
                    COUNT(*) AS deliveries
                FROM orders
                WHERE delivery_id = %s 
                  AND status = 'Entregue'
                  AND created_at >= CURRENT_DATE - INTERVAL '7 days'
                GROUP BY DATE(created_at)
                ORDER BY date DESC
            """, (g.profile_id,))
            
            history = cur.fetchall()
            return jsonify({
                "status": "success",
                "data": serialize_data_with_encoder([dict(h) for h in history])
            }), 200
            
    except psycopg2.Error as e:
        return jsonify({"status": "error", "message": "Erro de banco de dados", "detail": str(e)}), 500
    finally:
        if conn:
            conn.close()