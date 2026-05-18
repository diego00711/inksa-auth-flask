# src/routes/chat_routes.py
# Blueprint: chat_bp, prefix /api/chat

import logging
from flask import Blueprint, request, jsonify
import psycopg2
import psycopg2.extras
from ..utils.helpers import get_db_connection, get_user_id_from_token

logger = logging.getLogger(__name__)

chat_bp = Blueprint('chat', __name__)

MAX_MESSAGE_LENGTH = 500


def _table_exists(cur) -> bool:
    """Verifica se a tabela chat_messages existe."""
    try:
        cur.execute("""
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = 'chat_messages'
        """)
        return cur.fetchone() is not None
    except Exception:
        return False


@chat_bp.route('/<order_id>/messages', methods=['GET'])
def get_chat_messages(order_id):
    """
    GET /api/chat/<order_id>/messages
    Retorna lista de mensagens ordenadas por created_at ASC.
    Sem autenticacao obrigatoria.
    """
    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Erro de conexao com o banco de dados"}), 500

        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            if not _table_exists(cur):
                logger.warning("Tabela chat_messages nao existe. Execute create_chat.sql")
                return jsonify({
                    "messages": [],
                    "warning": "Tabela de chat nao configurada. Execute create_chat.sql"
                }), 200

            cur.execute("""
                SELECT id, order_id, sender_type, message, created_at
                FROM public.chat_messages
                WHERE order_id = %s
                ORDER BY created_at ASC
            """, (order_id,))
            rows = cur.fetchall()

        messages = []
        for row in rows:
            r = dict(row)
            r['id'] = str(r['id'])
            r['order_id'] = str(r['order_id'])
            if r.get('created_at'):
                r['created_at'] = r['created_at'].isoformat()
            messages.append(r)

        return jsonify({"messages": messages, "total": len(messages)}), 200

    except Exception as e:
        logger.error(f"Erro em get_chat_messages: {e}", exc_info=True)
        return jsonify({"error": "Erro interno do servidor"}), 500
    finally:
        if conn:
            conn.close()


@chat_bp.route('/<order_id>/messages', methods=['POST'])
def send_chat_message(order_id):
    """
    POST /api/chat/<order_id>/messages
    Body: { "sender_type": "client"|"delivery", "message": str }
    Valida: message nao vazio, max 500 chars.
    Retorna a mensagem criada.
    """
    conn = None
    try:
        user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
        if error:
            return error

        data = request.get_json(silent=True) or {}
        sender_type = str(data.get('sender_type', '')).strip().lower()
        message = str(data.get('message', '')).strip()

        if sender_type not in ('client', 'delivery'):
            return jsonify({"error": "sender_type invalido. Use 'client' ou 'delivery'"}), 400

        if not message:
            return jsonify({"error": "A mensagem nao pode estar vazia"}), 400

        if len(message) > MAX_MESSAGE_LENGTH:
            return jsonify({
                "error": f"A mensagem excede o limite de {MAX_MESSAGE_LENGTH} caracteres"
            }), 400

        # Valida que user_type bate com sender_type
        if user_type not in ('client', 'delivery', 'admin'):
            return jsonify({"error": "Apenas clientes e entregadores podem enviar mensagens"}), 403

        if user_type in ('client', 'delivery') and user_type != sender_type:
            return jsonify({"error": f"Seu perfil e '{user_type}', mas sender_type informado e '{sender_type}'"}), 403

        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Erro de conexao com o banco de dados"}), 500

        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            if not _table_exists(cur):
                logger.warning("Tabela chat_messages nao existe. Execute create_chat.sql")
                return jsonify({"error": "Tabela de chat nao configurada. Execute create_chat.sql"}), 503

            try:
                cur.execute("""
                    INSERT INTO public.chat_messages (order_id, sender_type, message)
                    VALUES (%s, %s, %s)
                    RETURNING id, order_id, sender_type, message, created_at
                """, (order_id, sender_type, message))
                new_msg = dict(cur.fetchone())
                conn.commit()
            except psycopg2.errors.CheckViolation:
                conn.rollback()
                return jsonify({"error": "sender_type invalido no banco"}), 400

        new_msg['id'] = str(new_msg['id'])
        new_msg['order_id'] = str(new_msg['order_id'])
        if new_msg.get('created_at'):
            new_msg['created_at'] = new_msg['created_at'].isoformat()

        return jsonify(new_msg), 201

    except Exception as e:
        logger.error(f"Erro em send_chat_message: {e}", exc_info=True)
        if conn:
            conn.rollback()
        return jsonify({"error": "Erro interno do servidor"}), 500
    finally:
        if conn:
            conn.close()
