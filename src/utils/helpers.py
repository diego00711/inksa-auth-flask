# src/utils/helpers.py

import os
from flask import jsonify
import psycopg2
import psycopg2.extras
from supabase import create_client, Client
from typing import Tuple, Optional, Union

# Configuração centralizada
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
DATABASE_URL = os.environ.get("DATABASE_URL")

# Cliente Supabase
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

def get_db_connection():
    """Estabelece conexão com o banco de dados PostgreSQL"""
    try:
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = False
        return conn
    except Exception as e:
        print(f"Erro na conexão com o banco de dados: {str(e)}")
        return None

def get_user_id_from_token(auth_header: str) -> Tuple[Optional[str], Optional[str], Optional[tuple]]:
    """
    Validação robusta de token JWT com Supabase
    
    Retorna:
        Tuple (user_id, user_type, error_response)
    
    Exemplo de uso:
        user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
        if error:
            return error
    """
    # Verificação básica do header
    if not auth_header or not auth_header.startswith('Bearer '):
        return None, None, (jsonify({
            "status": "error",
            "error": "Cabeçalho de autorização ausente ou mal formatado",
            "solution": "Inclua 'Bearer <token>' no header 'Authorization'"
        }), 401)

    token = auth_header.split(' ')[1].strip()
    if not token:
        return None, None, (jsonify({
            "error": "Token vazio",
            "details": "O token não pode ser uma string vazia"
        }), 401)

    try:
        # Validação com Supabase (versão mais recente da biblioteca)
        user_data = supabase.auth.get_user(token)
        
        if not user_data or not hasattr(user_data, 'user'):
            return None, None, (jsonify({
                "error": "Credenciais inválidas",
                "details": "O token não corresponde a nenhum usuário ativo"
            }), 403)

        user = user_data.user
        user_id = str(user.id)
        user_type = user.user_metadata.get('user_type')

        # Verificação adicional no banco local (opcional)
        conn = get_db_connection()
        if conn:
            try:
                with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                    cur.execute(
                        "SELECT user_type FROM users WHERE id = %s",
                        (user_id,)
                    )
                    db_user = cur.fetchone()
                    if db_user:
                        user_type = db_user['user_type'] or user_type
            except Exception as db_error:
                print(f"Aviso: Erro ao verificar banco local - {str(db_error)}")
            finally:
                conn.close()

        if not user_type:
            return None, None, (jsonify({
                "error": "Perfil incompleto",
                "details": "O tipo de usuário não está definido",
                "solution": "Complete seu cadastro no sistema"
            }), 403)

        return user_id, user_type, None

    except Exception as e:
        return None, None, (jsonify({
            "status": "error",
            "error": "Falha na autenticação",
            "details": str(e),
            "solution": "Tente novamente ou redefina seu token"
        }), 401)