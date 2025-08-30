# src/utils/helpers.py - VERSÃO ATUALIZADA (registra adaptador UUID)
import os
import logging
import psycopg2
from psycopg2.extras import register_uuid
from flask import request, jsonify
from supabase import create_client, Client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Read AUDIT_DEBUG setting
AUDIT_DEBUG = os.environ.get("AUDIT_DEBUG", "false").lower() in ("true", "1", "yes")

# Inicialização do cliente Supabase
supabase_client_type = None
try:
    SUPABASE_URL = os.environ.get("SUPABASE_URL")
    SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
    SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

    if AUDIT_DEBUG:
        logger.info(f"[AUDIT_DEBUG] Environment variables presence: "
                   f"SUPABASE_URL={bool(SUPABASE_URL)}, "
                   f"SUPABASE_SERVICE_KEY={bool(SUPABASE_SERVICE_KEY)}, "
                   f"SUPABASE_KEY={bool(SUPABASE_KEY)}")

    if not SUPABASE_URL:
        raise ValueError("Variável de ambiente SUPABASE_URL é obrigatória.")

    if SUPABASE_SERVICE_KEY:
        supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
        supabase_client_type = "service"
        logger.info("✅ Cliente Supabase inicializado com sucesso usando Service Role Key")
    elif SUPABASE_KEY:
        supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
        supabase_client_type = "default"
        logger.info("✅ Cliente Supabase inicializado com sucesso usando Anon Key")
    else:
        raise ValueError("Variável de ambiente SUPABASE_SERVICE_KEY ou SUPABASE_KEY é obrigatória.")
    
    if AUDIT_DEBUG:
        logger.info(f"[AUDIT_DEBUG] Supabase client initialized: type={supabase_client_type}")
        
except Exception as e:
    logger.error(f"❌ Falha ao inicializar o cliente Supabase: {e}")
    supabase = None
    supabase_client_type = None

def get_db_connection():
    """
    Cria e retorna uma conexão psycopg2 configurada.
    Registra adaptador de UUID para permitir passar objetos uuid.UUID diretamente
    como parâmetros em cur.execute(..., (some_uuid,)).
    Retorna None em caso de falha na conexão.
    """
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        logger.error("❌ DATABASE_URL não encontrada nas variáveis de ambiente.")
        return None

    try:
        conn = psycopg2.connect(database_url)
        # Registrar adaptador UUID para a conexão (permite passar uuid.UUID)
        try:
            register_uuid(conn)
            if AUDIT_DEBUG:
                logger.info("[AUDIT_DEBUG] register_uuid aplicado à conexão com sucesso")
        except Exception as e:
            # Não falhar a criação da conexão só por causa do registro do adaptador,
            # mas logar para investigação.
            logger.warning(f"⚠️ Falha ao registrar adaptador UUID na conexão: {e}")

        logger.info("✅ Conexão com banco de dados estabelecida com sucesso")
        return conn
    except Exception as e:
        logger.error(f"❌ Falha na conexão com o banco de dados: {e}", exc_info=True)
        return None

# ======================================================================
# ✅ FUNÇÃO PRINCIPAL CORRIGIDA (sem mudanças de lógica; mantém comportamento)
# ======================================================================
def get_user_id_from_token(auth_header):
    """
    Valida o token JWT e extrai o user_id e o user_type diretamente dele.
    """
    if not auth_header or not auth_header.startswith('Bearer '):
        # Retorna uma tupla de erro que pode ser usada diretamente no return da rota
        return None, None, (jsonify({"error": "Cabeçalho de autorização inválido"}), 401)

    token = auth_header.split(' ')[1]
    
    try:
        # Obter o objeto do usuário do Supabase usando o token
        user_response = supabase.auth.get_user(token)
        user = user_response.user
        
        if not user:
            return None, None, (jsonify({"error": "Token inválido ou expirado"}), 401)

        user_id = user.id
        
        # ✅ CORREÇÃO: Pega o user_type diretamente do user_metadata do token.
        # Isso é mais rápido e evita uma consulta desnecessária ao banco.
        user_type = user.user_metadata.get('user_type') if user.user_metadata else None
        
        if not user_type:
            # Se o tipo de usuário não estiver nos metadados do token, o acesso não é permitido.
            return None, None, (jsonify({"error": "Tipo de usuário (user_type) não encontrado no token"}), 403)

        # Se tudo deu certo, retorna o ID (string), o tipo e None para o erro.
        return str(user_id), user_type, None

    except Exception as e:
        logger.error(f"Erro ao decodificar ou validar token: {e}", exc_info=True)
        return None, None, (jsonify({"error": "Erro interno ao processar o token"}), 500)

# ======================================================================
# Função auxiliar para obter info do usuário (mantida)
# ======================================================================
def get_user_info():
    """
    Extrai informações do usuário a partir do token JWT no header Authorization.
    """
    try:
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            return None
        token = auth_header.split(' ')[1]
        user_response = supabase.auth.get_user(token)
        user = user_response.user
        if not user:
            return None
        return {
            'user_id': user.id,
            'email': user.email,
        }
    except Exception as e:
        logger.error(f"Erro ao obter informações do usuário: {e}", exc_info=True)
        return None
