# src/routes/delivery_orders.py - VERSÃO COMPLETA E CORRIGIDA

import os
import uuid
import traceback
import json
import logging
from flask import Blueprint, request, jsonify, g, current_app
import psycopg2
import psycopg2.extras
from datetime import date, timedelta, datetime, time
from decimal import Decimal
from ..utils.platform_settings import calculate_platform_commission, calculate_courier_payout
from functools import wraps
from flask_cors import cross_origin

from ..utils.helpers import get_db_connection, get_user_id_from_token, supabase

delivery_orders_bp = Blueprint('delivery_orders_bp', __name__)

@delivery_orders_bp.before_request
def handle_options():
    if request.method == "OPTIONS":
        response = jsonify()
        response.headers.add("Access-Control-Allow-Origin", request.headers.get("Origin", "*"))
        response.headers.add("Access-Control-Allow-Headers", "Content-Type,Authorization")
        response.headers.add("Access-Control-Allow-Methods", "GET,PUT,POST,DELETE,OPTIONS")
        response.headers.add("Access-Control-Allow-Credentials", "true")
        return response

def delivery_token_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if request.method == "OPTIONS":
            return f(*args, **kwargs)
            
        conn = None 
        try:
            auth_header = request.headers.get('Authorization')
            if not auth_header:
                return jsonify({"status": "error", "message": "Token de autorização ausente"}), 401
            
            user_auth_id, user_type, error_response = get_user_id_from_token(auth_header)
            
            if error_response:
                return error_response
            
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
            
            g.profile_id = str(profile['id'])
            g.user_auth_id = str(user_auth_id)

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

class CustomJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal): return float(obj)
        if isinstance(obj, (datetime, date, timedelta, time)): return obj.isoformat()
        if isinstance(obj, uuid.UUID): return str(obj)
        return super().default(obj)

def serialize_data_with_encoder(data):
    return json.loads(json.dumps(data, cls=CustomJSONEncoder))

@delivery_orders_bp.route('/orders-by-status', methods=['GET'])
@cross_origin()
@delivery_token_required
def get_orders_by_status():
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

@delivery_orders_bp.route('/orders/<order_id>', methods=['GET'])
@cross_origin()
@delivery_token_required
def get_order_details(order_id):
    conn = None
    try:
        profile_id = g.profile_id
        
        conn = get_db_connection()
        if not conn:
            return jsonify({"status": "error", "message": "Erro de conexão com o banco de dados"}), 500
        
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                SELECT o.*, 
                       cp.first_name || ' ' || cp.last_name AS client_name,
                       cp.phone AS client_phone,
                       rp.restaurant_name,
                       rp.address_street as restaurant_street,
                       rp.address_number as restaurant_number,
                       rp.address_neighborhood as restaurant_neighborhood,
                       rp.address_city as restaurant_city,
                       rp.address_state as restaurant_state,
                       rp.phone AS restaurant_phone
                FROM orders o
                LEFT JOIN client_profiles cp ON o.client_id = cp.id
                LEFT JOIN restaurant_profiles rp ON o.restaurant_id = rp.id
                WHERE o.id = %s AND o.delivery_id = %s
            """, (order_id, profile_id))
            
            order = cur.fetchone()

            if not order:
                return jsonify({"status": "error", "message": "Pedido não encontrado"}), 404

            order_dict = dict(order)
            # Os itens do pedido ficam em orders.items (jsonb) -- a tabela
            # order_items nunca e populada. Antes esta rota lia de order_items
            # e o entregador via o pedido SEM lista de itens.
            raw_items = order_dict.get('items')
            if isinstance(raw_items, str):
                try:
                    raw_items = json.loads(raw_items)
                except Exception:
                    raw_items = []
            order_dict['items'] = raw_items or []

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

# O blueprint já entra com prefixo /api/delivery/orders (main.py:283), então a
# rota NÃO deve repetir /orders — senão o caminho vira /api/delivery/orders/orders/...
# e o app do entregador (que chama POST /api/delivery/orders/<id>/cash-payment)
# tomava 404 ao confirmar recebimento em dinheiro.
@delivery_orders_bp.route('/<order_id>/cash-payment', methods=['POST'])
@cross_origin()
@delivery_token_required
def confirm_cash_payment(order_id):
    """Entregador confirma recebimento do dinheiro. Registra débito e atualiza perfil."""
    conn = None
    try:
        profile_id = g.profile_id
        conn = get_db_connection()
        if not conn:
            return jsonify({"status": "error", "message": "Erro de conexão com o banco de dados"}), 500

        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                SELECT o.id, o.status, o.payment_method, o.total_amount,
                       o.delivery_fee, o.change_for, o.comissao_plataforma, o.restaurant_id
                FROM orders o
                WHERE o.id = %s AND o.delivery_id = %s
            """, (order_id, profile_id))
            order = cur.fetchone()

            if not order:
                return jsonify({"status": "error", "message": "Pedido não encontrado"}), 404
            if order['payment_method'] != 'cash':
                return jsonify({"status": "error", "message": "Este pedido não é em dinheiro"}), 400
            if order['status'] != 'delivered':
                return jsonify({"status": "error", "message": "Pedido ainda não foi entregue"}), 400

            # Liquidação idempotente (mesma lógica do fechamento da entrega):
            # se o pedido já foi liquidado no complete_order, isto NÃO duplica a
            # dívida — apenas recalcula e devolve o resumo pro app mostrar. Não é
            # mais erro confirmar um pedido já liquidado.
            from ..utils.cash_settlement import settle_cash_order
            breakdown, _was_new = settle_cash_order(
                cur, order_id, profile_id, order['restaurant_id'],
                order['total_amount'], order['delivery_fee'], order.get('comissao_plataforma'))
            conn.commit()

        return jsonify({
            "status": "success",
            "message": "Recebimento em dinheiro confirmado!",
            "data": {
                "voce_recebeu": breakdown["total_amount"],
                "sua_taxa": breakdown["courier_freight"],
                "deve_a_plataforma": breakdown["cash_debt"],
                "comissao": breakdown["commission"],
                "repasse_restaurante": breakdown["restaurant_share"],
            }
        }), 200

    except psycopg2.Error as e:
        if conn: conn.rollback()
        return jsonify({"status": "error", "message": "Erro de banco de dados", "detail": str(e)}), 500
    except Exception as e:
        if conn: conn.rollback()
        traceback.print_exc()
        return jsonify({"status": "error", "message": "Erro interno do servidor", "detail": str(e)}), 500
    finally:
        if conn: conn.close()


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
                WHERE o.status IN ('pending', 'accepted', 'preparing', 'ready') AND o.delivery_id IS NULL
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
