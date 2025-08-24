# src/routes/auth.py

from flask import Blueprint, request, jsonify
import logging
from src.utils.helpers import get_db_connection, supabase, get_user_info

auth_bp = Blueprint('auth', __name__)
logger = logging.getLogger(__name__)

@auth_bp.route('/login', methods=['POST'])
def login():
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "Dados não fornecidos"}), 400
            
        email = data.get('email')
        password = data.get('password')
        
        if not email or not password:
            return jsonify({"error": "Email e senha são obrigatórios"}), 400

        # Autenticação com Supabase
        auth_response = supabase.auth.sign_in_with_password({
            "email": email,
            "password": password
        })
        
        user = auth_response.user
        session = auth_response.session
        
        if not user or not session:
            return jsonify({"error": "Falha na autenticação"}), 401
            
        # Buscar informações adicionais do usuário no banco local
        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Erro de conexão com o banco"}), 500
            
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, user_type, name, restaurant_id FROM users WHERE id = %s",
                    (str(user.id),)
                )
                user_data = cur.fetchone()
                
            if not user_data:
                return jsonify({"error": "Usuário não encontrado no sistema"}), 404
                
            user_id, user_type, name, restaurant_id = user_data
            
            return jsonify({
                "message": "Login realizado com sucesso",
                "user": {
                    "id": user_id,
                    "email": user.email,
                    "name": name,
                    "user_type": user_type,
                    "restaurant_id": restaurant_id
                },
                "session": {
                    "access_token": session.access_token,
                    "refresh_token": session.refresh_token,
                    "expires_at": session.expires_at
                }
            }), 200
                
        finally:
            conn.close()
            
    except Exception as e:
        logger.error(f"Erro no login: {str(e)}")
        return jsonify({"error": "Erro interno no servidor"}), 500

@auth_bp.route('/profile', methods=['GET'])
def handle_client_profile():
    """Obtém o perfil do usuário autenticado."""
    try:
        from src.utils.helpers import get_user_id_from_token
        
        # CORREÇÃO: A função retorna 4 valores, não 3
        user_id, user_type, error, status_code = get_user_id_from_token(request.headers.get('Authorization'))
        
        if error:
            return error, status_code
            
        # Buscar informações completas do usuário
        user_info = get_user_info(user_id)
        if not user_info:
            return jsonify({"error": "Usuário não encontrado"}), 404
            
        return jsonify({
            "user": {
                "id": user_info['id'],
                "email": user_info['email'],
                "name": user_info['name'],
                "user_type": user_info['user_type'],
                "restaurant_id": user_info['restaurant_id'],
                "created_at": user_info['created_at'].isoformat() if user_info['created_at'] else None
            }
        }), 200
            
    except Exception as e:
        logger.error(f"Erro ao obter perfil: {str(e)}")
        return jsonify({"error": "Erro interno ao obter perfil"}), 500

@auth_bp.route('/register', methods=['POST'])
def register():
    try:
        data = request.get_json()
        # ... implementação do registro
        return jsonify({"message": "Implementar registro"}), 200
    except Exception as e:
        logger.error(f"Erro no registro: {str(e)}")
        return jsonify({"error": "Erro interno no servidor"}), 500

# Outras rotas de auth...
