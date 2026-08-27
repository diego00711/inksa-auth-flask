# src/routes/social_admin.py
# CRUD (admin) do historico de eventos do Dia I (Inksa Social).
# O historico alimenta a pagina publica de prestacao de contas (/dia-i na landing)
# via GET /api/public/social-day/history.

import logging
from functools import wraps

import psycopg2.extras
from flask import Blueprint, jsonify, request

from ..utils.helpers import get_db_connection, get_user_id_from_token

logger = logging.getLogger(__name__)
social_admin_bp = Blueprint("social_admin_bp", __name__)


def _admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        _uid, user_type, err = get_user_id_from_token(request.headers.get("Authorization"))
        if err:
            return err
        if user_type != "admin":
            return jsonify({"error": "Acesso não autorizado"}), 403
        return fn(*args, **kwargs)
    return wrapper


def _row_to_event(r):
    return {
        "id": str(r["id"]),
        "date": r["event_date"].isoformat() if r["event_date"] else None,
        "start_time": r["start_time"],
        "end_time": r["end_time"],
        "raised": float(r["raised"] or 0),
        "orders_count": int(r["orders_count"] or 0),
        "destination": r["destination"] or "",
        "proof_url": r["proof_url"] or "",
    }


@social_admin_bp.post("/events")
@_admin_required
def create_event():
    """Registra um Dia I no historico (normalmente com os numeros do painel ao vivo)."""
    data = request.get_json(silent=True) or {}
    event_date = (data.get("date") or "").strip()
    if not event_date:
        return jsonify({"error": "Campo 'date' é obrigatório (YYYY-MM-DD)"}), 400
    try:
        raised = round(max(float(data.get("raised") or 0), 0), 2)
        orders_count = max(int(data.get("orders_count") or 0), 0)
    except (TypeError, ValueError):
        return jsonify({"error": "Valores numéricos inválidos"}), 400

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "DB indisponível"}), 503
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                """
                INSERT INTO social_day_events
                    (event_date, start_time, end_time, raised, orders_count, destination, proof_url)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    event_date,
                    (data.get("start_time") or "").strip() or None,
                    (data.get("end_time") or "").strip() or None,
                    raised,
                    orders_count,
                    (data.get("destination") or "").strip() or None,
                    (data.get("proof_url") or "").strip() or None,
                ),
            )
            row = cur.fetchone()
        conn.commit()
        return jsonify({"data": _row_to_event(row)}), 201
    except Exception:
        conn.rollback()
        logger.exception("create_event (social) falhou")
        return jsonify({"error": "Erro interno"}), 500
    finally:
        conn.close()


@social_admin_bp.put("/events/<uuid:event_id>")
@_admin_required
def update_event(event_id):
    """Edita um evento do historico (destino da doação, link, valores)."""
    data = request.get_json(silent=True) or {}
    allowed = {}
    if "destination" in data:
        allowed["destination"] = (data.get("destination") or "").strip() or None
    if "proof_url" in data:
        allowed["proof_url"] = (data.get("proof_url") or "").strip() or None
    if "raised" in data:
        try:
            allowed["raised"] = round(max(float(data.get("raised") or 0), 0), 2)
        except (TypeError, ValueError):
            return jsonify({"error": "Valor inválido em 'raised'"}), 400
    if "orders_count" in data:
        try:
            allowed["orders_count"] = max(int(data.get("orders_count") or 0), 0)
        except (TypeError, ValueError):
            return jsonify({"error": "Valor inválido em 'orders_count'"}), 400
    if not allowed:
        return jsonify({"error": "Nenhum campo válido para atualizar"}), 400

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "DB indisponível"}), 503
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            sets = ", ".join(f"{k} = %s" for k in allowed)
            cur.execute(
                f"UPDATE social_day_events SET {sets}, updated_at = NOW() WHERE id = %s RETURNING *",
                (*allowed.values(), str(event_id)),
            )
            row = cur.fetchone()
        conn.commit()
        if not row:
            return jsonify({"error": "Evento não encontrado"}), 404
        return jsonify({"data": _row_to_event(row)}), 200
    except Exception:
        conn.rollback()
        logger.exception("update_event (social) falhou")
        return jsonify({"error": "Erro interno"}), 500
    finally:
        conn.close()


@social_admin_bp.delete("/events/<uuid:event_id>")
@_admin_required
def delete_event(event_id):
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "DB indisponível"}), 503
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM social_day_events WHERE id = %s", (str(event_id),))
            deleted = cur.rowcount
        conn.commit()
        if not deleted:
            return jsonify({"error": "Evento não encontrado"}), 404
        return jsonify({"message": "Evento removido"}), 200
    except Exception:
        conn.rollback()
        logger.exception("delete_event (social) falhou")
        return jsonify({"error": "Erro interno"}), 500
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Indicações: quem a cidade acha que deve receber o lucro do Dia I
# ---------------------------------------------------------------------------
#
# A janela de indicação é uma CHAVE PRÓPRIA, não a fase do evento. O Diego abre
# cerca de um mês antes de propósito: quando o Dia I chegar, o destino já tem
# que estar decidido, com tempo de falar com a instituição e combinar a
# entrega. Amarrar isso ao "está acontecendo agora" deixaria a escolha pro dia
# do evento — que é exatamente tarde demais.
#
# Chave: social_nominations_open ('true' | 'false') em platform_settings.

_CHAVE_ABERTA = "social_nominations_open"


def _chave_do_nome(nome: str) -> str:
    """Agrupa 'Lar dos Idosos', 'lar dos idosos' e 'Lar  dos  Idosos'.

    Sem normalizar, o ranking nunca sobe: cada jeito de escrever vira uma
    linha de um voto só, e o número é a única coisa que faz esta tabela
    valer alguma coisa.
    """
    import re
    import unicodedata
    s = unicodedata.normalize("NFKD", nome or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9 ]", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


@social_admin_bp.get("/nominations")
@_admin_required
def listar_indicacoes():
    """Ranking das indicações + se a janela está aberta.

    Traz o `motivo` de cada pessoa junto, não só a contagem. Numa escolha
    dessas o texto costuma decidir mais que o número: "a creche da minha rua
    ficou sem gás" convence de um jeito que sete votos anônimos não convencem.
    """
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Banco indisponível"}), 503
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT value FROM platform_settings WHERE key = %s", (_CHAVE_ABERTA,))
            r = cur.fetchone()
            aberta = bool(r and (r["value"] or "").strip().lower() == "true")

            cur.execute("""
                SELECT n.nome_chave,
                       MIN(n.nome)                AS nome,
                       count(*)::int              AS votos,
                       max(n.created_at)          AS ultimo,
                       bool_or(n.escolhida_em IS NOT NULL) AS escolhida,
                       (array_agg(n.contato) FILTER (WHERE n.contato IS NOT NULL))[1] AS contato,
                       array_remove(array_agg(n.motivo ORDER BY n.created_at DESC), NULL) AS motivos,
                       array_agg(DISTINCT n.user_type) AS tipos
                  FROM social_nominations n
                 GROUP BY n.nome_chave
                 ORDER BY count(*) DESC, max(n.created_at) DESC
                 LIMIT 200
            """)
            linhas = []
            for row in cur.fetchall():
                d = dict(row)
                d["ultimo"] = d["ultimo"].isoformat() if d.get("ultimo") else None
                # Só os 5 motivos mais recentes: a tela mostra uma amostra, e
                # uma indicação com 80 votos traria 80 textos pro navegador.
                d["motivos"] = (d.get("motivos") or [])[:5]
                linhas.append(d)

        return jsonify({"status": "success", "aberta": aberta, "indicacoes": linhas}), 200
    except Exception:
        logger.exception("Erro ao listar indicações do Dia I")
        return jsonify({"error": "Erro ao listar indicações"}), 500
    finally:
        try:
            conn.close()
        except Exception:
            pass


@social_admin_bp.post("/nominations/abrir")
@_admin_required
def abrir_indicacoes():
    """Liga ou desliga a caixa de indicação nos três apps."""
    body = request.get_json(silent=True) or {}
    aberta = bool(body.get("aberta", True))

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Banco indisponível"}), 503
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO platform_settings (key, value, label, category, type, updated_at)
                VALUES (%s, %s, 'Indicações do Dia I abertas', 'social', 'boolean', NOW())
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
            """, (_CHAVE_ABERTA, "true" if aberta else "false"))
        conn.commit()
        return jsonify({
            "status": "success",
            "aberta": aberta,
            "message": "Indicações abertas nos apps." if aberta else "Indicações fechadas.",
        }), 200
    except Exception:
        logger.exception("Erro ao abrir/fechar indicações")
        try:
            conn.rollback()
        except Exception:
            pass
        return jsonify({"error": "Erro ao salvar"}), 500
    finally:
        try:
            conn.close()
        except Exception:
            pass


@social_admin_bp.post("/nominations/escolher")
@_admin_required
def escolher_indicacao():
    """Marca a instituição escolhida (ou desmarca).

    Marcar não fecha a votação nem apaga as outras: serve pra saber, meses
    depois, qual indicação virou o destino daquele Dia I. O valor doado
    continua sendo registrado no evento, em social_day_events.destination.
    """
    body = request.get_json(silent=True) or {}
    chave = (body.get("nome_chave") or "").strip()
    if not chave:
        return jsonify({"error": "nome_chave é obrigatório"}), 400
    escolhida = bool(body.get("escolhida", True))

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Banco indisponível"}), 503
    try:
        with conn.cursor() as cur:
            if escolhida:
                # Só uma por vez: escolher a segunda tira a marca da primeira,
                # senão a tela mostra duas escolhidas e ninguém sabe qual vale.
                cur.execute("UPDATE social_nominations SET escolhida_em = NULL "
                            "WHERE escolhida_em IS NOT NULL")
            cur.execute(
                "UPDATE social_nominations "
                "   SET escolhida_em = CASE WHEN %s THEN NOW() ELSE NULL END "
                " WHERE nome_chave = %s",
                (escolhida, chave),
            )
            n = cur.rowcount
        conn.commit()
        return jsonify({"status": "success", "atualizadas": n}), 200
    except Exception:
        logger.exception("Erro ao escolher indicação")
        try:
            conn.rollback()
        except Exception:
            pass
        return jsonify({"error": "Erro ao salvar"}), 500
    finally:
        try:
            conn.close()
        except Exception:
            pass


@social_admin_bp.route("/nominations/enviar", methods=["POST", "OPTIONS"])
def enviar_indicacao():
    """A pessoa indica. Cliente, parceiro OU entregador — os três podem.

    Fica no blueprint do Social porque o assunto é o mesmo, mas NÃO usa o
    @_admin_required: aqui basta estar logado, de qualquer app.
    """
    if request.method == "OPTIONS":
        return jsonify({}), 204

    user_id, user_type, err = get_user_id_from_token(request.headers.get("Authorization"))
    if err:
        return err

    body = request.get_json(silent=True) or {}
    nome = (body.get("nome") or "").strip()[:140]
    motivo = (body.get("motivo") or "").strip()[:400] or None
    contato = (body.get("contato") or "").strip()[:120] or None

    if len(nome) < 3:
        return jsonify({"error": "Escreva o nome da instituição."}), 400
    chave = _chave_do_nome(nome)
    if not chave:
        return jsonify({"error": "Escreva o nome da instituição."}), 400

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Serviço indisponível no momento."}), 503
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # A janela é conferida NO SERVIDOR. O app esconder a caixa é
            # conveniência; se a checagem morasse só lá, bastava manter a tela
            # aberta pra continuar votando depois de fechada.
            cur.execute("SELECT value FROM platform_settings WHERE key = %s", (_CHAVE_ABERTA,))
            r = cur.fetchone()
            if not (r and (r["value"] or "").strip().lower() == "true"):
                return jsonify({"error": "As indicações estão fechadas no momento."}), 409

            # UMA INDICAÇÃO POR PESSOA (não uma por instituição). Índice único
            # em user_id: quem indica de novo TROCA a sua, não soma outra.
            #
            # Trocar é permitido de propósito. Quem digitou o nome errado ou
            # mudou de ideia não pode ficar preso pra sempre a uma indicação
            # errada — o que não pode é uma pessoa valer por cinco.
            #
            # O motivo é substituído inteiro, inclusive por vazio: se a pessoa
            # trocou de instituição, o motivo antigo era sobre a outra e
            # mantê-lo colaria a justificativa de uma no nome da outra.
            cur.execute("""
                INSERT INTO social_nominations (user_id, user_type, nome, nome_chave, motivo, contato)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE
                   SET nome       = EXCLUDED.nome,
                       nome_chave = EXCLUDED.nome_chave,
                       motivo     = EXCLUDED.motivo,
                       contato    = COALESCE(EXCLUDED.contato, social_nominations.contato),
                       user_type  = EXCLUDED.user_type,
                       created_at = NOW()
            """, (user_id, user_type or "cliente", nome, chave, motivo, contato))

            cur.execute("SELECT count(*)::int AS n FROM social_nominations WHERE nome_chave = %s",
                        (chave,))
            n = int((cur.fetchone() or {}).get("n") or 1)
        conn.commit()
        return jsonify({"status": "ok", "votos": n, "nome": nome}), 201
    except Exception:
        logger.exception("Indicação do Dia I falhou")
        try:
            conn.rollback()
        except Exception:
            pass
        return jsonify({"error": "Não consegui registrar agora. Tente de novo."}), 500
    finally:
        try:
            conn.close()
        except Exception:
            pass


@social_admin_bp.route("/nominations/minha", methods=["GET", "OPTIONS"])
def minha_indicacao():
    """O que ESTA pessoa já indicou, se indicou.

    Existe por causa da regra de um voto por pessoa. Sem isto, quem já votou
    reabre a caixa e vê um formulário vazio — parece que o voto não foi
    registrado, e a pessoa manda de novo achando que a primeira falhou. O app
    precisa poder dizer "você indicou X" antes de oferecer o formulário.
    """
    if request.method == "OPTIONS":
        return jsonify({}), 204

    user_id, _utype, err = get_user_id_from_token(request.headers.get("Authorization"))
    if err:
        return err

    conn = get_db_connection()
    if not conn:
        return jsonify({"minha": None}), 200
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                "SELECT nome, nome_chave, motivo FROM social_nominations WHERE user_id = %s",
                (user_id,),
            )
            r = cur.fetchone()
            if not r:
                return jsonify({"minha": None}), 200
            cur.execute("SELECT count(*)::int AS n FROM social_nominations WHERE nome_chave = %s",
                        (r["nome_chave"],))
            n = int((cur.fetchone() or {}).get("n") or 1)
        return jsonify({"minha": {"nome": r["nome"], "motivo": r["motivo"], "votos": n}}), 200
    except Exception:
        logger.exception("Erro ao ler indicação da pessoa")
        # Falha aqui não pode travar a caixa: sem resposta, o app mostra o
        # formulário normal e o servidor continua garantindo o voto único.
        return jsonify({"minha": None}), 200
    finally:
        try:
            conn.close()
        except Exception:
            pass


@social_admin_bp.route("/nominations/lista", methods=["GET", "OPTIONS"])
def lista_publica_indicacoes():
    """Instituições já indicadas — só nome e quantos votos. Para o app.

    POR QUE EXISTE: `_chave_do_nome` agrupa maiúscula, acento e pontuação, mas
    não agrupa REDAÇÃO diferente. "Lar São Vicente", "Lar de Idosos São
    Vicente" e "Asilo São Vicente" viram três linhas de um voto cada, e o
    ranking — que é a única coisa que faz a votação valer — nunca sobe.
    Mostrar a lista antes de digitar resolve na origem: a pessoa reconhece a
    que já está lá e clica, em vez de inventar um jeito novo de escrever.

    NÃO devolve `motivo` nem `contato`, de propósito, mesmo tendo os dois na
    tabela. O motivo é o texto que a pessoa escreveu achando que ia para a
    Inksa decidir, não para a cidade ler; e o contato é telefone de uma
    instituição, que não tem por que circular num app de delivery. A lista
    administrativa (GET /nominations, com _admin_required) continua trazendo
    tudo — lá o público é outro.
    """
    if request.method == "OPTIONS":
        return jsonify({}), 204

    # Exige estar logado: a lista não é segredo, mas é resultado parcial de uma
    # votação em curso. Aberta a anônimo, vira alvo fácil de raspagem e de
    # empurrão coordenado numa instituição.
    _uid, _utype, err = get_user_id_from_token(request.headers.get("Authorization"))
    if err:
        return err

    conn = get_db_connection()
    if not conn:
        # Falha aqui não pode travar a caixa de indicação: sem lista, o
        # formulário continua funcionando do jeito de sempre.
        return jsonify({"itens": []}), 200
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                SELECT MIN(nome)      AS nome,
                       count(*)::int  AS votos
                  FROM social_nominations
                 GROUP BY nome_chave
                 ORDER BY count(*) DESC, MIN(nome) ASC
                 LIMIT 100
            """)
            itens = [{"nome": r["nome"], "votos": int(r["votos"])} for r in cur.fetchall()]
        return jsonify({"itens": itens}), 200
    except Exception:
        logger.exception("Erro ao listar indicações para o app")
        return jsonify({"itens": []}), 200
    finally:
        try:
            conn.close()
        except Exception:
            pass


@social_admin_bp.delete("/nominations")
@_admin_required
def limpar_indicacoes():
    """Zera as indicações pro próximo ciclo.

    APAGA MESMO, e não tem volta. É o que o Diego pediu, e faz sentido: a
    pergunta "quem deve receber o lucro do PRÓXIMO Dia I" não pode carregar os
    votos do anterior — quem votou há três meses já foi atendido.

    O que se perde é a lista bruta de votos. O RESULTADO não se perde: a
    instituição escolhida fica registrada no evento, em
    social_day_events.destination, que é o que alimenta a página pública de
    prestação de contas. Por isso limpar é seguro DEPOIS de registrar o
    evento, e perigoso antes.
    """
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Banco indisponível"}), 503
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM social_nominations")
            n = cur.rowcount
        conn.commit()
        logger.info("[SOCIAL] indicações limpas: %d linha(s)", n)
        return jsonify({
            "status": "success",
            "apagadas": n,
            "message": f"{n} indicação(ões) apagada(s).",
        }), 200
    except Exception:
        logger.exception("Erro ao limpar indicações")
        try:
            conn.rollback()
        except Exception:
            pass
        return jsonify({"error": "Erro ao limpar"}), 500
    finally:
        try:
            conn.close()
        except Exception:
            pass
