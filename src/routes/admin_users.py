import os
import traceback
from flask import Blueprint, request, jsonify
from flask_cors import CORS
import psycopg2.extras
from datetime import datetime
from functools import wraps

from ..utils.helpers import get_db_connection, get_user_id_from_token
from ..utils.audit import log_admin_action_auto

# Create blueprint for admin users API endpoints
admin_users_bp = Blueprint('admin_users_bp', __name__)

# Aplica o CORS diretamente a este blueprint, permitindo a URL específica da Vercel.
CORS(admin_users_bp, origins=["https://inksa-admin-v0-q4yqjmgnt-inksas-projects.vercel.app"], supports_credentials=True )

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

def get_user_status(user_data):
    """
    Determine user status based on profile data and user_type.
    """
    if user_data.get('full_name') and user_data.get('full_name').strip():
        return 'active'
    else:
        return 'inactive'

@admin_users_bp.route('/api/users', methods=['GET'])
@admin_required
def list_users():
    """
    List users with pagination and filtering support.
    """
    page = max(1, int(request.args.get('page', 1)))
    page_size = min(100, max(1, int(request.args.get('page_size', 20))))
    query = request.args.get('query', '').strip()
    status_filter = request.args.get('status', 'all').lower()
    role_filter = request.args.get('role', '').strip()
    sort_param = request.args.get('sort', 'created_at:desc').strip()
    
    if ':' in sort_param:
        sort_field, sort_direction = sort_param.split(':', 1)
        sort_direction = sort_direction.upper()
        if sort_direction not in ['ASC', 'DESC']:
            sort_direction = 'DESC'
    else:
        sort_field = sort_param
        sort_direction = 'DESC'
    
    allowed_sort_fields = ['created_at', 'email', 'user_type', 'full_name']
    if sort_field not in allowed_sort_fields:
        sort_field = 'created_at'
    
    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Erro de conexão com o banco de dados"}), 500

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            base_query = """
                SELECT 
                    u.id, u.email, u.user_type, u.created_at,
                    COALESCE(
                        cp.first_name || ' ' || cp.last_name, 
                        rp.restaurant_name, 
                        dp.first_name || ' ' || dp.last_name
                    ) AS full_name,
                    COALESCE(cp.address_city, rp.address_city, dp.address_city) AS city,
                    COALESCE(cp.phone, rp.phone, dp.phone) AS phone
                FROM users u
                LEFT JOIN client_profiles cp ON u.id = cp.user_id AND u.user_type = 'client'
                LEFT JOIN restaurant_profiles rp ON u.id = rp.id AND u.user_type = 'restaurant'
                LEFT JOIN delivery_profiles dp ON u.id = dp.user_id AND u.user_type = 'delivery'
            """
            
            where_clauses = []
            params = []
            
            if role_filter:
                where_clauses.append("u.user_type = %s")
                params.append(role_filter)
            
            if query:
                where_clauses.append("""
                    (u.email ILIKE %s OR 
                     COALESCE(
                        cp.first_name || ' ' || cp.last_name, 
                        rp.restaurant_name, 
                        dp.first_name || ' ' || dp.last_name
                     ) ILIKE %s)
                """)
                query_param = f'%{query}%'
                params.extend([query_param, query_param])
            
            where_sql = ""
            if where_clauses:
                where_sql = " WHERE " + " AND ".join(where_clauses)
            
            count_query = f"SELECT COUNT(DISTINCT u.id) as total FROM users u LEFT JOIN client_profiles cp ON u.id = cp.user_id AND u.user_type = 'client' LEFT JOIN restaurant_profiles rp ON u.id = rp.id AND u.user_type = 'restaurant' LEFT JOIN delivery_profiles dp ON u.id = dp.user_id AND u.user_type = 'delivery' {where_sql}"
            cur.execute(count_query, tuple(params))
            total_count = cur.fetchone()['total']
            
            final_query = f"{base_query} {where_sql} ORDER BY {sort_field} {sort_direction} LIMIT %s OFFSET %s"
            offset = (page - 1) * page_size
            cur.execute(final_query, tuple(params + [page_size, offset]))
            users = [dict(row) for row in cur.fetchall()]
            
            filtered_users = []
            for user in users:
                user['status'] = get_user_status(user)
                if status_filter == 'all' or user['status'] == status_filter:
                    filtered_users.append(user)
            
            if status_filter != 'all':
                total_count = len(filtered_users)
            
            response = {
                "items": filtered_users,
                "total": total_count,
                "page": page,
                "page_size": page_size
            }
            
            return jsonify(response), 200

    except Exception as e:
        traceback.print_exc()
        return jsonify({"status": "error", "message": "Erro interno ao buscar usuários.", "detail": str(e)}), 500
    finally:
        if conn:
            conn.close()

@admin_users_bp.route('/api/users/<uuid:user_id>', methods=['GET'])
@admin_required
def get_user_detail(user_id):
    """
    Get detailed information about a specific user.
    """
    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Erro de conexão com o banco de dados"}), 500

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            query = """
                SELECT 
                    u.id, u.email, u.user_type, u.created_at,
                    COALESCE(
                        cp.first_name || ' ' || cp.last_name, 
                        rp.restaurant_name, 
                        dp.first_name || ' ' || dp.last_name
                    ) AS full_name,
                    COALESCE(cp.address_city, rp.address_city, dp.address_city) AS city,
                    COALESCE(cp.phone, rp.phone, dp.phone) AS phone,
                    cp.first_name, cp.last_name, cp.cpf,
                    cp.address_street, cp.address_number, cp.address_neighborhood,
                    cp.address_city as client_city, cp.address_state, cp.address_zipcode,
                    rp.restaurant_name, rp.business_name, rp.cnpj,
                    rp.address_street as rest_address_street, rp.address_number as rest_address_number,
                    rp.address_neighborhood as rest_address_neighborhood, rp.address_city as rest_address_city,
                    rp.address_state as rest_address_state, rp.address_zipcode as rest_address_zipcode,
                    dp.first_name as delivery_first_name, dp.last_name as delivery_last_name,
                    dp.cpf as delivery_cpf, dp.birth_date, dp.vehicle_type
                FROM users u
                LEFT JOIN client_profiles cp ON u.id = cp.user_id AND u.user_type = 'client'
                LEFT JOIN restaurant_profiles rp ON u.id = rp.id AND u.user_type = 'restaurant'
                LEFT JOIN delivery_profiles dp ON u.id = dp.user_id AND u.user_type = 'delivery'
                WHERE u.id = %s
            """
            
            cur.execute(query, (str(user_id),))
            user = cur.fetchone()
            
            if not user:
                return jsonify({"status": "error", "message": "Usuário não encontrado"}), 404
            
            user_dict = dict(user)
            user_dict['status'] = get_user_status(user_dict)
            
            return jsonify({"status": "success", "data": user_dict}), 200

    except Exception as e:
        traceback.print_exc()
        return jsonify({"status": "error", "message": "Erro interno ao buscar usuário.", "detail": str(e)}), 500
    finally:
        if conn:
            conn.close()

@admin_users_bp.route('/api/users/<uuid:user_id>', methods=['PATCH'])
@admin_required  
def update_user(user_id):
    """
    Partial update for user status/role.
    """
    data = request.get_json()
    if not data:
        return jsonify({"status": "error", "message": "Nenhum dado enviado para atualização."}), 400

    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Erro de conexão com o banco de dados"}), 500

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT id, user_type, email FROM users WHERE id = %s", (str(user_id),))
            user = cur.fetchone()
            
            if not user:
                return jsonify({"status": "error", "message": "Usuário não encontrado"}), 404
            
            updates = []
            params = []
            update_details = []
            
            if 'user_type' in data:
                new_user_type = data['user_type']
                valid_types = ['client', 'restaurant', 'delivery', 'admin']
                
                if new_user_type not in valid_types:
                    return jsonify({"status": "error", "message": f"Tipo de usuário inválido. Deve ser um de: {', '.join(valid_types)}"}), 400
                
                updates.append("user_type = %s")
                params.append(new_user_type)
                update_details.append(f"user_type: {user['user_type']} -> {new_user_type}")
            
            if 'status' in data:
                new_status = data['status']
                if new_status not in ['active', 'inactive']:
                    return jsonify({"status": "error", "message": "Status inválido. Deve ser 'active' ou 'inactive'."}), 400
                update_details.append(f"status: -> {new_status}")
            
            if updates:
                params.append(str(user_id))
                update_query = f"UPDATE users SET {', '.join(updates)} WHERE id = %s"
                cur.execute(update_query, tuple(params))
                
                if cur.rowcount == 0:
                    conn.rollback()
                    return jsonify({"status": "error", "message": "Falha ao atualizar usuário"}), 500
                
                conn.commit()
            
            if update_details:
                log_admin_action_auto(
                    "UpdateUser", 
                    f"Updated user {user['email']} (ID: {user_id}): {', '.join(update_details)}"
                )
            
            return jsonify({"status": "success", "message": "Usuário atualizado com sucesso."}), 200

    except Exception as e:
        if conn:
            conn.rollback()
        traceback.print_exc()
        return jsonify({"status": "error", "message": "Erro interno ao atualizar usuário.", "detail": str(e)}), 500
    finally:
        if conn:
            conn.close()
