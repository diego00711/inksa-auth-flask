# src/utils/helpers.py — VERSÃO ROBUSTA (corrigida, sem uuid)

import os
import json
import uuid
import time as _time  # módulo time (o 'time' de datetime abaixo é a CLASSE, não colidir)
import logging
import jwt  # PyJWT — validação LOCAL do JWT do Supabase (sem bater no Auth remoto)
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
# ATENÇÃO: existem DOIS clientes propositalmente separados.
#   `supabase`       -> uso geral (dados via PostgREST). Também é usado para
#                       sign_in_with_password / sign_up de usuários, o que
#                       SOBRESCREVE a sessão interna do cliente pela do usuário
#                       logado. Por isso ele NÃO pode ser usado para operações
#                       de admin (auth.admin.*), senão herda o token do último
#                       usuário logado -> erro "not_admin" / "session_not_found".
#   `supabase_admin` -> instância dedicada, service_role, que NUNCA faz
#                       sign_in/sign_up. Use SEMPRE este para auth.admin.*
#                       (delete_user, update_user_by_id, invite_user_by_email…).
supabase: Optional[Client] = None
supabase_admin: Optional[Client] = None
try:
    SUPABASE_URL = os.environ.get("SUPABASE_URL")
    SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise ValueError("SUPABASE_URL e SUPABASE_SERVICE_KEY são obrigatórias.")
    supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    supabase_admin = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    logger.info("✅ Supabase client inicializado (geral + admin dedicado).")
except Exception as e:
    logger.error(f"❌ Falha ao inicializar Supabase: {e}")
    supabase = None
    supabase_admin = None


# --- DB ---
# Timeouts defensivos de conexão. SEM eles, uma conexão/consulta travada (TCP
# meio-aberto na latência cross-continente Oregon<->São Paulo, ou o SELECT do
# keep-alive) segura o worker gevent único e o Render mata por WORKER
# TIMEOUT/OOM — derrubando TODA a API (incidente 2026-07-11).
#   keepalives*      -> detectam socket morto e abortam em ~1min
#   connect_timeout  -> evita connect pendurado
#   statement_timeout-> aborta query longa no servidor (30s)
_DB_TCP_KWARGS = dict(
    connect_timeout=10,
    keepalives=1,
    keepalives_idle=30,
    keepalives_interval=10,
    keepalives_count=5,
)


def connect_hardened(url):
    """Abre uma conexão psycopg2 com os timeouts defensivos acima.

    LEVANTA em falha total (igual ao psycopg2.connect puro) — use onde o
    chamador espera uma conexão de verdade e trata a exceção (ex.: o
    DB_CONN_FACTORY da gamificação). Para o caminho que devolve None em vez
    de levantar, use get_db_connection()."""
    try:
        conn = psycopg2.connect(url, options="-c statement_timeout=30000", **_DB_TCP_KWARGS)
    except Exception as e_opt:
        # Alguns poolers (pgbouncer transaction mode) rejeitam 'options' no
        # startup — cai pra conexão sem statement_timeout, mas ainda com os
        # timeouts de TCP (que são o essencial contra o socket travado).
        logger.warning(f"⚠️ DB connect com statement_timeout falhou ({e_opt}); tentando sem.")
        conn = psycopg2.connect(url, **_DB_TCP_KWARGS)
    try:
        register_uuid(None, conn)  # garante suporte a UUID no cursor
    except Exception as e:
        logger.warning(f"⚠️ register_uuid falhou: {e}")
    return conn


def get_db_connection():
    url = os.environ.get("DATABASE_URL")
    if not url:
        logger.error("❌ DATABASE_URL não encontrada.")
        return None
    try:
        return connect_hardened(url)
    except Exception as e:
        logger.error(f"❌ Conexão DB falhou: {e}", exc_info=True)
        return None


# --- Validação LOCAL do JWT (corta a ida-e-volta cross-continente do Auth) ---
# O Supabase assina os access tokens com este segredo (HS256). Dashboard →
# Settings → API → JWT Settings → "JWT Secret". Com ele setado, validamos o
# token localmente (assinatura + expiração), SEM chamar supabase.auth.get_user()
# (um HTTP pro Auth em São Paulo) a CADA request autenticado. Sem o segredo, o
# código cai no caminho remoto de antes — então é seguro subir antes de configurar.
# Candidatos a segredo do JWT do Supabase, tentados em ordem. Aceita
# SUPABASE_JWT_SECRET (nome dedicado) OU o JWT_SECRET que já pode existir no
# Render — MAS SÓ funciona se esse valor for de fato o "JWT Secret" do Supabase
# (Dashboard → Settings → API → JWT Settings). ⚠️ O JWT_SECRET também vira o
# Flask SECRET_KEY (main.py); se ele for um valor próprio/aleatório (NÃO o do
# Supabase), a validação local só falha e cai no remoto — sem quebrar, mas sem
# ganho. O log de 1ª validação abaixo confirma qual caso é o real.
_JWT_SECRET_CANDIDATES = [s for s in (
    os.environ.get("SUPABASE_JWT_SECRET"),
    os.environ.get("JWT_SECRET"),
) if s]
_jwt_local_logged = {"ok": False, "fail": False}

if _JWT_SECRET_CANDIDATES:
    logger.info("✅ Segredo(s) de JWT presente(s) — tentando validação de token LOCAL (confirmar no log de 1ª validação).")
else:
    logger.warning("⚠️ Sem segredo de JWT — validando token via Auth REMOTO (mais lento).")


def _verify_jwt_local(token):
    """Valida o JWT do Supabase localmente (HS256 + exp), sem rede.

    Tenta cada segredo candidato. Retorna o user_id (claim 'sub') em caso de
    sucesso, ou None se não der pra validar localmente (sem segredo, assinatura
    inválida com todos, expirado, sem 'sub'/'exp', audience diferente) — aí o
    chamador cai no Auth remoto, que é autoritativo. Nunca levanta."""
    if not _JWT_SECRET_CANDIDATES or not token:
        return None
    for secret in _JWT_SECRET_CANDIDATES:
        try:
            claims = jwt.decode(
                token, secret,
                algorithms=["HS256"],
                audience="authenticated",
                options={"require": ["exp", "sub"]},
            )
            sub = claims.get("sub")
            if sub:
                if not _jwt_local_logged["ok"]:
                    _jwt_local_logged["ok"] = True
                    logger.info("🔓 Validação de token LOCAL funcionando (segredo do Supabase correto). Latência de auth cortada.")
                return str(sub)
        except jwt.ExpiredSignatureError:
            return None  # assinatura ok mas expirou — deixa o remoto rejeitar
        except Exception:
            continue  # este candidato não bate — tenta o próximo
    if not _jwt_local_logged["fail"]:
        _jwt_local_logged["fail"] = True
        logger.warning("⚠️ Token não validou com NENHUM segredo local — caindo no Auth remoto. Se isto persistir, o JWT_SECRET do Render NÃO é o JWT Secret do Supabase: setar SUPABASE_JWT_SECRET com o valor do Dashboard → Settings → API.")
    return None


# Cache em memória do user_type por user_id. O user_type é praticamente imutável
# (client/restaurant/delivery/admin), então cachear por alguns minutos elimina a
# consulta a public.users em QUASE todo request autenticado. Processo único
# (gunicorn -w 1 -k gevent) → dict simples é seguro (gevent é cooperativo, sem
# preempção no meio de um acesso). Só guarda acertos; miss/403 não são cacheados.
_USER_TYPE_TTL = 300  # segundos
_user_type_cache = {}  # user_id -> (user_type, expira_em_monotonic)


def _cached_user_type(user_id):
    hit = _user_type_cache.get(user_id)
    if hit and hit[1] > _time.monotonic():
        return hit[0]
    return None


def _store_user_type(user_id, user_type):
    if user_id and user_type:
        _user_type_cache[user_id] = (user_type, _time.monotonic() + _USER_TYPE_TTL)


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
        # 1) Tenta validar o JWT LOCALMENTE (rápido, sem rede). 2) Se não rolar
        #    (sem segredo, expirado, etc.), cai no Auth REMOTO do Supabase, que é
        #    autoritativo mas cross-continente. O caminho local corta uma
        #    ida-e-volta a São Paulo de todo request autenticado.
        user_id = _verify_jwt_local(token)
        if not user_id:
            if not supabase:
                raise RuntimeError("Supabase client não inicializado.")
            user_resp = supabase.auth.get_user(token)
            user = getattr(user_resp, "user", None)
            if not user:
                return None, None, (jsonify({"error": "Token inválido ou expirado"}), 401)
            user_id = str(user.id)

        # Cache: user_type quase nunca muda; se em cache, não toca o banco.
        cached_type = _cached_user_type(user_id)
        if cached_type:
            return user_id, cached_type, None

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

        _store_user_type(user_id, row["user_type"])
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


def get_user_info():
    """
    Extrai email/id do usuário autenticado a partir do header Authorization
    da requisição Flask atual (contexto ambiente, sem precisar passá-lo).
    Usado pelo audit log (best-effort) para saber qual admin fez a ação.
    """
    from flask import request as _request
    token = _extract_bearer_token(_request.headers.get("Authorization"))
    if not token or not supabase:
        return None
    try:
        user_resp = supabase.auth.get_user(token)
        user = getattr(user_resp, "user", None)
        if not user:
            return None
        return {"id": str(user.id), "email": user.email}
    except Exception:
        return None


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
