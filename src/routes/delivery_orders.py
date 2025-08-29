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

# --- Decorator de Autenticação CORRIGIDO ---
def delivery_token_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        conn = None 
        try:
            auth_header = request.headers.get('Authorization')
            # O helper get_user_id_from_token retorna 3 valores
            user_auth_id, user_type, error_response = get_user_id_from_token(auth_header)
            
            # Se o helper retornou um erro (ex: token expirado), retorne-o imediatamente.
            if error_response:
                return error_response
            
            # Verifica se o tipo de usuário é o correto para esta rota
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
            
            # Armazena o ID do perfil no contexto global da requisição (g)
            g.profile_id = str(profile['id']) 

            # <<< CORREÇÃO VITAL AQUI >>>
            # Executa a função da rota original e RETORNA sua resposta.
            return f(*args, **kwargs)

        except psycopg2.Error as e:
            traceback.print_exc()
            return jsonify({"status": "error", "message": "Erro de banco de dados no decorator", "detail": str(e)}), 500
        except Exception as e:
            traceback.print_exc()
            return jsonify({"status": "error", "message": "Erro interno no decorator", "detail": str(e)}), 500
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

# --- Blueprint e Rotas ---
delivery_orders_bp = Blueprint('delivery_orders_bp', __name__)

# (O restante das suas rotas neste arquivo permanece o mesmo)
@delivery_orders_bp.route('/orders', methods=['GET'])
@delivery_token_required
def get_my_orders():
    # ... seu código da rota ...
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

# ... (inclua aqui o resto das suas rotas: get_order_details, accept_delivery, complete_delivery)
# Certifique-se de que todas as rotas que precisam de autenticação usem o decorator @delivery_token_required corrigido.
