import os
import traceback
from flask import Blueprint, request, jsonify
import psycopg2.extras
from gotrue.errors import AuthApiError
from datetime import datetime, timedelta
from functools import wraps

from ..utils.helpers import get_db_connection, get_user_id_from_token, supabase
from ..utils.audit import log_admin_action

admin_bp = Blueprint('admin_bp', __name__, url_prefix='/admin')

# Decorador para verificar se o usuário é um administrador
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        auth_header = request.headers.get('Authorization')
        user_id, user_type, error_response = get_user_id_from_token(auth_header)
        
        if error_response:
            return error_response
        
        if user_type != 'admin':
            return jsonify({"status": "error", "message": "Acesso não autorizado. Rota exclusiva para administradores."}), 403
        
        return f(*args, **kwargs)
    return decorated_function

@admin_bp.route('/login', methods=['POST'])
def admin_login():
    data = request.get_json()
    email = data.get('email')
    password = data.get('password')

    if not email or not password:
        return jsonify({"status": "error", "message": "Email e senha são obrigatórios"}), 400

    try:
        response = supabase.auth.sign_in_with_password({"email": email, "password": password})
        user = response.user
        
        conn = get_db_connection()
        if not conn: return jsonify({"status": "error", "message": "Falha na conexão com a base de dados."}), 500

        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT user_type FROM users WHERE id = %s", (str(user.id),))
            db_user = cur.fetchone()
        
        if not db_user or db_user['user_type'] != 'admin':
            supabase.auth.sign_out()
            return jsonify({"status": "error", "message": "Acesso não permitido. Apenas para administradores."}), 403

        # Log successful admin login
        log_admin_action(user.email, "Login", f"Admin login successful", request)

        return jsonify({
            "status": "success", 
            "message": "Login de administrador bem-sucedido", 
            "access_token": response.session.access_token, 
            "data": { 
                "user": {
                    "id": user.id, 
                    "email": user.email, 
                    "user_type": db_user['user_type']
                } 
            }
        }), 200

    except AuthApiError:
        return jsonify({"status": "error", "message": "Credenciais inválidas"}), 401
    except Exception as e:
        traceback.print_exc()
        return jsonify({"status": "error", "message": f"Ocorreu um erro inesperado: {str(e)}"}), 500
    finally:
        if 'conn' in locals() and conn:
            conn.close()

@admin_bp.route('/logout', methods=['POST'])
@admin_required
def admin_logout():
    """Admin logout route with audit logging"""
    try:
        from ..utils.audit import log_admin_action_auto
        
        # Log logout action before signing out
        log_admin_action_auto("Logout", "Admin logout")
        
        # Sign out from Supabase
        supabase.auth.sign_out()
        
        return jsonify({
            "status": "success",
            "message": "Logout realizado com sucesso"
        }), 200
        
    except Exception as e:
        return jsonify({
            "status": "error", 
            "message": f"Erro durante logout: {str(e)}"
        }), 500

@admin_bp.route('/users', methods=['GET'])
@admin_required
def get_all_users():
    filter_user_type = request.args.get('user_type', None)
    filter_city = request.args.get('city', None)

    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Erro de conexão com o banco de dados"}), 500

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            params = []
            sql_query = """
                SELECT 
                    u.id, u.email, u.user_type, u.created_at,
                    COALESCE(
                        cp.first_name || ' ' || cp.last_name, 
                        rp.restaurant_name, 
                        dp.first_name || ' ' || dp.last_name
                    ) AS full_name,
                    COALESCE(cp.address_city, rp.address_city, dp.address_city) AS city
                FROM users u
                LEFT JOIN client_profiles cp ON u.id = cp.user_id AND u.user_type = 'client'
                LEFT JOIN restaurant_profiles rp ON u.id = rp.id AND u.user_type = 'restaurant'
                LEFT JOIN delivery_profiles dp ON u.id = dp.user_id AND u.user_type = 'delivery'
            """
            
            where_clauses = []
            if filter_user_type and filter_user_type.lower() != 'todos':
                where_clauses.append("u.user_type = %s")
                params.append(filter_user_type)
            if filter_city:
                where_clauses.append("COALESCE(cp.address_city, rp.address_city, dp.address_city) ILIKE %s")
                params.append(f'%{filter_city}%')
            if where_clauses:
                sql_query += " WHERE " + " AND ".join(where_clauses)

            sql_query += " ORDER BY u.created_at DESC;"
            cur.execute(sql_query, tuple(params))
            users = [dict(row) for row in cur.fetchall()]

        return jsonify({"status": "success", "data": users}), 200

    except Exception as e:
        traceback.print_exc()
        return jsonify({"status": "error", "message": "Erro interno ao buscar usuários.", "detail": str(e)}), 500
    finally:
        if conn:
            conn.close()

@admin_bp.route('/restaurants', methods=['GET'])
@admin_required
def get_all_restaurants():
    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Erro de conexão com o banco de dados"}), 500

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            sql_query = """
                SELECT rp.*, u.created_at
                FROM restaurant_profiles rp
                JOIN users u ON rp.id = u.id
                ORDER BY u.created_at DESC;
            """
            cur.execute(sql_query)
            restaurants = [dict(row) for row in cur.fetchall()]

        return jsonify({"status": "success", "data": restaurants}), 200

    except Exception as e:
        traceback.print_exc()
        return jsonify({"status": "error", "message": "Erro interno ao buscar restaurantes.", "detail": str(e)}), 500
    finally:
        if conn:
            conn.close()

@admin_bp.route('/kpi-summary', methods=['GET'])
@admin_required
def get_kpi_summary():
    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Erro de conexão com o banco de dados"}), 500

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            sql_query = """
                SELECT
                    (SELECT COALESCE(SUM(total_amount), 0) FROM orders WHERE status_pagamento = 'approved') AS totalRevenue,
                    (SELECT COALESCE(AVG(total_amount), 0) FROM orders WHERE status_pagamento = 'approved') AS averageTicket,
                    (SELECT COUNT(id) FROM orders WHERE DATE(created_at) = CURRENT_DATE) AS ordersToday,
                    (SELECT COUNT(id) FROM users WHERE user_type = 'client') AS totalClients,
                    (SELECT COUNT(id) FROM users WHERE user_type = 'client' AND DATE(created_at) = CURRENT_DATE) AS newClientsToday
            """
            cur.execute(sql_query)
            kpis = cur.fetchone()

        kpi_data = {
            "totalRevenue": float(kpis['totalrevenue']),
            "averageTicket": float(kpis['averageticket']),
            "ordersToday": kpis['orderstoday'],
            "totalClients": kpis['totalclients'],
            "newClientsToday": kpis['newclientstoday'],
        }
        
        return jsonify({"status": "success", "data": kpi_data}), 200

    except Exception as e:
        traceback.print_exc()
        return jsonify({"status": "error", "message": "Erro interno ao buscar KPIs.", "detail": str(e)}), 500
    finally:
        if conn:
            conn.close()

@admin_bp.route('/stats/revenue-chart', methods=['GET'])
@admin_required
def get_revenue_chart_data():
    conn = get_db_connection()
    if not conn: return jsonify({"status": "error", "message": "Erro de conexão com o banco de dados"}), 500

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            sql_query = """
                WITH last_7_days AS (
                    SELECT generate_series(
                        current_date - interval '6 days',
                        current_date,
                        '1 day'
                    )::date AS day
                )
                SELECT
                    to_char(d.day, 'DD/MM') AS formatted_date,
                    COALESCE(SUM(o.total_amount), 0) AS daily_revenue
                FROM last_7_days d
                LEFT JOIN orders o ON DATE(o.created_at) = d.day AND o.status_pagamento = 'approved'
                GROUP BY d.day
                ORDER BY d.day;
            """
            cur.execute(sql_query)
            chart_data = [dict(row) for row in cur.fetchall()]
        
        for item in chart_data:
            item['daily_revenue'] = float(item['daily_revenue'])

        return jsonify({"status": "success", "data": chart_data}), 200

    except Exception as e:
        traceback.print_exc()
        return jsonify({"status": "error", "message": "Erro interno ao buscar dados do gráfico.", "detail": str(e)}), 500
    finally:
        if conn: conn.close()

@admin_bp.route('/orders/recent', methods=['GET'])
@admin_required
def get_recent_orders():
    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Erro de conexão com o banco de dados"}), 500

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            sql_query = """
                SELECT 
                    o.id,
                    o.total_amount,
                    o.status,
                    o.created_at,
                    COALESCE(cp.first_name || ' ' || cp.last_name, 'Cliente Anônimo') AS client_name,
                    COALESCE(rp.restaurant_name, 'Restaurante Desconhecido') AS restaurant_name
                FROM orders o
                LEFT JOIN client_profiles cp ON o.client_id = cp.user_id
                LEFT JOIN restaurant_profiles rp ON o.restaurant_id = rp.id
                ORDER BY o.created_at DESC
                LIMIT 5;
            """
            cur.execute(sql_query)
            recent_orders = [dict(row) for row in cur.fetchall()]

        for order in recent_orders:
            order['total_amount'] = float(order['total_amount'])
            order['created_at'] = order['created_at'].isoformat()

        return jsonify({"status": "success", "data": recent_orders}), 200
        
    except Exception as e:
        traceback.print_exc()
        return jsonify({"status": "error", "message": "Erro interno ao buscar pedidos recentes.", "detail": str(e)}), 500
    finally:
        if conn:
            conn.close()

# Endpoint para ATUALIZAR os dados de um restaurante
@admin_bp.route('/restaurants/<uuid:restaurant_id>', methods=['PUT'])
@admin_required
def update_restaurant(restaurant_id):
    data = request.get_json()
    if not data:
        return jsonify({"status": "error", "message": "Nenhum dado enviado para atualização."}), 400

    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Erro de conexão com o banco de dados"}), 500

    try:
        with conn.cursor() as cur:
            set_parts = []
            values = []
            for key, value in data.items():
                if key.isalnum():
                    set_parts.append(f"{key} = %s")
                    values.append(value)

            if not set_parts:
                return jsonify({"status": "error", "message": "Nenhum campo válido para atualização."}), 400

            values.append(str(restaurant_id))
            
            sql_query = f"""
                UPDATE restaurant_profiles
                SET {', '.join(set_parts)}
                WHERE id = %s;
            """
            
            cur.execute(sql_query, tuple(values))

            if cur.rowcount == 0:
                return jsonify({"status": "error", "message": "Restaurante não encontrado."}), 404

            conn.commit()

            # Log restaurant update action
            from ..utils.audit import log_admin_action_auto
            restaurant_fields = ', '.join(data.keys())
            log_admin_action_auto("UpdateRestaurant", f"Updated restaurant {restaurant_id} fields: {restaurant_fields}")

        return jsonify({"status": "success", "message": "Restaurante atualizado com sucesso."}), 200

    except Exception as e:
        if conn:
            conn.rollback() 
        traceback.print_exc()
        return jsonify({"status": "error", "message": "Erro interno ao atualizar o restaurante.", "detail": str(e)}), 500
    finally:
        if conn:
            conn.close()

# NOVO ENDPOINT: DASHBOARD CONSOLIDADO
@admin_bp.route('/dashboard', methods=['GET'])
@admin_required
def get_dashboard():
    """
    Retorna todos os dados necessários para o dashboard admin em uma única resposta:
    KPIs, gráfico de faturamento e pedidos recentes.
    """
    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Erro de conexão com o banco de dados"}), 500
    
    try:
        # KPIs
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                SELECT
                    (SELECT COALESCE(SUM(total_amount), 0) FROM orders WHERE status_pagamento = 'approved') AS totalRevenue,
                    (SELECT COALESCE(AVG(total_amount), 0) FROM orders WHERE status_pagamento = 'approved') AS averageTicket,
                    (SELECT COUNT(id) FROM orders WHERE DATE(created_at) = CURRENT_DATE) AS ordersToday,
                    (SELECT COUNT(id) FROM users WHERE user_type = 'client') AS totalClients,
                    (SELECT COUNT(id) FROM users WHERE user_type = 'client' AND DATE(created_at) = CURRENT_DATE) AS newClientsToday
            """)
            kpis_row = cur.fetchone()
            kpis = {
                "totalRevenue": float(kpis_row['totalrevenue']),
                "averageTicket": float(kpis_row['averageticket']),
                "ordersToday": kpis_row['orderstoday'],
                "totalClients": kpis_row['totalclients'],
                "newClientsToday": kpis_row['newclientstoday'],
            }

        # Gráfico de faturamento (últimos 7 dias)
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                WITH last_7_days AS (
                    SELECT generate_series(
                        current_date - interval '6 days',
                        current_date,
                        '1 day'
                    )::date AS day
                )
                SELECT
                    to_char(d.day, 'DD/MM') AS formatted_date,
                    COALESCE(SUM(o.total_amount), 0) AS daily_revenue
                FROM last_7_days d
                LEFT JOIN orders o ON DATE(o.created_at) = d.day AND o.status_pagamento = 'approved'
                GROUP BY d.day
                ORDER BY d.day;
            """)
            chart_data = [dict(row) for row in cur.fetchall()]
            for item in chart_data:
                item['daily_revenue'] = float(item['daily_revenue'])

        # Pedidos recentes
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                SELECT 
                    o.id,
                    o.total_amount,
                    o.status,
                    o.created_at,
                    COALESCE(cp.first_name || ' ' || cp.last_name, 'Cliente Anônimo') AS client_name,
                    COALESCE(rp.restaurant_name, 'Restaurante Desconhecido') AS restaurant_name
                FROM orders o
                LEFT JOIN client_profiles cp ON o.client_id = cp.user_id
                LEFT JOIN restaurant_profiles rp ON o.restaurant_id = rp.id
                ORDER BY o.created_at DESC
                LIMIT 5;
            """)
            recent_orders = [dict(row) for row in cur.fetchall()]
            for order in recent_orders:
                order['total_amount'] = float(order['total_amount'])
                order['created_at'] = order['created_at'].isoformat()

        # Monta resposta unificada
        return jsonify({
            "status": "success",
            "kpis": kpis,
            "chartData": chart_data,
            "recentOrders": recent_orders
        }), 200

    except Exception as e:
        traceback.print_exc()
        return jsonify({"status": "error", "message": "Erro interno ao buscar dados do dashboard.", "detail": str(e)}), 500
    finally:
        if conn:
            conn.close()
