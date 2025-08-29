from flask import Blueprint, request, jsonify
import logging
import psycopg2
import psycopg2.extras
from src.utils.helpers import get_db_connection, supabase, get_user_id_from_token
from src.utils.audit import log_admin_action

# Mantemos este blueprint apenas para login/registro.
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

@auth_bp.route('/profile', methods=['GET', 'OPTIONS'])
def get_profile():
    """
    Endpoint universal para obter perfis de usuários.
    
    Suporta todos os tipos de usuários (cliente, restaurante, entregador, admin).
    Para usuários de restaurante, requer o parâmetro restaurant_id na query string.
    
    Returns:
        dict: Dados do perfil do usuário autenticado
    """
    logger.info(f"Endpoint /profile acessado. Método: {request.method}")
    
    if request.method == 'OPTIONS':
        return '', 200
        
    try:
        # Verificação de autenticação
        auth_header = request.headers.get('Authorization')
        if not auth_header:
            logger.warning("Tentativa de acesso sem token de autenticação")
            return jsonify({"error": "Token de autorização não fornecido"}), 401
            
        # Obter ID e tipo do usuário do token
        user_id, user_type, error = get_user_id_from_token(auth_header)
        if error:
            logger.warning(f"Erro na autenticação: {error[0].get('error')}")
            return error
        
        # Obter o restaurant_id (se aplicável)
        restaurant_id = request.args.get('restaurant_id')
        logger.info(f"Buscando perfil para usuário {user_id} (tipo: {user_type}), restaurant_id: {restaurant_id}")
        
        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Erro de conexão com o banco de dados"}), 500
            
        try:
            cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
            
            # CORREÇÃO: Buscar dados básicos do usuário SEM a coluna 'name'
            cursor.execute("""
                SELECT id, email, user_type, created_at 
                FROM users 
                WHERE id = %s
            """, (user_id,))
            
            user_data = cursor.fetchone()
            
            if not user_data:
                logger.warning(f"Usuário {user_id} não encontrado no banco de dados")
                return jsonify({"error": "Usuário não encontrado"}), 404
                
            profile_data = dict(user_data)
            
            # CORREÇÃO: Adicionar name baseado no email (já que não existe coluna name)
            profile_data['name'] = profile_data['email'].split('@')[0]
            
            # Adicionar dados específicos baseados no tipo de usuário
            if user_type == 'client':
                cursor.execute("SELECT * FROM client_profiles WHERE user_id = %s", (user_id,))
                client_profile = cursor.fetchone()
                if client_profile:
                    for k, v in dict(client_profile).items():
                        if k != 'user_id' and k not in profile_data:
                            profile_data[k] = v
            
            elif user_type == 'delivery':
                cursor.execute("SELECT * FROM delivery_profiles WHERE user_id = %s", (user_id,))
                delivery_profile = cursor.fetchone()
                if delivery_profile:
                    for k, v in dict(delivery_profile).items():
                        if k != 'user_id' and k not in profile_data:
                            profile_data[k] = v
                            
            # Para usuários do tipo restaurante ou admin que acessam dados de restaurante
            if restaurant_id and (user_type in ['restaurant', 'admin']):
                # Buscar papel do usuário no restaurante
                cursor.execute("""
                    SELECT role, permissions 
                    FROM restaurant_users 
                    WHERE user_id = %s AND restaurant_id = %s
                """, (user_id, restaurant_id))
                
                restaurant_role = cursor.fetchone()
                
                if restaurant_role:
                    profile_data['restaurant_role'] = restaurant_role['role']
                    profile_data['permissions'] = restaurant_role['permissions']
                else:
                    profile_data['restaurant_role'] = None
                    profile_data['permissions'] = None
                    
                # Se for admin, registrar auditoria
                if user_type == 'admin':
                    log_admin_action(
                        profile_data['email'], 
                        "Acesso ao Perfil", 
                        f"Admin acessou perfil no contexto de restaurante {restaurant_id}",
                        request
                    )
                    
            # Converter timestamps para strings
            if profile_data.get('created_at') and hasattr(profile_data['created_at'], 'isoformat'):
                profile_data['created_at'] = profile_data['created_at'].isoformat()
                
            logger.info(f"Perfil recuperado com sucesso para usuário {user_id}")
            return jsonify(profile_data), 200
            
        finally:
            cursor.close()
            conn.close()
            
    except Exception as e:
        logger.error(f"Erro ao buscar perfil: {str(e)}", exc_info=True)
        return jsonify({"error": "Erro interno no servidor"}), 500
