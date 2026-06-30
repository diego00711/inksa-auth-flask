# src/routes/support.py
# Sistema de tickets de suporte:
#  - Usuario logado (qualquer tipo) cria/lista seus tickets
#  - Admin lista/responde/muda status de todos
#  - Mensagens trafegam pelos mesmos tickets

import logging
import psycopg2.extras
from flask import Blueprint, jsonify, request

from ..utils.helpers import get_db_connection, get_user_id_from_token

logger = logging.getLogger(__name__)
support_bp = Blueprint("support_bp", __name__)

VALID_STATUS = {"aberto", "aguardando", "andamento", "resolvido"}
VALID_PRIORITY = {"Baixo", "Médio", "Alto", "Crítico"}
VALID_CATEGORY = {"Dúvida", "Pagamento", "Pedido", "Entrega", "Cardápio", "Conta", "Técnico", "Outro"}


def _auth_required():
    user_id, user_type, err = get_user_id_from_token(request.headers.get("Authorization"))
    if err:
        return None, None, err
    return user_id, user_type, None


@support_bp.post("/tickets")
def create_ticket():
    user_id, user_type, err = _auth_required()
    if err:
        return err
    data = request.get_json() or {}
    subject = (data.get("subject") or "").strip()
    description = (data.get("description") or data.get("body") or "").strip()
    category = data.get("category") if data.get("category") in VALID_CATEGORY else "Dúvida"
    priority = data.get("priority") if data.get("priority") in VALID_PRIORITY else "Baixo"

    if not subject or not description:
        return jsonify({"status": "error", "message": "Assunto e descrição são obrigatórios"}), 400

    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "DB indisponível"}), 503
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                INSERT INTO support_tickets (user_id, user_type, subject, category, priority)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
            """, (user_id, user_type, subject, category, priority))
            ticket_id = cur.fetchone()["id"]
            cur.execute("""
                INSERT INTO support_messages (ticket_id, author_id, author_role, body)
                VALUES (%s, %s, %s, %s)
            """, (ticket_id, user_id, user_type, description))
        conn.commit()
        return jsonify({"status": "success", "data": {"id": str(ticket_id)}}), 201
    except Exception:
        logger.exception("Erro em create_ticket")
        conn.rollback()
        return jsonify({"status": "error", "message": "Erro ao criar ticket"}), 500
    finally:
        conn.close()


@support_bp.get("/tickets")
def list_my_tickets():
    user_id, user_type, err = _auth_required()
    if err:
        return err
    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "DB indisponível"}), 503
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            if user_type == "admin":
                cur.execute("""
                    SELECT t.id, t.user_id, t.user_type, t.subject, t.category, t.priority,
                           t.status, t.created_at, t.updated_at, t.closed_at,
                           (SELECT COUNT(*) FROM support_messages m WHERE m.ticket_id = t.id) AS messages_count
                    FROM support_tickets t
                    ORDER BY t.updated_at DESC
                    LIMIT 200
                """)
            else:
                cur.execute("""
                    SELECT t.id, t.user_id, t.user_type, t.subject, t.category, t.priority,
                           t.status, t.created_at, t.updated_at, t.closed_at,
                           (SELECT COUNT(*) FROM support_messages m WHERE m.ticket_id = t.id) AS messages_count
                    FROM support_tickets t
                    WHERE t.user_id = %s
                    ORDER BY t.updated_at DESC
                """, (user_id,))
            tickets = [dict(r) for r in cur.fetchall()]
            for t in tickets:
                t["id"] = str(t["id"])
                t["user_id"] = str(t["user_id"])
        return jsonify({"status": "success", "data": tickets}), 200
    except Exception:
        logger.exception("Erro em list_my_tickets")
        return jsonify({"status": "error", "message": "Erro ao listar tickets"}), 500
    finally:
        conn.close()


@support_bp.get("/tickets/<uuid:ticket_id>")
def get_ticket(ticket_id):
    user_id, user_type, err = _auth_required()
    if err:
        return err
    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "DB indisponível"}), 503
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT * FROM support_tickets WHERE id = %s", (str(ticket_id),))
            ticket = cur.fetchone()
            if not ticket:
                return jsonify({"status": "error", "message": "Ticket não encontrado"}), 404
            if user_type != "admin" and str(ticket["user_id"]) != str(user_id):
                return jsonify({"status": "error", "message": "Acesso negado"}), 403
            cur.execute("""
                SELECT id, ticket_id, author_id, author_role, body, created_at
                FROM support_messages
                WHERE ticket_id = %s
                ORDER BY created_at ASC
            """, (str(ticket_id),))
            messages = [dict(m) for m in cur.fetchall()]
            ticket_dict = dict(ticket)
            ticket_dict["id"] = str(ticket_dict["id"])
            ticket_dict["user_id"] = str(ticket_dict["user_id"])
            for m in messages:
                m["id"] = str(m["id"])
                m["ticket_id"] = str(m["ticket_id"])
                m["author_id"] = str(m["author_id"])
        return jsonify({"status": "success", "data": {"ticket": ticket_dict, "messages": messages}}), 200
    except Exception:
        logger.exception("Erro em get_ticket")
        return jsonify({"status": "error", "message": "Erro ao buscar ticket"}), 500
    finally:
        conn.close()


@support_bp.post("/tickets/<uuid:ticket_id>/messages")
def add_message(ticket_id):
    user_id, user_type, err = _auth_required()
    if err:
        return err
    data = request.get_json() or {}
    body = (data.get("body") or "").strip()
    if not body:
        return jsonify({"status": "error", "message": "Mensagem vazia"}), 400

    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "DB indisponível"}), 503
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT user_id, status FROM support_tickets WHERE id = %s", (str(ticket_id),))
            t = cur.fetchone()
            if not t:
                return jsonify({"status": "error", "message": "Ticket não encontrado"}), 404
            if user_type != "admin" and str(t["user_id"]) != str(user_id):
                return jsonify({"status": "error", "message": "Acesso negado"}), 403

            cur.execute("""
                INSERT INTO support_messages (ticket_id, author_id, author_role, body)
                VALUES (%s, %s, %s, %s)
                RETURNING id, created_at
            """, (str(ticket_id), user_id, user_type, body))
            new_msg = dict(cur.fetchone())
            new_msg["id"] = str(new_msg["id"])

            new_status = "andamento" if user_type == "admin" else "aguardando"
            cur.execute("""
                UPDATE support_tickets
                SET status = %s, updated_at = NOW()
                WHERE id = %s
            """, (new_status, str(ticket_id)))
        conn.commit()
        return jsonify({"status": "success", "data": new_msg}), 201
    except Exception:
        logger.exception("Erro em add_message")
        conn.rollback()
        return jsonify({"status": "error", "message": "Erro ao enviar mensagem"}), 500
    finally:
        conn.close()


@support_bp.patch("/tickets/<uuid:ticket_id>/status")
def update_status(ticket_id):
    user_id, user_type, err = _auth_required()
    if err:
        return err
    data = request.get_json() or {}
    new_status = data.get("status")
    if new_status not in VALID_STATUS:
        return jsonify({"status": "error", "message": "Status inválido"}), 400

    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "DB indisponível"}), 503
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT user_id FROM support_tickets WHERE id = %s", (str(ticket_id),))
            t = cur.fetchone()
            if not t:
                return jsonify({"status": "error", "message": "Ticket não encontrado"}), 404
            # Usuario so pode fechar seu proprio ticket. Admin pode mudar qualquer status.
            if user_type != "admin":
                if str(t["user_id"]) != str(user_id):
                    return jsonify({"status": "error", "message": "Acesso negado"}), 403
                if new_status != "resolvido":
                    return jsonify({"status": "error", "message": "Você só pode fechar o ticket"}), 403

            closed_at = "NOW()" if new_status == "resolvido" else "NULL"
            cur.execute(f"""
                UPDATE support_tickets
                SET status = %s, updated_at = NOW(), closed_at = {closed_at}
                WHERE id = %s
            """, (new_status, str(ticket_id)))
        conn.commit()
        return jsonify({"status": "success"}), 200
    except Exception:
        logger.exception("Erro em update_status")
        conn.rollback()
        return jsonify({"status": "error", "message": "Erro ao atualizar status"}), 500
    finally:
        conn.close()
