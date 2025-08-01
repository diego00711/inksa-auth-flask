# src/routes/auth.py

import os
import traceback
import uuid
from flask import Blueprint, request, jsonify
import psycopg2
import psycopg2.extras
from gotrue.errors import AuthApiError
from ..utils.helpers import get_db_connection, get_user_id_from_token, supabase

auth_bp = Blueprint('auth_bp', __name__)

# --- ROTAS DE AUTENTICAÇÃO ---

@auth_bp.route('/auth/register', methods=['POST'])
def register():
    data = request.get_json()
    if not data: return jsonify({"status": "error", "error": "Nenhum dado fornecido"}), 400
    email = data.get('email')
    password = data.get('password')
    full_name = data.get('name')
    user_type = data.get('userType')
    profile_data = data.get('profileData', {})
    if not all([email, password, full_name, user_type]):
        return jsonify({"status": "error", "error": "Dados incompletos para o registo."}), 400
    user_id = None
    conn = get_db_connection()
    if not conn: return jsonify({"status": "error", "error": "Falha na conexão com a base de dados."}), 500
    try:
        user_response = supabase.auth.sign_up({
            "email": email, "password": password,
            "options": { "data": { "full_name": full_name, "user_type": user_type } }
        })
        user = user_response.user
        if not user: raise Exception("Falha ao criar utilizador no Supabase Auth.")
        user_id = user.id
        with conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO users (id, email, user_type) VALUES (%s, %s, %s)", (str(user_id), email, user_type))
                if user_type == 'client':
                    cur.execute("INSERT INTO client_profiles (user_id, first_name) VALUES (%s, %s)", (str(user_id), full_name.split(' ')[0]))
                elif user_type == 'restaurant':
                    restaurant_name = profile_data.get('restaurantName')
                    if not restaurant_name: raise ValueError("Nome do restaurante é obrigatório.")
                    profile_to_insert = { 'id': str(user_id), 'restaurant_name': restaurant_name }
                    columns = ', '.join(profile_to_insert.keys())
                    placeholders = ', '.join(['%s'] * len(profile_to_insert))
                    cur.execute(f"INSERT INTO restaurant_profiles ({columns}) VALUES ({placeholders})", tuple(profile_to_insert.values()))
        return jsonify({"status": "success", "message": f"Conta de {user_type} criada com sucesso!"}), 201
    except (AuthApiError, ValueError, psycopg2.Error) as e:
        if user_id:
            try: supabase.auth.admin.delete_user(user_id)
            except Exception as delete_e: print(f"AVISO: Falha ao reverter criação do utilizador no Auth: {delete_e}")
        error_message = getattr(e, 'message', str(e))
        if "User already registered" in error_message or "duplicate key value" in error_message:
            return jsonify({"status": "error", "error": "Este e-mail já está em uso."}), 409
        traceback.print_exc()
        return jsonify({"status": "error", "error": "Ocorreu uma falha interna.", "detail": error_message}), 500

@auth_bp.route('/auth/login', methods=['POST'])
def login():
    data = request.get_json()
    email = data.get('email'); password = data.get('password')
    user_type_req = data.get('user_type')
    if not all([email, password, user_type_req]):
        return jsonify({"status": "error", "error": "Dados de login incompletos"}), 400
    try:
        response = supabase.auth.sign_in_with_password({"email": email, "password": password})
        user = response.user
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute("SELECT user_type FROM users WHERE id = %s", (str(user.id),))
                db_user = cur.fetchone()
        if not db_user or db_user['user_type'] != user_type_req:
            return jsonify({"status": "error", "error": "Acesso não permitido para este tipo de utilizador."}), 403
        return jsonify({
            "status": "success", "message": "Login bem-sucedido", 
            "access_token": response.session.access_token, 
            "data": { "user": {"id": user.id, "email": user.email, "user_type": db_user['user_type']} }
        }), 200
    except AuthApiError: return jsonify({"status": "error", "error": "Credenciais inválidas"}), 401
    except Exception as e:
        traceback.print_exc()
        return jsonify({"status": "error", "error": "Ocorreu um erro inesperado."}), 500

# --- ROTAS DE PERFIL DO CLIENTE ---

@auth_bp.route('/auth/profile', methods=['GET', 'PUT'])
def handle_client_profile():
    user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
    if error: return error
    if user_type != 'client': return jsonify({"error": "Acesso não autorizado a este perfil"}), 403
    conn = get_db_connection()
    if not conn: return jsonify({"error": "Erro de conexão com o banco de dados"}), 500
    try:
        if request.method == 'GET':
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute("SELECT * FROM client_profiles WHERE user_id = %s", (user_id,))
                profile_raw = cur.fetchone()
            if not profile_raw: return jsonify({"error": "Perfil de cliente não encontrado"}), 404
            return jsonify({"status": "success", "data": dict(profile_raw)})
        if request.method == 'PUT':
            data = request.get_json()
            if not data: return jsonify({"error": "Nenhum dado fornecido"}), 400
            if 'birth_date' in data and not data['birth_date']:
                data['birth_date'] = None
            allowed_fields = [
                'first_name', 'last_name', 'phone', 'birth_date', 'cpf',
                'address_zipcode', 'address_street', 'address_number',
                'address_complement', 'address_neighborhood', 'address_city', 'address_state'
            ]
            update_fields = [f"{field} = %s" for field in allowed_fields if field in data]
            if not update_fields: return jsonify({"error": "Nenhum campo válido para atualizar"}), 400
            update_values = [data[field] for field in allowed_fields if field in data]
            sql = f"UPDATE client_profiles SET {', '.join(update_fields)} WHERE user_id = %s RETURNING *"
            update_values.append(user_id)
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute(sql, tuple(update_values))
                updated_profile = cur.fetchone()
                conn.commit()
            if updated_profile:
                return jsonify({"status": "success", "data": dict(updated_profile)})
            else:
                return jsonify({"error": "Perfil não encontrado para atualizar"}), 404
    except Exception as e:
        if conn: conn.rollback()
        traceback.print_exc()
        return jsonify({"error": "Erro interno no servidor.", "detail": str(e)}), 500
    finally:
        if conn: conn.close()

# ✅ CORREÇÃO: Função de upload de avatar corrigida para ser mais flexível
@auth_bp.route('/auth/avatar', methods=['POST'])
def upload_avatar():
    user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
    if error: return error
    if user_type != 'client': return jsonify({"error": "Acesso não autorizado"}), 403

    # Procura pelo ficheiro em chaves comuns ('file', 'avatar')
    if 'file' in request.files:
        file = request.files['file']
    elif 'avatar' in request.files:
        file = request.files['avatar']
    else:
        return jsonify({"error": "Nenhum ficheiro enviado com a chave correta ('file' ou 'avatar')."}), 400
    
    if file.filename == '':
        return jsonify({"error": "Nome de ficheiro vazio"}), 400

    try:
        file_ext = os.path.splitext(file.filename)[1]
        unique_filename = f"avatar_{user_id}{file_ext}"
        
        supabase.storage.from_("avatars").upload(
            file=file.read(),
            path=unique_filename,
            file_options={"content-type": file.mimetype, "upsert": "true"}
        )
        
        public_url = supabase.storage.from_("avatars").get_public_url(unique_filename)
        
        supabase.table('client_profiles').update({'avatar_url': public_url}).eq('user_id', user_id).execute()

        return jsonify({"avatar_url": public_url}), 200
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500