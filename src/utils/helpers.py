# src/utils/helpers.py — VERSÃO ROBUSTA (corrigida, sem uuid)

import os
import json
import uuid
import time as _time  # módulo time (o 'time' de datetime abaixo é a CLASSE, não colidir)
import logging
import threading
import jwt  # PyJWT — validação LOCAL do JWT do Supabase (sem bater no Auth remoto)
import psycopg2
import psycopg2.extras
from psycopg2.extras import register_uuid
from flask import jsonify
from supabase import create_client, Client
from datetime import date, datetime, timedelta, time, timezone
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


# --- POOL de conexão (LIGADO por padrão; kill-switch via DB_POOL_ENABLED=0) ---
# Sem pool, cada request abre uma conexão NOVA (handshake TLS+auth ~4-5 RTT
# cross-continente Oregon<->SP = ~0,5-0,8s SÓ pra conectar). O pool reusa
# conexões, matando esse custo. Vem LIGADO; se der problema em produção,
# setar DB_POOL_ENABLED=0 no Render desliga na hora (reinicia o worker) sem
# novo deploy. E qualquer tropeço do pool cai sozinho pra conexão direta.
_POOL_ENABLED = os.environ.get("DB_POOL_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
_DB_POOL = None
_DB_POOL_LOCK = threading.Lock()

# TERMÔMETRO DO POOL. Medido em 06/09/2026: a rota mais barata que toca o banco
# nunca baixava de 0,81s, contra 0,29s de uma rota que não toca — ou seja, o
# custo de conexão de ~0,5s estava em TODA requisição, como se não houvesse
# pool. Só que o pool vem ligado por padrão. Ou o kill-switch está setado no
# Render, ou ele está falhando e caindo pra conexão direta — e a queda é um
# logger.warning que ninguém lê.
#
# Sem acesso ao painel, não dá pra saber qual dos dois. Então o backend passa a
# dizer, em /api/health. Isto é só leitura: não muda nenhum comportamento.
#
# ── POR QUE O TERMÔMETRO SEPARA "CHEIO" DE "QUEBRADO" (11/09/2026) ───────────
#
# Na primeira versão havia UM contador só (`quedas_para_direta`) e um
# `ultimo_erro` que nunca era limpo. O /api/health passou a mostrar:
#
#     quedas_para_direta: 37,  ultimo_erro: "connection pool exhausted"
#
# e isso me levou a diagnosticar VAZAMENTO DE CONEXÃO — errado. Medido: 90
# chamadas em sequência não moveram o contador (ninguém vaza); já 25 chamadas
# SIMULTÂNEAS moveram +13 e 40 moveram +28. Ou seja, exatamente `n - maxconn`:
# é lotação momentânea, não vazamento. E o `ultimo_erro` grudado fazia um pico
# de dez dias atrás parecer defeito de agora.
#
# Lotação é CONTRAPRESSÃO ESPERADA; pool quebrado é DEFEITO. Um instrumento que
# mistura os dois faz perder tempo procurando bug que não existe.
_POOL_STATUS = {
    "habilitado": _POOL_ENABLED,
    "criado": False,
    "serviu_conexao": False,
    # Mantido: é a soma dos dois abaixo (quem já lia continua lendo).
    "quedas_para_direta": 0,
    "quedas_por_lotacao": 0,   # pool cheio no instante do pico — normal
    "quedas_por_erro": 0,      # pool falhou de verdade — isto é defeito
    "ultima_queda_em": None,   # pra saber se foi AGORA ou semana passada
    "ultimo_erro": None,
}


def _anota_queda(motivo, erro=None):
    """Registra uma queda pra conexão direta, separando lotação de defeito."""
    _POOL_STATUS["quedas_para_direta"] += 1
    _POOL_STATUS["quedas_por_lotacao" if motivo == "lotacao" else "quedas_por_erro"] += 1
    _POOL_STATUS["ultima_queda_em"] = datetime.now(timezone.utc).isoformat()
    if erro is not None:
        _POOL_STATUS["ultimo_erro"] = str(erro)


def pool_status():
    """Cópia do termômetro do pool, pra expor em /api/health."""
    return dict(_POOL_STATUS)

# Registra o typecaster de UUID GLOBALMENTE (as conexões do pool não passam pelo
# connect_hardened, que registrava por-conexão). Idempotente.
try:
    register_uuid()
except Exception as _e_uuid:
    logger.warning(f"⚠️ register_uuid global falhou: {_e_uuid}")


def _get_pool(url):
    """Cria (uma vez) e devolve o ThreadedConnectionPool, ou None se falhar."""
    global _DB_POOL
    if _DB_POOL is not None:
        return _DB_POOL
    with _DB_POOL_LOCK:
        if _DB_POOL is not None:  # outro greenlet criou enquanto esperávamos
            return _DB_POOL
        from psycopg2 import pool as _pgpool
        maxc = int(os.environ.get("DB_POOL_MAXCONN", "12"))
        # Tenta com statement_timeout; se o servidor rejeitar 'options' no
        # startup, recria sem (mesma lógica do connect_hardened).
        for opts in ('-c statement_timeout=30000', None):
            try:
                kw = dict(_DB_TCP_KWARGS)
                if opts:
                    kw["options"] = opts
                _DB_POOL = _pgpool.ThreadedConnectionPool(1, maxc, dsn=url, **kw)
                # ── A LINHA QUE CONSERTA A SERIALIZAÇÃO (11/09/2026) ────────
                #
                # `minconn` no psycopg2 NÃO é "mínimo de conexões vivas": é
                # quantas o pool GUARDA. Em `_putconn` (pool.py:105):
                #
                #     if len(self._pool) < self.minconn and not close:
                #         self._pool.append(conn)   # guarda
                #     else:
                #         conn.close()              # JOGA FORA
                #
                # Com minconn=1 o pool guardava UMA conexão e fechava todas as
                # outras ao devolver. Numa rajada isso vira desastre, porque
                # `_getconn` cria a conexão que falta DENTRO do cadeado global
                # (pool.py:163 -> `self._connect(key)`), e conectar daqui até
                # São Paulo custa ~1s. Então 12 chamadas simultâneas viravam
                # uma FILA de 1s cada — medido: a 1ª pegava em 0,0ms e a 12ª
                # esperava 11,3s. E na volta 11 eram descartadas, então a
                # rajada seguinte repetia tudo: o pool nunca esquentava.
                #
                # Mexer em minconn DEPOIS de construído muda só a retenção —
                # ele só é lido em dois lugares, a criação inicial (pool.py:58,
                # já passou) e essa retenção. Ou seja: nasce com 1 conexão (boot
                # rápido, sem 12 handshakes travando o primeiro request) e passa
                # a guardar tudo o que criar sob demanda.
                _DB_POOL.minconn = maxc
                logger.info(f"✅ Pool de conexão DB criado (guarda até {maxc}, "
                            f"statement_timeout={'sim' if opts else 'nao'}).")
                _POOL_STATUS["criado"] = True
                return _DB_POOL
            except Exception as e:
                logger.warning(f"⚠️ Falha ao criar pool DB (opts={bool(opts)}): {e}")
                _POOL_STATUS["ultimo_erro"] = f"criar pool: {e}"
                _DB_POOL = None
        return None


class _PooledConn:
    """Proxy de conexão: comporta-se como uma conexão psycopg2, mas .close()
    DEVOLVE ao pool (limpando o estado) em vez de fechar. Assim nenhuma das ~157
    rotas precisa mudar — elas seguem chamando get_db_connection()/conn.close().
    """
    __slots__ = ("_real", "_pool", "_returned")

    def __init__(self, real, pool):
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_pool", pool)
        object.__setattr__(self, "_returned", False)

    def close(self):
        # "Fechar" = devolver ao pool. Idempotente (finally pode chamar 2x).
        if object.__getattribute__(self, "_returned"):
            return
        object.__setattr__(self, "_returned", True)
        real = object.__getattribute__(self, "_real")
        pool = object.__getattribute__(self, "_pool")
        try:
            if getattr(real, "closed", 1):
                pool.putconn(real, close=True)  # já fechada -> descarta
                return
            try:
                # Reseta o estado antes do próximo uso: encerra qualquer
                # transação aberta/abortada e tira autocommit (uma rota de
                # leitura liga autocommit; não pode vazar pra próxima).
                real.rollback()
                if getattr(real, "autocommit", False):
                    real.autocommit = False
            except Exception:
                pool.putconn(real, close=True)  # estado suspeito -> descarta
                return
            pool.putconn(real)
        except Exception:
            try:
                real.close()
            except Exception:
                pass

    # Tudo o mais (cursor, commit, rollback, closed, encoding, ...) delega ao real.
    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_real"), name)

    def __setattr__(self, name, value):
        setattr(object.__getattribute__(self, "_real"), name, value)

    def __enter__(self):
        # psycopg2: 'with conn:' gerencia transação (commit/rollback), NÃO fecha.
        object.__getattribute__(self, "_real").__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        return object.__getattribute__(self, "_real").__exit__(exc_type, exc, tb)


# Quanto esperar por uma vaga antes de desistir e abrir conexão direta.
#
# ⚠️ PADRÃO 0 (DESLIGADO) DE PROPÓSITO — leia antes de ligar.
#
# A espera parece obviamente boa: vaga volta em milissegundos, reconectar custa
# ~0,5-0,8s. Liguei com 1,5s, medi, e o contador de quedas caiu bonito (25
# chamadas simultâneas: 13 quedas -> 4). Só que contador não é o objetivo,
# LATÊNCIA é — e aí apareceu o problema de verdade (11/09/2026):
#
#   rota                    usa              sozinha   12 juntas   fator
#   /healthz                nada              0,48s      0,27s      ok
#   /api/health             HTTP do Supabase  1,44s      0,93s      ok
#   /api/gamification/...   conexão DIRETA    1,93s      1,89s      ok
#   /api/club/levels        O POOL            2,31s     11,94s     5,2x
#   /api/banners            O POOL            2,66s     12,12s     4,5x
#
# Quem usa o pool SERIALIZA; quem abre conexão direta não. E três rajadas
# seguidas deram o mesmo tempo — o pool não "esquenta", então não é só o custo
# de crescer. A causa ainda NÃO está identificada.
#
# Enquanto for assim, esperar por uma vaga do pool segura a requisição no
# caminho LENTO em vez de deixá-la escapar pra conexão direta, que é o caminho
# que paraleliza. Ou seja: ligar isto provavelmente PIORA a latência em pico,
# mesmo melhorando o contador. Não tenho medição de latência de antes da
# mudança pra provar o contrário, e chutar em produção não vale.
#
# O mecanismo fica pronto e testado. Liga com DB_POOL_WAIT_SECONDS=1.5 DEPOIS
# que a serialização estiver entendida e resolvida.
_POOL_ESPERA_S = float(os.environ.get("DB_POOL_WAIT_SECONDS", "0"))


def _pega_do_pool(pool):
    """Pega uma conexão do pool; se estiver cheio, ESPERA um pouco.

    ── POR QUE ESPERAR ──────────────────────────────────────────────────────
    `ThreadedConnectionPool.getconn()` não enfileira: cheio = estoura na hora.
    Cada estouro vira uma conexão NOVA, e conexão nova custa ~0,5-0,8s de
    handshake TLS+auth daqui pra SP (é o motivo de o pool existir). Ou seja, o
    comportamento antigo respondia ao pico do jeito mais caro possível.

    Uma consulta típica dura ~0,2s, então a vaga costuma voltar em
    milissegundos — esperar é quase sempre mais barato que reconectar.

    ── POR QUE ISTO NÃO TRAVA O WORKER ──────────────────────────────────────
    A API roda `gunicorn -w 1 -k gevent`, e o `main.py` chama `monkey.patch_all()`
    como PRIMEIRA linha (mais `psycogreen` pro psycopg2). Com isso o `sleep`
    abaixo CEDE o worker em vez de bloqueá-lo: os outros greenlets seguem
    rodando e devolvendo as conexões que esta espera precisa. Sem o
    monkey-patch isto seria o contrário — pararia a API inteira.
    ⚠️ Se um dia o worker deixar de ser gevent, reveja esta função.

    Devolve a conexão real, ou None se o tempo acabou (aí o chamador cai pra
    conexão direta, como sempre fez).
    """
    limite = _time.monotonic() + max(0.0, _POOL_ESPERA_S)
    while True:
        try:
            return pool.getconn()
        except Exception as e:
            # Só "pool cheio" vale esperar. Qualquer outro erro é defeito e
            # tem que subir na hora, sem mascarar atrás de uma espera.
            if "exhausted" not in str(e).lower():
                raise
            if _time.monotonic() >= limite:
                return None
            _time.sleep(0.02)


def get_db_connection():
    url = os.environ.get("DATABASE_URL")
    if not url:
        logger.error("❌ DATABASE_URL não encontrada.")
        return None
    # Caminho POOL (opt-in). Qualquer tropeço -> conexão direta (comportamento
    # de sempre), então ligar o pool nunca deixa a API sem saída.
    if _POOL_ENABLED:
        try:
            pool = _get_pool(url)
            if pool is not None:
                real = _pega_do_pool(pool)
                if real is not None:
                    _POOL_STATUS["serviu_conexao"] = True
                    return _PooledConn(real, pool)
                # Esperou a vaga e ela não veio: pico de verdade. Conexão
                # direta resolve a requisição; o contador de LOTAÇÃO registra.
                _anota_queda("lotacao")
            else:
                # Pool nem existe — isto sim é defeito, e é o caso mais
                # silencioso de todos (cai pra direta sem exceção nenhuma).
                _anota_queda("erro", "pool indisponível (não criado)")
        except Exception as e:
            logger.warning(f"⚠️ Pool indisponível ({e}); usando conexão direta.")
            _anota_queda("erro", e)
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


# ── API admin do GoTrue, sem passar pelo SDK ────────────────────────────────
# POR QUE ISTO EXISTE: `supabase_admin.auth.admin.*` NÃO é confiável neste
# backend. O cliente supabase-py guarda sessão internamente e, num processo
# único que atende todo mundo, ele acaba mandando o token do último usuário
# que fez login em vez da service_role. O GoTrue responde 403
# "this token needs to have one of the following roles: supabase_admin,
# service_role" e a operação falha — de forma INTERMITENTE, que é o pior tipo
# de falha: funciona no teste e quebra com usuário real.
#
# Já mordeu duas vezes:
#   • 2026-07-14 — exclusão de usuário no admin ("not_admin"/"bad_jwt").
#     Resolvido ali com requests direto (ver admin_users.py).
#   • 2026-08-27 — redefinição de senha. O sogro do Diego recebeu o e-mail,
#     abriu o link, e o POST /reset-password devolveu o erro genérico porque
#     o PUT em /admin/users/<id> voltou 403. Confirmado nos auth_logs do
#     Supabase às 00:13:42 e 00:14:08 UTC.
#
# Mandando a service_role EXPLICITAMENTE no header não existe sessão pra
# poluir. Use SEMPRE esta função para auth.admin.*; o SDK fica só para leitura.
def gotrue_admin(metodo, caminho, payload=None, timeout=10):
    """Chama /auth/v1/admin/<caminho> com a service_role no header.

    Devolve o objeto Response do requests. Levanta RuntimeError se faltar
    configuração — falhar alto aqui é melhor que devolver 403 silencioso.
    """
    import requests  # local: helpers é importado cedo, requests nem sempre é usado

    service_key = (os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
                   or os.environ.get("SUPABASE_SERVICE_KEY"))
    base_url = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
    if not service_key or not base_url:
        raise RuntimeError("SUPABASE_URL/SERVICE_KEY ausentes para chamada admin")

    return requests.request(
        metodo,
        f"{base_url}/auth/v1/admin/{caminho.lstrip('/')}",
        headers={
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=timeout,
    )
