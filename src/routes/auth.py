# src/routes/auth.py - VERSÃO FINAL COM A ROTA DE PERFIL MOVIDA

import os
import traceback
import logging
from flask import Blueprint, request, jsonify
import psycopg2
import psycopg2.extras
from datetime import datetime, date

from ..utils.helpers import get_db_connection, get_user_id_from_token, supabase

auth_bp = Blueprint('auth_bp', __name__)
logger = logging.getLogger(__name__)

@auth_bp.route('/login', methods=['POST'])
def login():
    try:
        data = request.get_json()
        if not data or 'email' not in data or 'password' not in data:
            return jsonify({"error": "Email e senha são obrigatórios"}), 400

        email, password = data.get('email'), data.get('password')
        auth_response = supabase.auth.sign_in_with_password({"email": email, "password": password})
        
        user, session = auth_response.user, auth_response.session
        if not user or not session:
            return jsonify({"error": "Falha na autenticação"}), 401

        return jsonify({
            "message": "Login realizado com sucesso",
            "token": session.access_token,
            "user": { "id": user.id, "email": user.email }
        }), 200
    except Exception as e:
        logger.error(f"Erro no login: {str(e)}", exc_info=True)
        return jsonify({"error": "Credenciais inválidas ou erro interno"}), 401

@auth_bp.route('/profile', methods=['GET'])
def handle_profile():
    logger.info('[auth.py] handle_profile chamado')
    user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
    if error:
        return error

    table_map = {
        'client': 'client_profiles',
        'restaurant': 'restaurant_profiles',
        'delivery': 'delivery_profiles'
    }
    table_name = table_map.get(user_type)
    if not table_name:
        # Para admin ou outros tipos, retorna dados básicos
        return jsonify({"id": user_id, "user_type": user_type})

    id_column = 'user_id' if user_type != 'restaurant' else 'user_id' # Corrigido para sempre usar user_id

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Erro de conexão com o banco de dados"}), 500
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # Une com a tabela de usuários para pegar o email
            cur.execute(f"""
                SELECT p.*, u.email 
                FROM {table_name} p
                JOIN users u ON p.user_id = u.id
                WHERE p.{id_column} = %s
            """, (user_id,))
            profile_raw = cur.fetchone()

        if not profile_raw:
            return jsonify({"error": f"Perfil de {user_type} não encontrado"}), 404
        
        profile_dict = dict(profile_raw)
        for key, value in profile_dict.items():
            if isinstance(value, (datetime, date)):
                profile_dict[key] = value.isoformat()

        return jsonify(profile_dict), 200
    except Exception as e:
        logger.exception(f'Erro em handle_profile para user_type {user_type}')
        return jsonify({"error": "Erro interno no servidor."}), 500
    finally:
        if conn: conn.close()
