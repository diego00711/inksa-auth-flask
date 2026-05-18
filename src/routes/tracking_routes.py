# src/routes/tracking_routes.py
# Rastreamento em tempo real de pedidos/entregadores

import logging
from flask import Blueprint, request, jsonify
import psycopg2
import psycopg2.extras
from ..utils.helpers import get_db_connection, get_user_id_from_token

logger = logging.getLogger(__name__)

tracking_bp = Blueprint('tracking', __name__)


@tracking_bp.route('/<order_id>/location', methods=['PATCH'])
def update_delivery_location(order_id):
    """
    PATCH /api/deliveries/<order_id>/location
    Body: { "latitude": float, "longitude": float }
    Requer autenticacao de entregador.
    Salva/atualiza na tabela delivery_tracking (INSERT ON CONFLICT DO UPDATE).
    """
    conn = None
    try:
        user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
        if error:
            return error
        if user_type != 'delivery':
            return jsonify({"error": "Apenas entregadores podem atualizar a localizacao"}), 403

        data = request.get_json(silent=True) or {}
        try:
            latitude = float(data['latitude'])
            longitude = float(data['longitude'])
        except (KeyError, ValueError, TypeError):
            return jsonify({"error": "latitude e longitude sao obrigatorios e devem ser numericos"}), 400

        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Erro de conexao com o banco de dados"}), 500

        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # Verifica que o pedido pertence ao entregador autenticado
            cur.execute("SELECT id FROM delivery_profiles WHERE user_id = %s", (user_id,))
            dprof = cur.fetchone()
            if not dprof:
                return jsonify({"error": "Perfil de entregador nao encontrado"}), 404

            cur.execute("SELECT id FROM orders WHERE id = %s AND delivery_id = %s", (order_id, str(dprof['id'])))
            if not cur.fetchone():
                return jsonify({"error": "Pedido nao encontrado ou nao pertence a este entregador"}), 404

            try:
                cur.execute("""
                    INSERT INTO public.delivery_tracking (order_id, latitude, longitude, updated_at)
                    VALUES (%s, %s, %s, NOW())
                    ON CONFLICT (order_id) DO UPDATE
                        SET latitude = EXCLUDED.latitude,
                            longitude = EXCLUDED.longitude,
                            updated_at = NOW()
                    RETURNING order_id, latitude, longitude, updated_at
                """, (order_id, latitude, longitude))
                row = dict(cur.fetchone())
                conn.commit()
            except psycopg2.errors.UndefinedTable:
                conn.rollback()
                logger.warning("Tabela delivery_tracking nao existe. Execute create_tracking.sql")
                return jsonify({"error": "Tabela de rastreamento nao configurada. Execute create_tracking.sql"}), 503

        row['order_id'] = str(row['order_id'])
        row['latitude'] = float(row['latitude'])
        row['longitude'] = float(row['longitude'])
        if row.get('updated_at'):
            row['updated_at'] = row['updated_at'].isoformat()

        return jsonify({"success": True, "location": row}), 200

    except Exception as e:
        logger.error(f"Erro em update_delivery_location: {e}", exc_info=True)
        if conn:
            conn.rollback()
        return jsonify({"error": "Erro interno do servidor"}), 500
    finally:
        if conn:
            conn.close()


@tracking_bp.route('/<order_id>/location', methods=['GET'])
def get_delivery_location(order_id):
    """
    GET /api/deliveries/<order_id>/location
    Retorna { latitude, longitude, updated_at } ou 404 se nao existe.
    Sem autenticacao obrigatoria (cliente pode consultar).
    """
    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Erro de conexao com o banco de dados"}), 500

        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            try:
                cur.execute("""
                    SELECT order_id, latitude, longitude, updated_at
                    FROM public.delivery_tracking
                    WHERE order_id = %s
                """, (order_id,))
                row = cur.fetchone()
            except psycopg2.errors.UndefinedTable:
                logger.warning("Tabela delivery_tracking nao existe. Execute create_tracking.sql")
                return jsonify({"error": "Tabela de rastreamento nao configurada. Execute create_tracking.sql"}), 503

        if not row:
            return jsonify({"error": "Localizacao nao encontrada para este pedido"}), 404

        result = {
            "order_id": str(row['order_id']),
            "latitude": float(row['latitude']),
            "longitude": float(row['longitude']),
            "updated_at": row['updated_at'].isoformat() if row['updated_at'] else None,
        }
        return jsonify(result), 200

    except Exception as e:
        logger.error(f"Erro em get_delivery_location: {e}", exc_info=True)
        return jsonify({"error": "Erro interno do servidor"}), 500
    finally:
        if conn:
            conn.close()
