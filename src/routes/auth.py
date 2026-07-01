# src/routes/auth.py - VERSÃO COM LOGOUT, ME, FECHAMENTO AUTOMÁTICO, REGISTER E FORGOT-PASSWORD

import logging
import re
from flask import Blueprint, request, jsonify
from ..utils.helpers import get_db_connection, get_user_id_from_token, supabase
from src.extensions import limiter

auth_bp = Blueprint('auth_bp', __name__)
logger = logging.getLogger(__name__)

# Rótulos amigáveis por tipo de conta (usado nas mensagens de erro)
USER_TYPE_LABELS = {
    'client': 'Cliente',
    'restaurant': 'Restaurante',
    'delivery': 'Entregador',
    'admin': 'Administrador',
}


def _traduzir_erro_supabase(err_msg: str) -> str:
    """Converte mensagens de erro do Supabase (em inglês) para PT-BR amigável."""
    m = (err_msg or '').lower()
    if 'invalid login credentials' in m or 'invalid credentials' in m:
        return 'E-mail ou senha incorretos. Confira e tente de novo.'
    if 'email not confirmed' in m:
        return 'Seu e-mail ainda não foi confirmado. Verifique sua caixa de entrada.'
    if 'user not found' in m:
        return 'Não encontramos uma conta com esse e-mail.'
    if 'too many requests' in m or 'rate limit' in m:
        return 'Muitas tentativas em pouco tempo. Aguarde um momento e tente de novo.'
    if 'network' in m or 'timeout' in m:
        return 'Falha de conexão. Verifique sua internet e tente novamente.'
    return 'Não foi possível entrar. Verifique seus dados e tente novamente.'


@auth_bp.route('/login', methods=['POST'])
@limiter.limit("10 per minute")
def login():
    try:
        data = request.get_json()
        if not data or 'email' not in data or 'password' not in data:
            return jsonify({"status": "error", "error": "Preencha e-mail e senha."}), 400

        email, password = data.get('email'), data.get('password')
        # Cada app envia o tipo esperado (client/restaurant/delivery) para bloquear login cruzado
        expected_user_type = (data.get('expected_user_type') or '').strip().lower() or None

        try:
            auth_response = supabase.auth.sign_in_with_password({"email": email, "password": password})
        except Exception as auth_err:
            friendly = _traduzir_erro_supabase(str(auth_err.args[0]) if auth_err.args else str(auth_err))
            return jsonify({"status": "error", "error": friendly}), 401

        user, session = auth_response.user, auth_response.session
        if not user or not session:
            return jsonify({"status": "error", "error": "E-mail ou senha incorretos."}), 401

        user_metadata = user.user_metadata or {}
        user_type = user_metadata.get('user_type', 'unknown')

        # --- Bloqueia login cruzado entre apps ---
        if expected_user_type and user_type != expected_user_type:
            conta_label = USER_TYPE_LABELS.get(user_type, 'de outro tipo')
            app_label = USER_TYPE_LABELS.get(expected_user_type, 'este')
            return jsonify({
                "status": "error",
                "error": f"Esta conta é de {conta_label}, não pode entrar no app de {app_label}. Use o app correto ou crie uma conta de {app_label}.",
                "error_code": "WRONG_ACCOUNT_TYPE"
            }), 403

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
        return jsonify({"status": "error", "error": "Erro interno ao entrar. Tente novamente em instantes."}), 500


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


# ✅ NOVO ENDPOINT: CADASTRO DE RESTAURANTE
@auth_bp.route('/register', methods=['POST'])
@limiter.limit("5 per minute")
def register():
    """
    Cadastra um novo usuário no Supabase Auth.
    Campos obrigatórios: name, email, password
    Campo opcional: user_type (default: 'client')
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({"status": "error", "error": "Corpo da requisição inválido ou ausente"}), 400

        name = (data.get('name') or '').strip()
        email = (data.get('email') or '').strip().lower()
        password = data.get('password') or ''
        # Aceita tanto snake_case quanto camelCase; padrão é 'client'
        user_type = (data.get('user_type') or data.get('userType') or 'client').strip().lower()

        VALID_USER_TYPES = {'client', 'restaurant', 'delivery'}
        if user_type not in VALID_USER_TYPES:
            user_type = 'client'

        # --- Validações ---
        if not name:
            return jsonify({"status": "error", "error": "O campo 'name' é obrigatório"}), 400

        if not email:
            return jsonify({"status": "error", "error": "O campo 'email' é obrigatório"}), 400

        email_regex = r'^[^\s@]+@[^\s@]+\.[^\s@]+$'
        if not re.match(email_regex, email):
            return jsonify({"status": "error", "error": "Formato de e-mail inválido"}), 400

        if len(password) < 6:
            return jsonify({"status": "error", "error": "A senha deve ter no mínimo 6 caracteres"}), 400

        if not supabase:
            logger.error("Supabase client não inicializado em /register")
            return jsonify({"status": "error", "error": "Serviço de autenticação indisponível"}), 500

        # --- Cria o usuário no Supabase Auth ---
        try:
            sign_up_response = supabase.auth.sign_up({
                "email": email,
                "password": password,
                "options": {
                    "data": {
                        "name": name,
                        "user_type": user_type
                    }
                }
            })
        except Exception as auth_err:
            err_msg = str(auth_err)
            logger.warning(f"Erro ao criar usuário no Supabase Auth: {err_msg}")
            # Supabase retorna erro específico para e-mail duplicado
            if "already registered" in err_msg.lower() or "user already exists" in err_msg.lower() or "already been registered" in err_msg.lower():
                return jsonify({"status": "error", "error": "E-mail já cadastrado"}), 409
            return jsonify({"status": "error", "error": "Erro ao criar conta: " + err_msg}), 400

        user = sign_up_response.user if sign_up_response else None
        if not user:
            # Supabase retorna user=None quando o e-mail já existe mas confirmação está desativada
            return jsonify({"status": "error", "error": "E-mail já cadastrado"}), 409

        user_id = str(user.id)
        logger.info(f"Usuário registrado: user_id={user_id}, email={email}, user_type={user_type}")

        # --- Cria o perfil na tabela correspondente ao user_type ---
        phone = (data.get('phone') or '').strip()
        _db_conn = None
        try:
            _db_conn = get_db_connection()
            if _db_conn:
                with _db_conn.cursor() as _cur:
                    if user_type == 'restaurant':
                        _cur.execute(
                            """INSERT INTO restaurant_profiles (id, user_id, restaurant_name, phone, is_open)
                               VALUES (%s, %s, %s, %s, FALSE)
                               ON CONFLICT (user_id) DO NOTHING""",
                            (user_id, user_id, name, phone or None)
                        )
                        logger.info(f"✅ Perfil de restaurante criado para user_id={user_id}")
                    elif user_type == 'delivery':
                        first_name = name.split()[0] if name else 'Entregador'
                        last_name = ' '.join(name.split()[1:]) if name and ' ' in name else ''
                        _cur.execute(
                            """INSERT INTO delivery_profiles (user_id, first_name, last_name, phone)
                               VALUES (%s, %s, %s, %s)
                               ON CONFLICT (user_id) DO NOTHING""",
                            (user_id, first_name, last_name or None, phone or '00000000000')
                        )
                        logger.info(f"✅ Perfil de entregador criado para user_id={user_id}")
                    elif user_type == 'client':
                        name_parts = name.split(' ', 1)
                        client_first = name_parts[0]
                        client_last = name_parts[1] if len(name_parts) > 1 else ''
                        _cur.execute(
                            """INSERT INTO client_profiles (user_id, first_name, last_name, phone)
                               VALUES (%s, %s, %s, %s)""",
                            (user_id, client_first, client_last, phone or None)
                        )
                        logger.info(f"✅ Perfil de cliente criado para user_id={user_id}")
                _db_conn.commit()
            else:
                logger.warning(f"⚠️ Não foi possível criar perfil de {user_type} — conexão DB falhou")
        except Exception as _profile_err:
            logger.error(f"⚠️ Erro ao criar perfil de {user_type} para {user_id}: {_profile_err}", exc_info=True)
            if _db_conn:
                try: _db_conn.rollback()
                except Exception: pass
            # Não bloqueia o cadastro — perfil será auto-criado na primeira requisição autenticada
        finally:
            if _db_conn:
                try: _db_conn.close()
                except Exception: pass

        return jsonify({
            "status": "success",
            "data": {
                "message": "Cadastro realizado com sucesso",
                "user": {
                    "id": user_id,
                    "email": user.email,
                    "user_type": user_type
                }
            }
        }), 201

    except Exception as e:
        logger.error(f"Erro crítico em /register: {e}", exc_info=True)
        return jsonify({"status": "error", "error": "Erro interno ao realizar cadastro"}), 500


# ✅ NOVO ENDPOINT: ESQUECI A SENHA
@auth_bp.route('/forgot-password', methods=['POST'])
def forgot_password():
    """
    Envia e-mail de reset de senha via Supabase Auth.
    Por segurança, sempre retorna 200 independentemente de o e-mail existir ou não.
    Campo obrigatório: email
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({"status": "error", "error": "Corpo da requisição inválido ou ausente"}), 400

        email = (data.get('email') or '').strip().lower()

        # --- Validações ---
        if not email:
            return jsonify({"status": "error", "error": "O campo 'email' é obrigatório"}), 400

        email_regex = r'^[^\s@]+@[^\s@]+\.[^\s@]+$'
        if not re.match(email_regex, email):
            return jsonify({"status": "error", "error": "Formato de e-mail inválido"}), 400

        if not supabase:
            logger.error("Supabase client não inicializado em /forgot-password")
            return jsonify({"status": "error", "error": "Serviço de autenticação indisponível"}), 500

        # --- Para onde o link do e-mail deve levar (a página de redefinição do
        # app que pediu) — derivado do Origin, validado contra os domínios aceitos.
        origin = (request.headers.get('Origin') or '').rstrip('/')
        origin_ok = bool(origin) and (
            origin.endswith('.inksadelivery.com.br')
            or origin.endswith('.vercel.app')
            or origin.startswith('http://localhost')
            or origin.startswith('http://127.0.0.1')
        )
        redirect_to = (f"{origin}/reset-password" if origin_ok
                       else "https://clientes.inksadelivery.com.br/reset-password")

        # --- Envia o e-mail de reset via Supabase Auth ---
        try:
            try:
                supabase.auth.reset_password_email(email, {"redirect_to": redirect_to})
            except TypeError:
                # SDK sem suporte a options nesse formato — envia sem redirect
                supabase.auth.reset_password_email(email)
            logger.info(f"Reset de senha solicitado para: {email} (redirect={redirect_to})")
        except Exception as reset_err:
            # Registra internamente mas NÃO revela ao cliente se o e-mail existe ou não
            logger.warning(f"Erro ao enviar reset de senha para {email}: {reset_err}")

        # Sempre retorna 200 por segurança (não revela se o e-mail está cadastrado)
        return jsonify({
            "status": "success",
            "data": {
                "message": "Se o e-mail estiver cadastrado, você receberá as instruções de recuperação em breve."
            }
        }), 200

    except Exception as e:
        logger.error(f"Erro crítico em /forgot-password: {e}", exc_info=True)
        return jsonify({"status": "error", "error": "Erro interno ao processar solicitação"}), 500


@auth_bp.route('/reset-password', methods=['POST'])
def reset_password():
    """Redefine a senha usando o token de recuperação (access_token vindo do link do e-mail).
    Campos: token (ou access_token) + new_password (ou password).
    """
    try:
        data = request.get_json() or {}
        access_token = (data.get('token') or data.get('access_token') or '').strip()
        new_password = data.get('new_password') or data.get('password') or ''

        if not access_token:
            return jsonify({"status": "error", "error": "Token de redefinição ausente"}), 400
        if len(new_password) < 6:
            return jsonify({"status": "error", "error": "A nova senha deve ter no mínimo 6 caracteres"}), 400
        if not supabase:
            return jsonify({"status": "error", "error": "Serviço de autenticação indisponível"}), 500

        # Valida o token de recuperação e identifica o usuário
        try:
            user_resp = supabase.auth.get_user(access_token)
            user = getattr(user_resp, "user", None)
        except Exception as ve:
            logger.warning(f"Token de reset inválido: {ve}")
            user = None
        if not user:
            return jsonify({"status": "error", "error": "Link expirado ou inválido. Solicite um novo."}), 401

        # Atualiza a senha via admin API (service_role)
        supabase.auth.admin.update_user_by_id(user.id, {"password": new_password})
        logger.info(f"Senha redefinida com sucesso para o usuário {user.id}")
        return jsonify({
            "status": "success",
            "message": "Senha redefinida com sucesso! Faça login com a nova senha.",
        }), 200

    except Exception as e:
        logger.error(f"Erro em /reset-password: {e}", exc_info=True)
        return jsonify({"status": "error", "error": "Erro ao redefinir a senha"}), 500
