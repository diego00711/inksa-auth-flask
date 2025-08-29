# src/routes/auth.py - VERSÃO FINAL, SIMPLIFICADA E CORRIGIDA

import logging
from flask import Blueprint, request, jsonify
from ..utils.helpers import get_db_connection, get_user_id_from_token, supabase

auth_bp = Blueprint('auth_bp', __name__)
logger = logging.getLogger(__name__)

@auth_bp.route('/login', methods=['POST'])
def login():
    try:
        data = request.get_json()
        if not data or 'email' not in data or 'password' not in data:
            return jsonify({"status": "error", "error": "Email e senha são obrigatórios"}), 400

        email, password = data.get('email'), data.get('password')
        auth_response = supabase.auth.sign_in_with_password({"email": email, "password": password})
        
        user, session = auth_response.user, auth_response.session
        if not user or not session:
            return jsonify({"status": "error", "error": "Falha na autenticação"}), 401

        # A resposta agora inclui o user_type, que é crucial para o front-end.
        user_metadata = user.user_metadata or {}
        user_type = user_metadata.get('user_type', 'unknown')

        return jsonify({
            "status": "success",
            "data": {
                "message": "Login realizado com sucesso",
                "token": session.access_token,
                "user": { 
                    "id": user.id, 
                    "email": user.email,
                    "user_type": user_type 
                }
            }
        }), 200
    except Exception as e:
        logger.error(f"Erro no login: {str(e)}", exc_info=True)
        # Retorna o erro específico do Supabase se disponível
        error_message = str(e.args[0]) if e.args else "Credenciais inválidas ou erro interno"
        return jsonify({"status": "error", "error": error_message}), 401

# ✅ REMOVIDO: A rota /profile foi removida deste arquivo.
# A responsabilidade de buscar o perfil agora é dos blueprints específicos
# (client_bp, restaurant_bp, etc.), o que elimina o conflito.

