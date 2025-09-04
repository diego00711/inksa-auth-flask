# src/utils/helpers.py - VERSÃO ATUALIZADA (com suporte a OPTIONS)
import os
import logging
import psycopg2
from psycopg2.extras import register_uuid
from flask import request, jsonify
from functools import wraps
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
# ✅ FUNÇÃO PRINCIPAL CORRIGIDA (com suporte a OPTIONS)
# ======================================================================
def get_user_id_from_token(auth_header):
    """
    Valida o token JWT e extrai o user_id e o user_type diretamente dele.
    Retorna None para requisições OPTIONS.
    """
    # Permitir requisições OPTIONS sem autenticação
    if request.method == "OPTIONS":
        return None, None, None
        
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
        # Isso é mais rápido evita uma consulta desnecessária ao banco.
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

# ======================================================================
# ✅ FUNÇÃO AUSENTE ADICIONADA: delivery_token_required (com suporte a OPTIONS)
# ======================================================================
def delivery_token_required(f):
    """
    Decorator que verifica se o token é válido e pertence a um usuário do tipo 'delivery'.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Permitir requisições OPTIONS sem autenticação
        if request.method == 'OPTIONS':
            return jsonify(), 200
            
        auth_header = request.headers.get('Authorization')
        user_id, user_type, error_response = get_user_id_from_token(auth_header)
        
        if error_response:
            return error_response
        
        if user_type != 'delivery':
            return jsonify({"error": "Acesso negado. Apenas usuários do tipo delivery podem acessar esta rota."}), 403
        
        # Adiciona user_id e user_type ao contexto da requisição
        request.user_id = user_id
        request.user_type = user_type
        
        return f(*args, **kwargs)
    
    return decorated_function

# ======================================================================
# ✅ FUNÇÃO AUSENTE ADICIONADA: serialize_delivery_data
# ======================================================================
def serialize_delivery_data(row):
    """
    Serializa dados de entregas para formato JSON.
    """
    if not row:
        return None
    
    return {
        'id': row[0],
        'order_id': row[1],
        'delivery_user_id': row[2],
        'status': row[3],
        'created_at': row[4].isoformat() if row[4] else None,
        'updated_at': row[5].isoformat() if row[5] else None,
        'delivery_address': row[6],
        'customer_phone': row[7],
        'estimated_delivery_time': row[8].isoformat() if row[8] else None,
        'actual_delivery_time': row[9].isoformat() if row[9] else None,
        'delivery_notes': row[10],
        'delivery_fee': float(row[11]) if row[11] else None,
        'payment_status': row[12],
        'customer_name': row[13],
        'restaurant_name': row[14],
        'restaurant_address': row[15]
    }
