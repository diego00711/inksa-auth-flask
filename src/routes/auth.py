from flask import Blueprint, request, jsonify
import logging
from src.utils.helpers import get_db_connection, supabase, get_user_info
from src.utils.audit import log_admin_action

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

        # Autentica com Supabase
        auth_response = supabase.auth.sign_in_with_password({
            "email": email,
            "password": password
        })

        user = auth_response.user
        session = auth_response.session

        if not user or not session:
            return jsonify({"error": "Falha na autenticação"}), 401

        # Busca dados adicionais do usuário na tabela personalizada
        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Erro de conexão com o banco"}), 500

        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, user_type, email, created_at FROM public.users WHERE id = %s",
                    (str(user.id),)
                )
                user_data = cur.fetchone()

            if not user_data:
                return jsonify({"error": "Usuário não encontrado no sistema"}), 404

            user_id, user_type, user_email, created_at = user_data

            # Log admin logins (for non-admin logins, we skip audit logging)
            if user_type == 'admin':
                log_admin_action(user_email, "Login", f"Admin login via client endpoint", request)

            return jsonify({
                "message": "Login realizado com sucesso",
                "token": session.access_token,  # <-- esse campo é ESSENCIAL para o frontend
                "refresh_token": session.refresh_token,
                "user": {
                    "id": user_id,
                    "email": user_email,
                    "name": user_email.split('@')[0],
                    "user_type": user_type,
                    "created_at": created_at.isoformat() if created_at else None
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

        # Ajuste caso sua função retorne mais de 3 valores
        user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))

        if error:
            return error

        user_info = get_user_info(user_id)
        if not user_info:
            return jsonify({"error": "Usuário não encontrado"}), 404

        return jsonify({
            "user": {
                "id": user_info['id'],
                "email": user_info['email'],
                "name": user_info['email'].split('@')[0],
                "user_type": user_info['user_type'],
                "created_at": user_info['created_at'].isoformat() if user_info.get('created_at') else None
            }
        }), 200

    except Exception as e:
        logger.error(f"Erro ao obter perfil: {str(e)}")
        return jsonify({"error": "Erro interno ao obter perfil"}), 500

@auth_bp.route('/register', methods=['POST'])
def register():
    try:
        data = request.get_json()
        # ... implementação do registro ...
        return jsonify({"message": "Implementar registro"}), 200
    except Exception as e:
        logger.error(f"Erro no registro: {str(e)}")
        return jsonify({"error": "Erro interno no servidor"}), 500

# Outras rotas de autenticação podem ser adicionadas aqui...
