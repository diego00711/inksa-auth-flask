# src/utils/helpers.py

import os
import psycopg2
import psycopg2.extras
from flask import jsonify
from supabase import create_client, Client
from dotenv import load_dotenv
import logging

# Configurar logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

load_dotenv()

# --- Configuração do Supabase ---
url: str = os.environ.get("SUPABASE_URL")
key: str = os.environ.get("SUPABASE_KEY")

# Debug: Verifique se as variáveis estão sendo carregadas
logger.info("=== DEBUG: Variáveis de ambiente ===")
logger.info(f"SUPABASE_URL: {'✅' if url else '❌'} {url}")
logger.info(f"SUPABASE_KEY: {'✅' if key else '❌'} {key[:20]}...{key[-20:] if key and len(key) > 40 else ''}")
logger.info(f"DATABASE_URL: {'✅' if os.environ.get('DATABASE_URL') else '❌'}")
logger.info("===================================")

if not url or not key:
    raise ValueError("Variáveis de ambiente SUPABASE_URL e SUPABASE_KEY são necessárias")

try:
    supabase: Client = create_client(url, key)
    logger.info("✅ Cliente Supabase inicializado com sucesso")
except Exception as e:
    logger.error(f"❌ Erro ao inicializar Supabase: {e}")
    supabase = None

# --- Conexão com o Banco de Dados ---
def get_db_connection():
    """Estabelece e retorna uma conexão com o banco de dados PostgreSQL usando DATABASE_URL."""
    try:
        database_url = os.environ.get("DATABASE_URL")
        
        if not database_url:
            logger.error("Erro: Variável de ambiente DATABASE_URL não encontrada.")
            return None

        logger.info(f"Tentando conectar com: {database_url.split('@')[1] if '@' in database_url else 'Database URL'}")

        # Conexão com SSL obrigatório para Render + Supabase
        conn = psycopg2.connect(database_url, sslmode="require")
        logger.info("✅ Conexão com banco de dados estabelecida com sucesso")
        return conn
        
    except psycopg2.OperationalError as e:
        logger.error(f"❌ Erro de conexão com o banco de dados: {e}")
        return None
    except Exception as e:
        logger.error(f"❌ Erro inesperado na conexão com o banco: {e}")
        return None

# --- Função de Autenticação ---
def get_user_id_from_token(auth_header):
    """
    Valida o token JWT, extrai o ID do usuário e busca o tipo de usuário
    diretamente do banco de dados para maior segurança.
    """
    if not auth_header:
        return None, None, jsonify({"error": "Token de autorização ausente"}), 401
    
    parts = auth_header.split()
    if parts[0].lower() != 'bearer' or len(parts) != 2:
        return None, None, jsonify({"error": "Formato do token inválido"}), 401
        
    jwt_token = parts[1]
    
    try:
        # 1. Valida o token com o Supabase
        if not supabase:
            logger.error("Erro: Cliente Supabase não inicializado. Verifique as variáveis de ambiente SUPABASE_URL and SUPABASE_KEY.")
            return None, None, jsonify({"error": "Configuração do servidor incompleta"}), 500

        user_response = supabase.auth.get_user(jwt_token)
        user = user_response.user
        if not user:
            raise ValueError("Token inválido ou expirado")
            
        user_id = str(user.id)

        # 2. Busca o tipo de usuário na tabela public.users (sua tabela personalizada)
        conn = get_db_connection()
        if not conn:
            return None, None, jsonify({"error": "Falha na conexão com o banco de dados"}), 500
            
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                # CORREÇÃO: Buscar da tabela public.users
                cur.execute("SELECT user_type FROM public.users WHERE id = %s", (user_id,))
                db_user = cur.fetchone()
            
            if not db_user:
                return None, None, jsonify({"error": "Usuário não encontrado no banco de dados local"}), 404
                
            user_type = db_user['user_type']
            
            # 3. Retorna os dados validados
            return user_id, user_type, None, None
            
        finally:
            conn.close()

    except ValueError as e:
        logger.warning(f"Token inválido: {e}")
        return None, None, jsonify({"error": "Token inválido ou expirado"}), 401
    except Exception as e:
        logger.error(f"❌ Erro na validação do token: {e}")
        return None, None, jsonify({"error": "Erro interno na validação do token"}), 500

# --- Função auxiliar para verificar se o usuário é admin ---
def is_admin(user_id):
    """Verifica se o usuário é administrador."""
    try:
        conn = get_db_connection()
        if not conn:
            return False
            
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # CORREÇÃO: Buscar da tabela public.users
            cur.execute("SELECT user_type FROM public.users WHERE id = %s", (user_id,))
            db_user = cur.fetchone()
        
        conn.close()

        return db_user and db_user['user_type'] == 'admin'
        
    except Exception as e:
        logger.error(f"❌ Erro ao verificar se usuário é admin: {e}")
        return False

# --- Função auxiliar para verificar se o usuário é estabelecimento ---
def is_establishment(user_id):
    """Verifica se o usuário é estabelecimento."""
    try:
        conn = get_db_connection()
        if not conn:
            return False
            
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # CORREÇÃO: Buscar da tabela public.users
            cur.execute("SELECT user_type FROM public.users WHERE id = %s", (user_id,))
            db_user = cur.fetchone()
        
        conn.close()

        return db_user and db_user['user_type'] == 'establishment'
        
    except Exception as e:
        logger.error(f"❌ Erro ao verificar se usuário é estabelecimento: {e}")
        return False

# --- Função auxiliar para verificar se o usuário é cliente ---
def is_client(user_id):
    """Verifica se o usuário é cliente."""
    try:
        conn = get_db_connection()
        if not conn:
            return False
            
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # CORREÇÃO: Buscar da tabela public.users
            cur.execute("SELECT user_type FROM public.users WHERE id = %s", (user_id,))
            db_user = cur.fetchone()
        
        conn.close()

        return db_user and db_user['user_type'] == 'client'
        
    except Exception as e:
        logger.error(f"❌ Erro ao verificar se usuário é cliente: {e}")
        return False

# --- Função auxiliar para verificar se o usuário é entregador ---
def is_delivery(user_id):
    """Verifica se o usuário é entregador."""
    try:
        conn = get_db_connection()
        if not conn:
            return False
            
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # CORREÇÃO: Buscar da tabela public.users
            cur.execute("SELECT user_type FROM public.users WHERE id = %s", (user_id,))
            db_user = cur.fetchone()
        
        conn.close()

        return db_user and db_user['user_type'] == 'delivery'
        
    except Exception as e:
        logger.error(f"❌ Erro ao verificar se usuário é entregador: {e}")
        return False

# --- Função para obter informações completas do usuário ---
def get_user_info(user_id):
    """Obtém informações completas do usuário da tabela public.users."""
    try:
        conn = get_db_connection()
        if not conn:
            return None
            
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # CORREÇÃO: Buscar da tabela public.users com colunas existentes
            cur.execute("""
                SELECT id, email, user_type, created_at, updated_at, last_login
                FROM public.users WHERE id = %s
            """, (user_id,))
            user_info = cur.fetchone()
        
        conn.close()
        return user_info
        
    except Exception as e:
        logger.error(f"❌ Erro ao obter informações do usuário: {e}")
        return None

# --- Função para obter tipo de usuário ---
def get_user_type(user_id):
    """Obtém apenas o tipo de usuário."""
    try:
        conn = get_db_connection()
        if not conn:
            return None
            
        with conn.cursor() as cur:
            # CORREÇÃO: Buscar da tabela public.users
            cur.execute("SELECT user_type FROM public.users WHERE id = %s", (user_id,))
            result = cur.fetchone()
        
        conn.close()
        return result[0] if result else None
        
    except Exception as e:
        logger.error(f"❌ Erro ao obter tipo de usuário: {e}")
        return None
