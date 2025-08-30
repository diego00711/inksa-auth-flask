from flask import Blueprint, request, jsonify
from ..utils.helpers import get_db_connection, get_user_id_from_token

menu_item_reviews_bp = Blueprint('menu_item_reviews_bp', __name__)

@menu_item_reviews_bp.route('/menu-items/<uuid:menu_item_id>/reviews', methods=['POST'])
def create_menu_item_review(menu_item_id):
    user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
    if error:
        return error
    if user_type != 'client':
        return jsonify({'error': 'Apenas clientes podem avaliar itens do menu.'}), 403

    data = request.get_json()
    rating = data.get('rating')
    comment = data.get('comment', '')
    order_id = data.get('order_id')
    if not order_id or not rating:
        return jsonify({'error': 'order_id e rating são obrigatórios'}), 400

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            # Checa se o cliente realmente comprou esse item nesse pedido
            cur.execute(
                "SELECT 1 FROM orders o "
                "JOIN order_items oi ON oi.order_id = o.id "
                "WHERE o.id=%s AND o.client_id=(SELECT id FROM client_profiles WHERE user_id=%s) AND oi.menu_item_id=%s",
                (order_id, user_id, menu_item_id)
            )
            has_item = cur.fetchone()
            if not has_item:
                return jsonify({'error': 'Este item não faz parte do pedido do cliente.'}), 400

            # Evita avaliação duplicada para o mesmo item no mesmo pedido
            cur.execute(
                "SELECT 1 FROM menu_item_reviews WHERE order_id=%s AND menu_item_id=%s AND client_id=(SELECT id FROM client_profiles WHERE user_id=%s)",
                (order_id, menu_item_id, user_id)
            )
            if cur.fetchone():
                return jsonify({'error': 'Você já avaliou este item para este pedido.'}), 400

            cur.execute("""
                INSERT INTO menu_item_reviews (order_id, menu_item_id, client_id, rating, comment)
                VALUES (%s, %s, (SELECT id FROM client_profiles WHERE user_id=%s), %s, %s)
                RETURNING id
            """, (order_id, menu_item_id, user_id, rating, comment))
            conn.commit()
            return jsonify({'message': 'Avaliação do item registrada com sucesso!'}), 201
    finally:
        conn.close()

@menu_item_reviews_bp.route('/menu-items/<uuid:menu_item_id>/reviews', methods=['GET'])
def list_menu_item_reviews(menu_item_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT rating, comment, created_at FROM menu_item_reviews WHERE menu_item_id=%s ORDER BY created_at DESC",
                (menu_item_id,)
            )
            reviews = [dict(zip(['rating', 'comment', 'created_at'], row)) for row in cur.fetchall()]
            cur.execute(
                "SELECT AVG(rating)::float, COUNT(*) FROM menu_item_reviews WHERE menu_item_id=%s",
                (menu_item_id,)
            )
            avg, count = cur.fetchone()
            return jsonify({
                'reviews': reviews,
                'average_rating': avg or 0,
                'total_reviews': count
            }), 200
    finally:
        conn.close()
