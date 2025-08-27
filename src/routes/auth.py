from flask import Blueprint, request, jsonify
import logging
from src.utils.helpers import get_db_connection, supabase
from src.utils.audit import log_admin_action

# Este blueprint cuida somente de autenticação (login/registro).
# O endpoint de perfil do cliente está em src/routes/client.py (GET/PUT /api/auth/profile).
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

            if user_type == 'admin':
                log_admin_action(user_email, "Login", "Admin login via client endpoint", request)

            return jsonify({
                "message": "Login realizado com sucesso",
                "token": session.access_token,
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
        logger.error(f"Erro no login: {str(e)}", exc_info=True)
        return jsonify({"error": "Erro interno no servidor"}), 500

@auth_bp.route('/register', methods=['POST'])
def register():
    try:
        data = request.get_json() or {}
        # TODO: implementar registro
        return jsonify({"message": "Implementar registro"}), 200
    except Exception as e:
        logger.error(f"Erro no registro: {str(e)}", exc_info=True)
        return jsonify({"error": "Erro interno no servidor"}), 500
