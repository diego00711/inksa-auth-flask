from flask import Blueprint, request, jsonify
from src.utils.helpers import get_db_connection, get_user_id_from_token

restaurante_reviews_bp = Blueprint('restaurante_reviews_bp', __name__)

@restaurante_reviews_bp.route('/restaurants/<uuid:restaurant_id>/reviews', methods=['POST'])
def create_restaurant_review(restaurant_id):
    user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
    if error:
        return error
    if user_type != 'client':
        return jsonify({'error': 'Apenas clientes podem avaliar.'}), 403

    data = request.get_json()
    rating = data.get('rating')
    comment = data.get('comment', '')
    order_id = data.get('order_id')
    if not order_id or not rating:
        return jsonify({'error': 'order_id e rating são obrigatórios'}), 400

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
                INSERT INTO restaurant_reviews (order_id, restaurant_id, client_id, rating, comment)
                VALUES (%s, %s, (SELECT id FROM client_profiles WHERE user_id=%s), %s, %s)
                RETURNING id
            """, (order_id, restaurant_id, user_id, rating, comment))
            conn.commit()
            return jsonify({'message': 'Avaliação registrada com sucesso!'}), 201
    finally:
        conn.close()

@restaurante_reviews_bp.route('/restaurants/<uuid:restaurant_id>/reviews', methods=['GET'])
def list_restaurant_reviews(restaurant_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT rating, comment, created_at FROM restaurant_reviews WHERE restaurant_id=%s ORDER BY created_at DESC",
                (restaurant_id,)
            )
            reviews = [dict(zip(['rating', 'comment', 'created_at'], row)) for row in cur.fetchall()]
            # Também retorna média e contagem
            cur.execute(
                "SELECT AVG(rating)::float, COUNT(*) FROM restaurant_reviews WHERE restaurant_id=%s",
                (restaurant_id,)
            )
            avg, count = cur.fetchone()
            return jsonify({
                'reviews': reviews,
                'average_rating': avg or 0,
                'total_reviews': count
            }), 200
    finally:
        conn.close()
