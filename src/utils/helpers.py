# src/utils/helpers.py — VERSÃO ROBUSTA (corrigida, sem uuid)

import os
import json
import uuid
import logging
import psycopg2
import psycopg2.extras
from psycopg2.extras import register_uuid
from flask import jsonify
from supabase import create_client, Client
from datetime import date, datetime, timedelta, time
from decimal import Decimal
from typing import Optional

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Supabase ---
supabase: Optional[Client] = None
try:
    SUPABASE_URL = os.environ.get("SUPABASE_URL")
    SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise ValueError("SUPABASE_URL e SUPABASE_SERVICE_KEY são obrigatórias.")
    supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    logger.info("✅ Supabase client inicializado.")
except Exception as e:
    logger.error(f"❌ Falha ao inicializar Supabase: {e}")
    supabase = None


# --- DB ---
def get_db_connection():
    url = os.environ.get("DATABASE_URL")
    if not url:
        logger.error("❌ DATABASE_URL não encontrada.")
        return None
    try:
        conn = psycopg2.connect(url)
        register_uuid(None, conn)  # garante suporte a UUID no cursor
        return conn
    except Exception as e:
        logger.error(f"❌ Conexão DB falhou: {e}", exc_info=True)
        return None


# --- Auth helper ---
def _extract_bearer_token(auth_header: str):
    """Extrai o token de um cabeçalho Authorization.
    Aceita:
      - 'Bearer <jwt>'
      - '<jwt>' (sem 'Bearer', comum quando front erra)
    """
    if not auth_header:
        return None
    parts = auth_header.strip().split()
    if len(parts) == 0:
        return None
    if parts[0].lower() == "bearer" and len(parts) >= 2:
        return parts[1]
    # se não veio 'Bearer', mas é um JWT, devolve assim mesmo
    return parts[0]


def get_user_id_from_token(auth_header):
    """
    Retorna (user_id:str, user_type:str|None, error_response|None)
    - Em caso de erro/autorização, o terceiro item é um tuple (json_response, status_code)
    """
    token = _extract_bearer_token(auth_header)
    if not token:
        return None, None, (jsonify({"error": "Authorization ausente ou inválido"}), 401)

    conn = None
    try:
        if not supabase:
            raise RuntimeError("Supabase client não inicializado.")

        # Valida o JWT no Supabase e extrai o user.id (UUID do auth)
        user_resp = supabase.auth.get_user(token)
        user = getattr(user_resp, "user", None)
        if not user:
            return None, None, (jsonify({"error": "Token inválido ou expirado"}), 401)

        user_id = str(user.id)

        conn = get_db_connection()
        if not conn:
            return None, None, (jsonify({"error": "Falha ao conectar para verificar permissões"}), 500)

        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # ✅ versão segura: consulta SOMENTE por 'id' (remove OR uuid = %s)
            cur.execute(
                """
                SELECT user_type
                FROM public.users
                WHERE id = %s
                LIMIT 1
                """,
                (user_id,),
            )
            row = cur.fetchone()

            # (Opcional) Fallback: verificar existência no catálogo do Supabase Auth
            if not row:
                try:
                    cur.execute(
                        """
                        SELECT id
                        FROM auth.users
                        WHERE id = %s
                        LIMIT 1
                        """,
                        (user_id,),
                    )
                    auth_row = cur.fetchone()
                    if auth_row:
                        # Usuário existe no auth, mas não tem permissão registrada na sua tabela
                        return None, None, (jsonify({"error": "Permissão não encontrada para este usuário"}), 403)
                except Exception:
                    # Se o role do banco não permite ler auth.users, ignore o fallback
                    pass

        if not row or not row.get("user_type"):
            return None, None, (jsonify({"error": "Permissão não encontrada para este usuário"}), 403)

        return user_id, row["user_type"], None

    except Exception as e:
        msg = str(e)
        logger.error(f"Erro ao processar token: {msg}", exc_info=True)
        if "invalid" in msg.lower() or "jwt" in msg.lower() or "token" in msg.lower():
            return None, None, (jsonify({"error": f"Erro de autenticação: {msg}"}), 401)
        return None, None, (jsonify({"error": "Erro interno ao validar token"}), 500)
    finally:
        if conn:
            conn.close()


# --- JSON utils ---
class CustomJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        if isinstance(obj, (datetime, date, time)):
            return obj.isoformat()
        if isinstance(obj, uuid.UUID):
            return str(obj)
        return super().default(obj)


def serialize_data(data):
    return json.loads(json.dumps(data, cls=CustomJSONEncoder))
