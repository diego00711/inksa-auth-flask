# src/routes/auth.py
# VERSÃO COMPLETA E CORRIGIDA

import logging
import psycopg2
import psycopg2.extras
from datetime import datetime, date
from flask import Blueprint, request, jsonify

# Importações de utilitários (assumindo que estão corretas no seu projeto)
from src.utils.helpers import get_db_connection, supabase, get_user_id_from_token
from src.utils.audit import log_admin_action

# Configuração do Blueprint e Logger
auth_bp = Blueprint('auth', __name__)
logger = logging.getLogger(__name__)

@auth_bp.route('/login', methods=['POST'])
def login():
    """
    Realiza o login de um usuário (qualquer tipo) e retorna o token e dados do perfil.
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "Dados não fornecidos"}), 400

        email = data.get('email')
        password = data.get('password')

        if not email or not password:
            return jsonify({"error": "Email e senha são obrigatórios"}), 400

        # Autentica com o Supabase
        auth_response = supabase.auth.sign_in_with_password({
            "email": email,
            "password": password
        })

        user = auth_response.user
        session = auth_response.session

        if not user or not session:
            return jsonify({"error": "Falha na autenticação"}), 401

        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Erro de conexão com o banco de dados"}), 500

        try:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                # Busca os dados do usuário na tabela 'users' do seu banco
                cur.execute(
                    "SELECT id, user_type, email, created_at FROM public.users WHERE id = %s",
                    (str(user.id),)
                )
                user_data = cur.fetchone()

            if not user_data:
                return jsonify({"error": "Usuário não encontrado no sistema"}), 404

            user_id, user_type, user_email, created_at = user_data

            # Log de auditoria se um admin fizer login por esta rota
            if user_type == 'admin':
                log_admin_action(user_email, "Login", "Admin login via client endpoint", request)

            # Retorna a resposta de sucesso com todos os dados necessários para o frontend
            return jsonify({
                "message": "Login realizado com sucesso",
                "token": session.access_token,
                "refresh_token": session.refresh_token,
                "user": {
                    "id": user_id,
                    "email": user_email,
                    "name": user_email.split('@')[0], # Nome padrão
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
            if conn:
                conn.close()

    except Exception as e:
        logger.error(f"Erro no login: {str(e)}", exc_info=True)
        # Em caso de credenciais erradas, o Supabase levanta uma exceção.
        # É uma boa prática retornar um erro 401 genérico.
        return jsonify({"error": "Credenciais inválidas ou erro interno"}), 401

@auth_bp.route('/register', methods=['POST'])
def register():
    """
    Rota para registro de novos usuários. (Atualmente como placeholder).
    """
    try:
        data = request.get_json() or {}
        # TODO: Implementar a lógica de registro de novos usuários.
        return jsonify({"message": "Rota de registro a ser implementada"}), 501 # 501 Not Implemented
    except Exception as e:
        logger.error(f"Erro no registro: {str(e)}", exc_info=True)
        return jsonify({"error": "Erro interno no servidor"}), 500

@auth_bp.route('/profile', methods=['GET', 'OPTIONS'])
def get_profile():
    """
    Endpoint universal e CORRIGIDO para obter o perfil do usuário autenticado.
    Suporta todos os tipos de usuários (cliente, restaurante, entregador, admin).
    """
    logger.info(f"Endpoint /profile acessado. Método: {request.method}")
    
    if request.method == 'OPTIONS':
        return '', 200
        
    try:
        # 1. Verificação de autenticação
        auth_header = request.headers.get('Authorization')
        if not auth_header:
            logger.warning("Tentativa de acesso sem token de autenticação")
            return jsonify({"error": "Token de autorização não fornecido"}), 401
            
        # 2. Obter ID e tipo do usuário do token
        user_id, user_type, error = get_user_id_from_token(auth_header)
        if error:
            # O helper já retorna um jsonify, então apenas o retornamos
            return error
        
        logger.info(f"Buscando perfil para usuário {user_id} (tipo: {user_type})")
        
        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Erro de conexão com o banco de dados"}), 500
            
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
                
                # 3. Buscar dados básicos do usuário da tabela 'users'
                cursor.execute("SELECT id, email, user_type, created_at FROM users WHERE id = %s", (user_id,))
                user_data = cursor.fetchone()
                
                if not user_data:
                    logger.warning(f"Usuário {user_id} (do token) não encontrado no banco de dados")
                    return jsonify({"error": "Usuário não encontrado"}), 404
                    
                profile_data = dict(user_data)
                profile_data['name'] = profile_data['email'].split('@')[0]
                
                # 4. Buscar dados específicos do perfil correspondente
                specific_profile = None
                if user_type == 'client':
                    cursor.execute("SELECT * FROM client_profiles WHERE user_id = %s", (user_id,))
                    specific_profile = cursor.fetchone()
                
                elif user_type == 'delivery':
                    cursor.execute("SELECT * FROM delivery_profiles WHERE user_id = %s", (user_id,))
                    specific_profile = cursor.fetchone()
                
                elif user_type == 'restaurant':
                    # Para restaurantes, o ID do perfil é o mesmo ID do usuário.
                    cursor.execute("SELECT * FROM restaurant_profiles WHERE user_id = %s", (user_id,))
                    specific_profile = cursor.fetchone()
                
                # 5. Unir os dados do perfil específico aos dados básicos do usuário
                if specific_profile:
                    for key, value in dict(specific_profile).items():
                        if key not in profile_data: # Evita sobrescrever 'id', 'user_id', etc.
                            profile_data[key] = value

                # 6. Converter datas e outros tipos para formatos compatíveis com JSON
                for key, value in profile_data.items():
                    if isinstance(value, (datetime, date)):
                        profile_data[key] = value.isoformat()
                
                logger.info(f"Perfil para usuário {user_id} recuperado com sucesso.")
                return jsonify(profile_data), 200
            
        finally:
            if conn:
                conn.close()
            
    except Exception as e:
        logger.error(f"Erro crítico ao buscar perfil: {str(e)}", exc_info=True)
        return jsonify({"error": "Erro interno no servidor ao processar o perfil"}), 500

