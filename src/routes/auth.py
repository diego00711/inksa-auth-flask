# src/routes/auth.py - VERSÃO COM LOGOUT, ME E FECHAMENTO AUTOMÁTICO

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


# ✅ NOVO ENDPOINT: RETORNA DADOS DO USUÁRIO AUTENTICADO (INCLUI EMAIL)
@auth_bp.route('/me', methods=['GET'])
def get_current_user():
    """
    Retorna os dados do usuário autenticado incluindo email.
    Usado pelo frontend para obter o email do usuário.
    """
    try:
        # Valida e extrai o token do header
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            return jsonify({
                "status": "error", 
                "error": "Token de autenticação não fornecido"
            }), 401
        
        token = auth_header.split('Bearer ')[1]
        
        # Busca o usuário usando o token
        user_response = supabase.auth.get_user(token)
        
        if not user_response or not user_response.user:
            return jsonify({
                "status": "error",
                "error": "Usuário não encontrado ou token inválido"
            }), 401
        
        user = user_response.user
        user_metadata = user.user_metadata or {}
        user_type = user_metadata.get('user_type', 'unknown')
        
        logger.info(f"✅ Dados do usuário retornados: {user.id}")
        
        return jsonify({
            "status": "success",
            "data": {
                "id": user.id,
                "email": user.email,
                "user_type": user_type,
                "created_at": str(user.created_at) if user.created_at else None,
                "user_metadata": user_metadata
            }
        }), 200
        
    except Exception as e:
        logger.error(f"❌ Erro ao buscar usuário autenticado: {e}", exc_info=True)
        return jsonify({
            "status": "error",
            "error": "Erro ao buscar dados do usuário"
        }), 500


# ✅ ROTA: LOGOUT COM FECHAMENTO AUTOMÁTICO DE RESTAURANTE
@auth_bp.route('/logout', methods=['POST'])
def logout():
    """
    Faz logout do usuário e, se for restaurante, fecha automaticamente.
    """
    try:
        # Pega o token do header
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            return jsonify({
                "status": "error", 
                "error": "Token de autenticação não fornecido"
            }), 401
        
        token = auth_header.split('Bearer ')[1]
        
        # Busca informações do usuário usando o token
        try:
            user_response = supabase.auth.get_user(token)
            user = user_response.user if user_response else None
            
            if user:
                user_id = user.id
                user_metadata = user.user_metadata or {}
                user_type = user_metadata.get('user_type', 'unknown')
                
                logger.info(f"🔓 Logout iniciado para user_id: {user_id}, tipo: {user_type}")
                
                # ✅ SE FOR RESTAURANTE, FECHA AUTOMATICAMENTE
                if user_type == 'restaurant':
                    try:
                        # Atualiza o status para fechado
                        supabase.table('restaurant_profiles').update({
                            'is_open': False,
                            'updated_at': 'now()'
                        }).eq('user_id', user_id).execute()
                        
                        logger.info(f"🏪 Restaurante {user_id} fechado automaticamente no logout")
                        
                    except Exception as e:
                        logger.error(f"⚠️ Erro ao fechar restaurante no logout: {e}")
                        # Não bloqueia o logout se der erro ao fechar
                
                # ✅ SE FOR ENTREGADOR, PODE ADICIONAR LÓGICA AQUI
                elif user_type == 'delivery':
                    try:
                        # Por exemplo: marcar como offline
                        supabase.table('delivery_profiles').update({
                            'is_online': False,
                            'updated_at': 'now()'
                        }).eq('user_id', user_id).execute()
                        
                        logger.info(f"🚴 Entregador {user_id} marcado como offline no logout")
                        
                    except Exception as e:
                        logger.error(f"⚠️ Erro ao atualizar entregador no logout: {e}")
                
        except Exception as e:
            logger.warning(f"⚠️ Não foi possível buscar dados do usuário no logout: {e}")
            # Continua com o logout mesmo se não conseguir buscar dados
        
        # Invalida o token no Supabase
        try:
            supabase.auth.sign_out()
            logger.info("✅ Token invalidado com sucesso")
        except Exception as e:
            logger.warning(f"⚠️ Erro ao invalidar token: {e}")
        
        return jsonify({
            "status": "success",
            "message": "Logout realizado com sucesso"
        }), 200
        
    except Exception as e:
        logger.error(f"❌ Erro crítico no logout: {e}", exc_info=True)
        return jsonify({
            "status": "error",
            "error": "Erro ao realizar logout"
        }), 500
