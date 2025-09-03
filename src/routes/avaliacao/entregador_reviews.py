# src/routes/avaliacao/entregador_reviews.py

from flask import Blueprint, request, jsonify
import psycopg2.extras
from src.utils.helpers import get_db_connection, get_user_id_from_token

entregador_reviews_bp = Blueprint('entregador_reviews_bp', __name__)

# Rota POST original (sem alterações)
@entregador_reviews_bp.route('/delivery/<uuid:delivery_id>/reviews', methods=['POST'])
def create_delivery_review(delivery_id):
    # ... seu código original aqui ...
    pass

# ✅✅✅ INÍCIO DA NOVA ROTA ✅✅✅
@entregador_reviews_bp.route('/delivery/my-reviews', methods=['GET'])
def get_my_delivery_reviews():
    """ Busca as avaliações que o entregador logado recebeu dos clientes. """
    user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
    if error: return error
    if user_type != 'delivery': return jsonify({'error': 'Acesso negado.'}), 403

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT id FROM delivery_profiles WHERE user_id = %s", (user_id,))
            delivery_profile = cur.fetchone()
            if not delivery_profile: return jsonify({'error': 'Perfil de entregador não encontrado.'}), 404
            
            delivery_id = delivery_profile['id']

            cur.execute("""
                SELECT 
                    dr.rating, 
                    dr.comment, 
                    dr.created_at,
                    (cp.first_name || ' ' || cp.last_name) as reviewer_name
                FROM 
                    delivery_reviews dr
                JOIN 
                    client_profiles cp ON dr.client_id = cp.id
                WHERE 
                    dr.delivery_id = %s 
                ORDER BY 
                    dr.created_at DESC
            """, (delivery_id,))
            
            reviews = [dict(row) for row in cur.fetchall()]

            cur.execute("SELECT AVG(rating)::float, COUNT(*) FROM delivery_reviews WHERE delivery_id = %s", (delivery_id,))
            avg, count = cur.fetchone()
            
            return jsonify({
                'reviews': reviews,
                'average_rating': avg or 0,
                'total_reviews': count
            }), 200
            
    except Exception as e:
        print(f"Erro ao buscar avaliações do entregador: {e}")
        return jsonify({'error': 'Erro interno do servidor'}), 500
    finally:
        if conn: conn.close()
# ✅✅✅ FIM DA NOVA ROTA ✅✅✅

# Rota GET original (pode ser mantida)
@entregador_reviews_bp.route('/delivery/<uuid:delivery_id>/reviews', methods=['GET'])
def list_delivery_reviews(delivery_id):
    # ... seu código original aqui ...
    pass
