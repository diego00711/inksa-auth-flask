# src/routes/delivery_orders.py - VERSÃO COMPLETA E CORRIGIDA

import os
import uuid
import traceback
import json
from flask import Blueprint, request, jsonify, g, current_app
import psycopg2
import psycopg2.extras
from datetime import date, timedelta, datetime, time
from decimal import Decimal
from functools import wraps
from flask_cors import cross_origin

from ..utils.helpers import get_db_connection, get_user_id_from_token, supabase
from .gamification_routes import add_points_for_event

# --- Blueprint e Rotas ---
delivery_orders_bp = Blueprint('delivery_orders_bp', __name__)

# --- Handler para requisições OPTIONS ---
@delivery_orders_bp.before_request
def handle_options():
    if request.method == "OPTIONS":
        response = jsonify()
        response.headers.add("Access-Control-Allow-Origin", request.headers.get("Origin", "*"))
        response.headers.add("Access-Control-Allow-Headers", "Content-Type,Authorization")
        response.headers.add("Access-Control-Allow-Methods", "GET,PUT,POST,DELETE,OPTIONS")
        response.headers.add("Access-Control-Allow-Credentials", "true")
        return response

# --- Decorator de Autenticação CORRIGIDO ---
def delivery_token_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # ✅ CORREÇÃO: Permitir requisições OPTIONS sem autenticação
        if request.method == "OPTIONS":
            return f(*args, **kwargs)
            
        conn = None 
        try:
            auth_header = request.headers.get('Authorization')
            if not auth_header:
                return jsonify({"status": "error", "message": "Token de autorização ausente"}), 401
            
            # ✅ CORREÇÃO: Chama a função corretamente
            user_auth_id, user_type, error_response = get_user_id_from_token(auth_header)
            
            if error_response:
                return error_response
            
            # Verifica se o tipo de usuário é o correto
            if user_type != 'delivery':
                return jsonify({"status": "error", "message": "Acesso não autorizado. Apenas para entregadores."}), 403
            
            conn = get_db_connection()
            if not conn:
                return jsonify({"status": "error", "message": "Erro de conexão com o banco de dados"}), 500
            
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute("SELECT id FROM delivery_profiles WHERE user_id = %s", (user_auth_id,))
                profile = cur.fetchone()
            
            if not profile:
                return jsonify({"status": "error", "message": "Perfil de entregador não encontrado para este usuário"}), 404
            
            # Armazena o ID do perfil no contexto global
            g.profile_id = str(profile['id'])
            g.user_auth_id = str(user_auth_id)

            # ✅ CORREÇÃO: Executa a função original
            return f(*args, **kwargs)

        except psycopg2.Error as e:
            traceback.print_exc()
            return jsonify({"status": "error", "message": "Erro de banco de dados", "detail": str(e)}), 500
        except Exception as e:
            traceback.print_exc()
            return jsonify({"status": "error", "message": "Erro interno no servidor", "detail": str(e)}), 500
        finally:
            if conn:
                conn.close()
    
    return decorated_function

# --- Encoder JSON Customizado ---
class CustomJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal): return float(obj)
        if isinstance(obj, (datetime, date, timedelta, time)): return obj.isoformat()
        if isinstance(obj, uuid.UUID): return str(obj)
        return super().default(obj)

def serialize_data_with_encoder(data):
    return json.loads(json.dumps(data, cls=CustomJSONEncoder))

# --- Rota para buscar entregas por status (NOVA) ---
@delivery_orders_bp.route('/orders-by-status', methods=['GET'])
@cross_origin()
@delivery_token_required
def get_orders_by_status():
    """Busca entregas por status - CORREÇÃO PARA O ERRO 'getDeliveriesByStatus is not a function'"""
    conn = None
    try:
        status = request.args.get('status', 'all')
        profile_id = g.profile_id
        
        conn = get_db_connection()
        if not conn:
            return jsonify({"status": "error", "message": "Erro de conexão com o banco de dados"}), 500
        
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            base_query = """
                SELECT o.*, 
                       cp.first_name || ' ' || cp.last_name AS client_name,
                       rp.restaurant_name,
                       rp.address_street as restaurant_street,
                       rp.address_number as restaurant_number,
                       rp.address_neighborhood as restaurant_neighborhood,
                       rp.address_city as restaurant_city,
                       rp.address_state as restaurant_state
                FROM orders o
                LEFT JOIN client_profiles cp ON o.client_id = cp.id
                LEFT JOIN restaurant_profiles rp ON o.restaurant_id = rp.id
                WHERE o.delivery_id = %s
            """
            params = [profile_id]
            
            if status != 'all':
                base_query += " AND o.status = %s"
                params.append(status.capitalize())
            
            base_query += " ORDER BY o.created_at DESC"
            cur.execute(base_query, tuple(params))
            orders = cur.fetchall()
            
            return jsonify({
                "status": "success",
                "data": serialize_data_with_encoder([dict(o) for o in orders])
            }), 200
            
    except psycopg2.Error as e:
        return jsonify({"status": "error", "message": "Erro de banco de dados", "detail": str(e)}), 500
    except Exception as e:
        return jsonify({"status": "error", "message": "Erro interno do servidor", "detail": str(e)}), 500
    finally:
        if conn:
            conn.close()

# --- Rota para buscar pedidos do entregador ---
@delivery_orders_bp.route('/orders', methods=['GET'])
@cross_origin()
@delivery_token_required
def get_my_orders():
    conn = None
    try:
        profile_id = g.profile_id
        status_filter = request.args.get('status', 'all').lower()
        
        conn = get_db_connection()
        if not conn:
            return jsonify({"status": "error", "message": "Erro de conexão com o banco de dados"}), 500
        
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
    except Exception as e:
        return jsonify({"status": "error", "message": "Erro interno do servidor", "detail": str(e)}), 500
    finally:
        if conn:
            conn.close()

# --- Rota para buscar detalhes de um pedido específico ---
@delivery_orders_bp.route('/orders/pending', methods=['GET'])
@cross_origin()
@delivery_token_required
def get_pending_orders():
    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({"status": "error", "message": "Erro de conexão com o banco de dados"}), 500
        
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # 🔧 CORREÇÃO: Buscar pedidos com status 'pending' OU 'accepted'
            cur.execute("""
                SELECT o.*, 
                       cp.first_name || ' ' || cp.last_name AS client_name,
                       rp.restaurant_name,
                       rp.address_street as restaurant_street,
                       rp.address_number as restaurant_number,
                       rp.address_neighborhood as restaurant_neighborhood,
                       rp.address_city as restaurant_city,
                       rp.address_state as restaurant_state,
                       rp.latitude as restaurant_latitude,
                       rp.longitude as restaurant_longitude
                FROM orders o
                LEFT JOIN client_profiles cp ON o.client_id = cp.id
                LEFT JOIN restaurant_profiles rp ON o.restaurant_id = rp.id
                WHERE o.status IN ('pending', 'accepted') AND o.delivery_id IS NULL
                ORDER BY o.created_at DESC
            """)
            
            orders = cur.fetchall()
            
            return jsonify({
                "status": "success",
                "data": serialize_data_with_encoder([dict(o) for o in orders])
            }), 200
            
    except psycopg2.Error as e:
        return jsonify({"status": "error", "message": "Erro de banco de dados", "detail": str(e)}), 500
    except Exception as e:
        return jsonify({"status": "error", "message": "Erro interno do servidor", "detail": str(e)}), 500
    finally:
        if conn:
            conn.close()
            
            if not order:
                return jsonify({"status": "error", "message": "Pedido não encontrado"}), 404
            
            # Buscar itens do pedido
            cur.execute("""
                SELECT oi.*, mi.name as item_name
                FROM order_items oi
                LEFT JOIN menu_items mi ON oi.menu_item_id = mi.id
                WHERE oi.order_id = %s
            """, (order_id,))
            
            items = cur.fetchall()
            order_dict = dict(order)
            order_dict['items'] = [dict(item) for item in items]
            
            return jsonify({
                "status": "success",
                "data": serialize_data_with_encoder(order_dict)
            }), 200
            
    except psycopg2.Error as e:
        return jsonify({"status": "error", "message": "Erro de banco de dados", "detail": str(e)}), 500
    except Exception as e:
        return jsonify({"status": "error", "message": "Erro interno do servidor", "detail": str(e)}), 500
    finally:
        if conn:
            conn.close()

# --- Rota para aceitar uma entrega ---
@delivery_orders_bp.route('/orders/<order_id>/accept', methods=['POST'])
@cross_origin()
@delivery_token_required
def accept_delivery(order_id):
    conn = None
    try:
        profile_id = g.profile_id
        
        conn = get_db_connection()
        if not conn:
            return jsonify({"status": "error", "message": "Erro de conexão com o banco de dados"}), 500
        
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # Verificar se o pedido está disponível para aceitação
            cur.execute("""
                SELECT status FROM orders 
                WHERE id = %s AND (delivery_id IS NULL OR delivery_id = %s)
            """, (order_id, profile_id))
            
            order = cur.fetchone()
            
            if not order:
                return jsonify({"status": "error", "message": "Pedido não encontrado ou já atribuído"}), 404
            
            if order['status'] != 'Pendente':
                return jsonify({"status": "error", "message": "Pedido não está disponível para aceitação"}), 400
            
            # Atualizar o pedido
            cur.execute("""
                UPDATE orders 
                SET delivery_id = %s, status = 'Aceito', updated_at = NOW()
                WHERE id = %s
                RETURNING *
            """, (profile_id, order_id))
            
            updated_order = cur.fetchone()
            conn.commit()
            
            # Adicionar pontos de gamificação
            try:
                add_points_for_event(profile_id, 'order_accepted', {
                    'order_id': order_id,
                    'delivery_fee': updated_order['delivery_fee']
                })
            except Exception as gamification_error:
                print(f"Erro na gamificação: {gamification_error}")
                # Não falha a operação principal por causa da gamificação
            
            return jsonify({
                "status": "success",
                "message": "Pedido aceito com sucesso",
                "data": serialize_data_with_encoder(dict(updated_order))
            }), 200
            
    except psycopg2.Error as e:
        if conn:
            conn.rollback()
        return jsonify({"status": "error", "message": "Erro de banco de dados", "detail": str(e)}), 500
    except Exception as e:
        if conn:
            conn.rollback()
        return jsonify({"status": "error", "message": "Erro interno do servidor", "detail": str(e)}), 500
    finally:
        if conn:
            conn.close()

# --- Rota para completar uma entrega ---
@delivery_orders_bp.route('/orders/<order_id>/complete', methods=['POST'])
@cross_origin()
@delivery_token_required
def complete_delivery(order_id):
    conn = None
    try:
        profile_id = g.profile_id
        
        conn = get_db_connection()
        if not conn:
            return jsonify({"status": "error", "message": "Erro de conexão com o banco de dados"}), 500
        
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # Verificar se o pedido pertence ao entregador e pode ser completado
            cur.execute("""
                SELECT status, delivery_id FROM orders 
                WHERE id = %s
            """, (order_id,))
            
            order = cur.fetchone()
            
            if not order:
                return jsonify({"status": "error", "message": "Pedido não encontrado"}), 404
            
            if order['delivery_id'] != profile_id:
                return jsonify({"status": "error", "message": "Este pedido não pertence a você"}), 403
            
            if order['status'] not in ['Aceito', 'A caminho']:
                return jsonify({"status": "error", "message": "Pedido não pode ser marcado como entregue"}), 400
            
            # Atualizar o pedido
            cur.execute("""
                UPDATE orders 
                SET status = 'Concluído', updated_at = NOW(), completed_at = NOW()
                WHERE id = %s
                RETURNING *
            """, (order_id,))
            
            updated_order = cur.fetchone()
            
            # Atualizar estatísticas do entregador
            cur.execute("""
                UPDATE delivery_profiles 
                SET total_deliveries = total_deliveries + 1,
                    total_earnings = total_earnings + %s,
                    updated_at = NOW()
                WHERE id = %s
            """, (updated_order['delivery_fee'], profile_id))
            
            conn.commit()
            
            # Adicionar pontos de gamificação
            try:
                add_points_for_event(profile_id, 'order_completed', {
                    'order_id': order_id,
                    'delivery_fee': updated_order['delivery_fee'],
                    'completion_time': datetime.now().isoformat()
                })
            except Exception as gamification_error:
                print(f"Erro na gamificação: {gamification_error}")
                # Não falha a operação principal por causa da gamificação
            
            return jsonify({
                "status": "success",
                "message": "Pedido marcado como entregue com sucesso",
                "data": serialize_data_with_encoder(dict(updated_order))
            }), 200
            
    except psycopg2.Error as e:
        if conn:
            conn.rollback()
        return jsonify({"status": "error", "message": "Erro de banco de dados", "detail": str(e)}), 500
    except Exception as e:
        if conn:
            conn.rollback()
        return jsonify({"status": "error", "message": "Erro interno do servidor", "detail": str(e)}), 500
    finally:
        if conn:
            conn.close()

# --- Rota para buscar pedidos pendentes (disponíveis para aceitação) ---
@delivery_orders_bp.route('/orders/pending', methods=['GET'])
@cross_origin()
@delivery_token_required
def get_pending_orders():
    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({"status": "error", "message": "Erro de conexão com o banco de dados"}), 500
        
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                SELECT o.*, 
                       cp.first_name || ' ' || cp.last_name AS client_name,
                       rp.restaurant_name,
                       rp.address_street as restaurant_street,
                       rp.address_number as restaurant_number,
                       rp.address_neighborhood as restaurant_neighborhood,
                       rp.address_city as restaurant_city,
                       rp.address_state as restaurant_state,
                       rp.latitude as restaurant_latitude,
                       rp.longitude as restaurant_longitude
                FROM orders o
                LEFT JOIN client_profiles cp ON o.client_id = cp.id
                LEFT JOIN restaurant_profiles rp ON o.restaurant_id = rp.id
                WHERE o.status = 'Pendente' AND o.delivery_id IS NULL
                ORDER BY o.created_at DESC
            """)
            
            orders = cur.fetchall()
            
            return jsonify({
                "status": "success",
                "data": serialize_data_with_encoder([dict(o) for o in orders])
            }), 200
            
    except psycopg2.Error as e:
        return jsonify({"status": "error", "message": "Erro de banco de dados", "detail": str(e)}), 500
    except Exception as e:
        return jsonify({"status": "error", "message": "Erro interno do servidor", "detail": str(e)}), 500
    finally:
        if conn:
            conn.close()
