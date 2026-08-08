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

    # O ENTREGADOR também avalia o parceiro (espera na retirada, organização).
    # Antes a rota era exclusiva de cliente: a avaliação que o app do entregador
    # enviava no fim da entrega falhava calada (403) e nunca era registrada.
    if user_type not in ('client', 'delivery'):
        return jsonify({'error': 'Apenas clientes e entregadores podem avaliar parceiros.'}), 403

    data = request.get_json()
    rating = data.get('rating')
    comment = data.get('comment', '')
    # os apps mandam orderId (camelCase); aceitar as duas grafias
    order_id = data.get('order_id') or data.get('orderId')
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
            # Quem avalia precisa ter participado do pedido: o cliente é o dono
            # dele; o entregador é quem retirou nesse parceiro.
            if user_type == 'client':
                cur.execute(
                    "SELECT o.status, cp.id FROM orders o JOIN client_profiles cp ON o.client_id = cp.id "
                    "WHERE o.id=%s AND cp.user_id=%s AND o.restaurant_id=%s",
                    (order_id, user_id, restaurant_id)
                )
            else:
                cur.execute(
                    "SELECT o.status, dp.id FROM orders o JOIN delivery_profiles dp ON o.delivery_id = dp.id "
                    "WHERE o.id=%s AND dp.user_id=%s AND o.restaurant_id=%s",
                    (order_id, user_id, restaurant_id)
                )
            order = cur.fetchone()
            if not order:
                return jsonify({'error': 'Pedido inválido ou não associado a este parceiro'}), 400
            reviewer_profile_id = order[1]

            # Cliente só avalia depois de receber. O entregador já pode avaliar
            # a partir da retirada (é quando ele passa pelo parceiro).
            if user_type == 'client':
                if order[0] != 'delivered':
                    return jsonify({'error': 'Pedido inválido ou ainda não entregue'}), 400
            elif order[0] not in ('delivering', 'Saiu para Entrega', 'Entregando', 'delivered', 'delivery_failed'):
                return jsonify({'error': 'O pedido ainda não foi retirado.'}), 400

            # Evita avaliação duplicada do MESMO avaliador
            cur.execute(
                "SELECT 1 FROM restaurant_reviews WHERE order_id=%s AND reviewer_type=%s AND reviewer_id=%s",
                (order_id, user_type, reviewer_profile_id)
            )
            if cur.fetchone():
                return jsonify({'error': 'Você já avaliou esse pedido.'}), 400

            cur.execute("""
                INSERT INTO restaurant_reviews
                    (order_id, restaurant_id, client_id, reviewer_type, reviewer_id, rating, comment, tags, category_ratings)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id, reviewer_id
            """, (order_id, restaurant_id,
                  reviewer_profile_id if user_type == 'client' else None,
                  user_type, reviewer_profile_id, rating, comment,
                  psycopg2.extras.Json(tags) if tags else None,
                  psycopg2.extras.Json(category_ratings) if category_ratings else None))
            review_row = cur.fetchone()
            conn.commit()

            if _award_points_for_action:
                try:
                    if user_type == 'client':
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
