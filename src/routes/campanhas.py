# -*- coding: utf-8 -*-
# src/routes/campanhas.py
"""DE ONDE VEM O CLIENTE — o funil de uma campanha.

Nasceu pra medir uma publi paga no Instagram, mas não tem nada do
influenciador aqui: o código é texto livre e a campanha passa a existir no
instante em que alguém clica num link que a carrega. Sem cadastro, sem
aprovação, sem CRUD. Inventa o código, põe no link (`?de=guga`), conta.

Ser geral em vez de "a campanha do fulano" não é generalização gratuita: 40
cadastros é bom? Só comparando com o que entra sozinho numa semana normal. O
balde "sem campanha" é metade da resposta, e ele só existe se tudo for medido.

TRÊS DEGRAUS, e a confiança cresce a cada um:

  1. clique   — rota PÚBLICA, sem login. Número mole: qualquer um pode mandar
                POST e inflar. Serve pro funil ter topo, não como prova.
  2. cadastro — exige conta. Carimbado no PERFIL, uma vez só.
  3. pedido   — exige pagamento. É o número que decide se a publi se pagou.

Quem paga publi olha o degrau 3. Os outros dois existem pra dizer ONDE o funil
vaza: muita chegada e pouco cadastro é problema da nossa tela; muito cadastro
e pouco pedido é problema de oferta.
"""
import logging
import re

from flask import Blueprint, jsonify, request

from ..utils.helpers import get_db_connection, get_user_id_from_token
from ..utils.decorators import admin_required
from src.extensions import limiter

logger = logging.getLogger(__name__)
campanhas_bp = Blueprint('campanhas', __name__)
campanhas_admin_bp = Blueprint('campanhas_admin', __name__)

# O que é um código aceitável. Estreito de propósito: esta lista alimenta uma
# rota pública, então sem isto a tela do admin viraria vitrine pra qualquer
# texto que um estranho resolvesse mandar. Minúsculas pra `guga`, `Guga` e
# `GUGA` serem a MESMA campanha — o link vai ser digitado errado, e três
# baldes pro mesmo influenciador é o mesmo que não medir.
_FORMATO = re.compile(r'^[a-z0-9][a-z0-9_-]{1,31}$')


def normalizar(bruto):
    """Devolve o código limpo, ou None se não serve."""
    c = (bruto or '').strip().lower()
    if not _FORMATO.match(c):
        return None
    return c


@campanhas_bp.post('/clique')
@limiter.limit("30 per minute")
def registrar_clique():
    """Alguém abriu um link de campanha. Sem login, sem corpo obrigatório.

    ⚠️ Responde 204 SEMPRE, inclusive quando o código não serve e quando o
    banco está fora. Isto é telemetria pendurada no carregamento do app do
    cliente: se um erro daqui pudesse aparecer pra quem chegou pela publi, a
    medição estaria atrapalhando exatamente a visita que ela existe pra contar.
    O preço é que falha some — por isso o log.
    """
    try:
        dados = request.get_json(silent=True) or {}
        codigo = normalizar(dados.get('codigo'))
        if not codigo:
            return '', 204

        conn = get_db_connection()
        if not conn:
            return '', 204
        try:
            with conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO campanha_cliques (codigo) VALUES (%s)", (codigo,)
                )
        finally:
            conn.close()
    except Exception:
        logger.exception("campanha: falhei ao registrar clique (seguindo em silêncio)")
    return '', 204


@campanhas_bp.post('/atribuir')
def atribuir():
    """Carimba a campanha no perfil de quem acabou de entrar.

    ⚠️ `WHERE origem_campanha IS NULL`: carimba uma vez e nunca mais. Quem
    chegou pelo Guga e depois tocar num link de rádio continua sendo do Guga —
    quem trouxe, trouxe. Sem essa cláusula a última campanha rouba o crédito
    de todas as anteriores, que é o jeito mais fácil de medir errado.

    Os códigos de status importam: o app do cliente só APAGA o código guardado
    diante de uma resposta de negócio. 404 aqui é normal no primeiro acesso (o
    perfil ainda não existe — a conta entra antes do perfil aparecer), e o app
    tenta de novo na próxima abertura em vez de perder a atribuição.
    """
    auth_uid, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
    if error:
        return error
    if user_type != 'client':
        # Não é erro do usuário, é campanha que só faz sentido pra cliente.
        return jsonify({"ok": False, "motivo": "nao_e_cliente"}), 200

    codigo = normalizar((request.get_json(silent=True) or {}).get('codigo'))
    if not codigo:
        return jsonify({"ok": False, "motivo": "codigo_invalido"}), 200

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "DB indisponível"}), 503
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE client_profiles
                      SET origem_campanha = %s
                    WHERE user_id = %s
                      AND origem_campanha IS NULL
                RETURNING id""",
                (codigo, auth_uid),
            )
            carimbou = cur.fetchone() is not None
            if not carimbou:
                # Ou o perfil não existe ainda, ou já tinha origem. São coisas
                # muito diferentes pro app: a primeira é "pergunte de novo", a
                # segunda é "pronto, pode esquecer o código".
                cur.execute(
                    "SELECT 1 FROM client_profiles WHERE user_id = %s", (auth_uid,)
                )
                if cur.fetchone() is None:
                    return jsonify({"ok": False, "motivo": "perfil_ainda_nao_existe"}), 404
        return jsonify({"ok": True, "novo": carimbou}), 200
    except Exception:
        logger.exception("campanha: falhei ao atribuir origem")
        return jsonify({"error": "Erro interno"}), 500
    finally:
        conn.close()


# O funil inteiro numa consulta.
#
# FULL JOIN porque os dois lados existem sozinhos: campanha que teve clique e
# nenhum cadastro (o link circulou e ninguém se interessou) e campanha com
# cadastro e zero clique (o link foi copiado sem o `?de=`, ou o clique caiu).
# Com INNER ou LEFT, um dos dois casos sumiria da tela — e são justamente os
# dois que contam uma história.
#
# A linha "sem campanha" vem à parte porque ela não é uma campanha: é a régua
# contra a qual todas as outras são lidas.
_SQL_FUNIL = """
WITH cliques AS (
    SELECT codigo, COUNT(*)::int AS cliques, MAX(criado_em) AS ultimo_clique
      FROM campanha_cliques
     WHERE criado_em >= NOW() - (%s || ' days')::interval
     GROUP BY codigo
),
clientes AS (
    SELECT cp.origem_campanha AS codigo,
           COUNT(DISTINCT cp.id)::int AS cadastros,
           COUNT(o.id)::int           AS pedidos,
           COALESCE(SUM(o.total_amount), 0)::numeric AS receita,
           COUNT(DISTINCT o.client_id)::int AS compradores
      FROM client_profiles cp
      LEFT JOIN orders o
             ON o.client_id = cp.id
            AND o.status NOT IN ('cancelled', 'canceled', 'rejected')
     WHERE cp.origem_campanha IS NOT NULL
     GROUP BY cp.origem_campanha
)
SELECT COALESCE(cl.codigo, ct.codigo)        AS codigo,
       COALESCE(cl.cliques, 0)               AS cliques,
       COALESCE(ct.cadastros, 0)             AS cadastros,
       COALESCE(ct.compradores, 0)           AS compradores,
       COALESCE(ct.pedidos, 0)               AS pedidos,
       COALESCE(ct.receita, 0)               AS receita,
       cl.ultimo_clique
  FROM cliques cl
  FULL JOIN clientes ct ON ct.codigo = cl.codigo
 ORDER BY COALESCE(ct.receita, 0) DESC,
          COALESCE(ct.cadastros, 0) DESC,
          COALESCE(cl.cliques, 0) DESC
"""

_SQL_ORGANICO = """
SELECT COUNT(DISTINCT cp.id)::int           AS cadastros,
       COUNT(o.id)::int                     AS pedidos,
       COALESCE(SUM(o.total_amount), 0)::numeric AS receita,
       COUNT(DISTINCT o.client_id)::int     AS compradores
  FROM client_profiles cp
  LEFT JOIN orders o
         ON o.client_id = cp.id
        AND o.status NOT IN ('cancelled', 'canceled', 'rejected')
 WHERE cp.origem_campanha IS NULL
"""


@campanhas_admin_bp.get('')
@campanhas_admin_bp.get('/')
@admin_required
def funil():
    """Funil por campanha + a régua orgânica.

    `dias` filtra só os CLIQUES, não os cadastros e pedidos. É de propósito: a
    publi acontece num dia e os pedidos dela caem ao longo de semanas. Cortar a
    receita pela mesma janela do clique faria toda campanha parecer pior quanto
    mais recente fosse a consulta.
    """
    try:
        dias = max(1, min(int(request.args.get('dias', 90)), 730))
    except (TypeError, ValueError):
        dias = 90

    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "DB indisponível"}), 503
    try:
        import psycopg2.extras
        with conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(_SQL_FUNIL, (str(dias),))
            campanhas = []
            for r in cur.fetchall():
                d = dict(r)
                d['receita'] = float(d['receita'] or 0)
                d['ultimo_clique'] = (d['ultimo_clique'].isoformat()
                                      if d.get('ultimo_clique') else None)
                campanhas.append(d)
            cur.execute(_SQL_ORGANICO)
            org = dict(cur.fetchone())
            org['receita'] = float(org['receita'] or 0)
        return jsonify({
            "status": "success",
            "data": {"dias": dias, "campanhas": campanhas, "organico": org},
        }), 200
    except Exception:
        logger.exception("campanha: falhei ao montar o funil")
        return jsonify({"status": "error", "message": "Erro interno"}), 500
    finally:
        conn.close()
