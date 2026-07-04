from flask import Blueprint, request, jsonify
import uuid
import psycopg2.extras
from src.utils.helpers import get_db_connection, get_user_id_from_token

try:
    from src.routes.gamification_routes import award_points_for_action as _award_points_for_action
except Exception:
    _award_points_for_action = None

restaurante_reviews_bp = Blueprint('restaurante_reviews_bp', __name__)

@restaurante_reviews_bp.route('/restaurants/<uuid:restaurant_id>/reviews', methods=['POST'])
def create_restaurant_review(restaurant_id):
    # Converte UUID para string para evitar "can't adapt type 'UUID'"
    if isinstance(restaurant_id, uuid.UUID):
        restaurant_id = str(restaurant_id)

    user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
    if error:
        return error

    # Se get_user_id_from_token retornar uuid.UUID, converte também
    if isinstance(user_id, uuid.UUID):
        user_id = str(user_id)

    if user_type != 'client':
        return jsonify({'error': 'Apenas clientes podem avaliar.'}), 403

    data = request.get_json()
    rating = data.get('rating')
    comment = data.get('comment', '')
    order_id = data.get('order_id')
    tags = data.get('tags')
    category_ratings = data.get('categoryRatings') or data.get('category_ratings') or data.get('categories')
    if not order_id or not rating:
        return jsonify({'error': 'order_id e rating são obrigatórios'}), 400

    try:
        rating = int(rating)
        if not 1 <= rating <= 5:
            raise ValueError
    except (ValueError, TypeError):
        return jsonify({'error': 'rating deve ser um número inteiro entre 1 e 5'}), 400

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            # Só permite avaliar pedidos entregues
            cur.execute(
                "SELECT status FROM orders WHERE id=%s AND client_id=(SELECT id FROM client_profiles WHERE user_id=%s) AND restaurant_id=%s",
                (order_id, user_id, restaurant_id)
            )
            order = cur.fetchone()
            if not order or order[0] != 'delivered':
                return jsonify({'error': 'Pedido inválido ou ainda não entregue'}), 400

            # Evita avaliação duplicada
            cur.execute(
                "SELECT 1 FROM restaurant_reviews WHERE order_id=%s AND client_id=(SELECT id FROM client_profiles WHERE user_id=%s)",
                (order_id, user_id)
            )
            if cur.fetchone():
                return jsonify({'error': 'Você já avaliou esse pedido.'}), 400

            cur.execute("""
                INSERT INTO restaurant_reviews (order_id, restaurant_id, client_id, rating, comment, tags, category_ratings)
                VALUES (%s, %s, (SELECT id FROM client_profiles WHERE user_id=%s), %s, %s, %s, %s)
                RETURNING id, client_id
            """, (order_id, restaurant_id, user_id, rating, comment,
                  psycopg2.extras.Json(tags) if tags else None,
                  psycopg2.extras.Json(category_ratings) if category_ratings else None))
            review_row = cur.fetchone()
            conn.commit()

            if _award_points_for_action:
                try:
                    _award_points_for_action(
                        user_id=str(review_row[1]),
                        action_key="review_given_client",
                        order_id=str(order_id),
                        description="Avaliação enviada",
                    )
                    if rating == 5:
                        _award_points_for_action(
                            user_id=str(restaurant_id),
                            action_key="five_star_received_restaurant",
                            order_id=str(order_id),
                            description="Avaliação 5 estrelas recebida",
                        )
                except Exception:
                    pass

            return jsonify({'message': 'Avaliação registrada com sucesso!'}), 201
    finally:
        conn.close()

@restaurante_reviews_bp.route('/restaurants/<uuid:restaurant_id>/reviews', methods=['GET'])
def list_restaurant_reviews(restaurant_id):
    # Converte UUID para string para evitar "can't adapt type 'UUID'"
    if isinstance(restaurant_id, uuid.UUID):
        restaurant_id = str(restaurant_id)

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT rating, comment, tags, category_ratings, created_at FROM restaurant_reviews WHERE restaurant_id=%s ORDER BY created_at DESC",
                (restaurant_id,)
            )
            reviews = [dict(zip(['rating', 'comment', 'tags', 'category_ratings', 'created_at'], row)) for row in cur.fetchall()]
            # Também retorna média e contagem
            cur.execute(
                "SELECT AVG(rating)::float, COUNT(*) FROM restaurant_reviews WHERE restaurant_id=%s",
                (restaurant_id,)
            )
            avg, count = cur.fetchone()
            return jsonify({
                'reviews': reviews,
                'average_rating': round(avg or 0, 1),
                'total_reviews': count
            }), 200
    finally:
        conn.close()
