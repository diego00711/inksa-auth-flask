# src/routes/avaliacao/entregador_reviews.py

import uuid
import logging
from flask import Blueprint, request, jsonify
import psycopg2.extras
from src.utils.helpers import get_db_connection, get_user_id_from_token

entregador_reviews_bp = Blueprint('entregador_reviews_bp', __name__)


@entregador_reviews_bp.route('/delivery/<uuid:delivery_id>/reviews', methods=['POST'])
def create_delivery_review(delivery_id):
    """Cliente avalia o entregador após entrega concluída."""
    if isinstance(delivery_id, uuid.UUID):
        delivery_id = str(delivery_id)

    user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
    if error:
        return error
    if user_type != 'client':
        return jsonify({'error': 'Apenas clientes podem avaliar entregadores.'}), 403

    data = request.get_json(silent=True) or {}
    rating = data.get('rating')
    comment = data.get('comment', '')
    order_id = data.get('order_id')

    if not order_id or not rating:
        return jsonify({'error': 'order_id e rating são obrigatórios'}), 400

    try:
        rating = int(rating)
        if not 1 <= rating <= 5:
            raise ValueError
    except (ValueError, TypeError):
        return jsonify({'error': 'rating deve ser um número inteiro entre 1 e 5'}), 400

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                SELECT o.status, cp.id AS client_profile_id
                FROM orders o
                JOIN client_profiles cp ON o.client_id = cp.id
                WHERE o.id = %s AND cp.user_id = %s AND o.delivery_id = %s
            """, (order_id, user_id, delivery_id))
            row = cur.fetchone()
            if not row:
                return jsonify({'error': 'Pedido inválido ou não associado a este entregador.'}), 400
            if row['status'] != 'delivered':
                return jsonify({'error': 'O pedido ainda não foi entregue.'}), 400

            cur.execute(
                "SELECT 1 FROM delivery_reviews WHERE order_id = %s AND client_id = %s",
                (order_id, row['client_profile_id'])
            )
            if cur.fetchone():
                return jsonify({'error': 'Você já avaliou este entregador para este pedido.'}), 400

            cur.execute("""
                INSERT INTO delivery_reviews (order_id, delivery_id, client_id, rating, comment)
                VALUES (%s, %s, %s, %s, %s)
            """, (order_id, delivery_id, row['client_profile_id'], rating, comment))
            conn.commit()
            return jsonify({'message': 'Avaliação do entregador registrada com sucesso!'}), 201
    except Exception as e:
        if conn:
            conn.rollback()
        logging.error(f"Erro ao criar avaliação do entregador: {e}")
        return jsonify({'error': 'Erro interno do servidor'}), 500
    finally:
        if conn:
            conn.close()


@entregador_reviews_bp.route('/delivery/my-reviews', methods=['GET'])
def get_my_delivery_reviews():
    """Busca as avaliações que o entregador logado recebeu dos clientes."""
    user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
    if error:
        return error
    if user_type != 'delivery':
        return jsonify({'error': 'Acesso negado.'}), 403

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT id FROM delivery_profiles WHERE user_id = %s", (user_id,))
            delivery_profile = cur.fetchone()
            if not delivery_profile:
                return jsonify({'error': 'Perfil de entregador não encontrado.'}), 404

            delivery_id = delivery_profile['id']

            cur.execute("""
                SELECT dr.rating, dr.comment, dr.created_at,
                       (cp.first_name || ' ' || cp.last_name) AS reviewer_name
                FROM delivery_reviews dr
                JOIN client_profiles cp ON dr.client_id = cp.id
                WHERE dr.delivery_id = %s
                ORDER BY dr.created_at DESC
            """, (delivery_id,))
            reviews = [dict(row) for row in cur.fetchall()]

            cur.execute(
                "SELECT AVG(rating)::float, COUNT(*) FROM delivery_reviews WHERE delivery_id = %s",
                (delivery_id,)
            )
            avg, count = cur.fetchone()
            return jsonify({
                'reviews': reviews,
                'average_rating': round(avg or 0, 1),
                'total_reviews': count
            }), 200
    except Exception as e:
        logging.error(f"Erro ao buscar avaliações do entregador: {e}")
        return jsonify({'error': 'Erro interno do servidor'}), 500
    finally:
        if conn:
            conn.close()


@entregador_reviews_bp.route('/delivery/<uuid:delivery_id>/reviews', methods=['GET'])
def list_delivery_reviews(delivery_id):
    """Lista avaliações públicas de um entregador por ID."""
    if isinstance(delivery_id, uuid.UUID):
        delivery_id = str(delivery_id)

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                SELECT dr.rating, dr.comment, dr.created_at,
                       (cp.first_name || ' ' || cp.last_name) AS reviewer_name
                FROM delivery_reviews dr
                JOIN client_profiles cp ON dr.client_id = cp.id
                WHERE dr.delivery_id = %s
                ORDER BY dr.created_at DESC
            """, (delivery_id,))
            reviews = [dict(r) for r in cur.fetchall()]

            cur.execute(
                "SELECT AVG(rating)::float, COUNT(*) FROM delivery_reviews WHERE delivery_id = %s",
                (delivery_id,)
            )
            avg, count = cur.fetchone()
            return jsonify({
                'reviews': reviews,
                'average_rating': round(avg or 0, 1),
                'total_reviews': count
            }), 200
    except Exception as e:
        logging.error(f"Erro ao listar avaliações do entregador: {e}")
        return jsonify({'error': 'Erro interno do servidor'}), 500
    finally:
        if conn:
            conn.close()
