# src/routes/avaliacao/cliente_reviews.py

from flask import Blueprint, request, jsonify
# Importa o DictCursor para facilitar a manipulação dos resultados
import psycopg2.extras
from src.utils.helpers import get_db_connection, get_user_id_from_token

cliente_reviews_bp = Blueprint('cliente_reviews_bp', __name__)

#
# ROTA POST ORIGINAL (SEM ALTERAÇÕES)
#
@cliente_reviews_bp.route('/clients/<uuid:client_id>/reviews', methods=['POST'])
def create_client_review(client_id):
    user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
    if error:
        return error
    if user_type not in ['restaurant', 'delivery']:
        return jsonify({'error': 'Apenas restaurantes ou entregadores podem avaliar clientes.'}), 403

    data = request.get_json()
    rating = data.get('rating')
    comment = data.get('comment', '')
    order_id = data.get('order_id')
    if not order_id or not rating:
        return jsonify({'error': 'order_id e rating são obrigatórios'}), 400

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            if user_type == 'restaurant':
                cur.execute(
                    "SELECT 1 FROM orders WHERE id=%s AND restaurant_id=(SELECT id FROM restaurant_profiles WHERE user_id=%s) AND client_id=%s",
                    (order_id, user_id, client_id)
                )
            else: # delivery
                cur.execute(
                    "SELECT 1 FROM orders WHERE id=%s AND delivery_id=(SELECT id FROM delivery_profiles WHERE user_id=%s) AND client_id=%s",
                    (order_id, user_id, client_id)
                )
            has_order = cur.fetchone()
            if not has_order:
                return jsonify({'error': 'Você não pode avaliar este cliente para este pedido.'}), 400

            cur.execute(
                "SELECT 1 FROM client_reviews WHERE order_id=%s AND reviewer_type=%s AND reviewer_id=(CASE WHEN %s='restaurant' THEN (SELECT id FROM restaurant_profiles WHERE user_id=%s) ELSE (SELECT id FROM delivery_profiles WHERE user_id=%s) END)",
                (order_id, user_type, user_type, user_id, user_id)
            )
            if cur.fetchone():
                return jsonify({'error': 'Você já avaliou este cliente para este pedido.'}), 400

            reviewer_id = None
            if user_type == 'restaurant':
                cur.execute("SELECT id FROM restaurant_profiles WHERE user_id=%s", (user_id,))
            else:
                cur.execute("SELECT id FROM delivery_profiles WHERE user_id=%s", (user_id,))
            reviewer_id = cur.fetchone()[0]

            cur.execute("""
                INSERT INTO client_reviews (order_id, client_id, reviewer_type, reviewer_id, rating, comment)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (order_id, client_id, user_type, reviewer_id, rating, comment))
            conn.commit()
            return jsonify({'message': 'Avaliação do cliente registrada com sucesso!'}), 201
    finally:
        conn.close()

#
# ✅✅✅ INÍCIO DA NOVA ROTA ADICIONADA ✅✅✅
#
@cliente_reviews_bp.route('/clients/my-reviews', methods=['GET'])
def get_my_client_reviews():
    """
    Busca as avaliações que o cliente logado recebeu de restaurantes e entregadores.
    """
    # 1. Pega o ID do usuário a partir do token de autenticação
    user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
    if error:
        return error
    if user_type != 'client':
        return jsonify({'error': 'Acesso negado. Apenas para clientes.'}), 403

    conn = None
    try:
        conn = get_db_connection()
        # Usa DictCursor para que o resultado seja um dicionário (mais fácil de usar)
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # 2. Busca o ID do perfil do cliente, que é usado nas avaliações
            cur.execute("SELECT id FROM client_profiles WHERE user_id = %s", (user_id,))
            client_profile = cur.fetchone()
            if not client_profile:
                return jsonify({'error': 'Perfil de cliente não encontrado.'}), 404
            
            client_id = client_profile['id']

            # 3. Busca as avaliações recebidas por este cliente, juntando com os perfis
            #    de quem avaliou para pegar seus nomes.
            cur.execute("""
                SELECT 
                    cr.reviewer_type, 
                    cr.rating, 
                    cr.comment, 
                    cr.created_at,
                    -- Pega o nome do avaliador, seja ele um restaurante ou um entregador
                    CASE 
                        WHEN cr.reviewer_type = 'restaurant' THEN rp.restaurant_name
                        WHEN cr.reviewer_type = 'delivery' THEN (dp.first_name || ' ' || dp.last_name)
                        ELSE 'Avaliador Anônimo'
                    END as reviewer_name
                FROM 
                    client_reviews cr
                LEFT JOIN 
                    restaurant_profiles rp ON cr.reviewer_id = rp.id AND cr.reviewer_type = 'restaurant'
                LEFT JOIN 
                    delivery_profiles dp ON cr.reviewer_id = dp.id AND cr.reviewer_type = 'delivery'
                WHERE 
                    cr.client_id = %s 
                ORDER BY 
                    cr.created_at DESC
            """, (client_id,))
            
            reviews = [dict(row) for row in cur.fetchall()]

            # 4. Calcula a média e o total de avaliações
            cur.execute(
                "SELECT AVG(rating)::float, COUNT(*) FROM client_reviews WHERE client_id = %s",
                (client_id,)
            )
            avg, count = cur.fetchone()
            
            # 5. Retorna um pacote completo de dados para o frontend
            return jsonify({
                'reviews': reviews,
                'average_rating': avg or 0,
                'total_reviews': count
            }), 200
            
    except Exception as e:
        print(f"Erro ao buscar avaliações do cliente: {e}")
        return jsonify({'error': 'Erro interno do servidor'}), 500
    finally:
        if conn:
            conn.close()
# ✅✅✅ FIM DA NOVA ROTA ADICIONADA ✅✅✅


#
# ROTA GET ORIGINAL (Pode ser mantida ou removida se não for mais necessária)
#
@cliente_reviews_bp.route('/clients/<uuid:client_id>/reviews', methods=['GET'])
def list_client_reviews(client_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT reviewer_type, rating, comment, created_at FROM client_reviews WHERE client_id=%s ORDER BY created_at DESC",
                (client_id,)
            )
            reviews = [dict(zip(['reviewer_type', 'rating', 'comment', 'created_at'], row)) for row in cur.fetchall()]
            cur.execute(
                "SELECT AVG(rating)::float, COUNT(*) FROM client_reviews WHERE client_id=%s",
                (client_id,)
            )
            avg, count = cur.fetchone()
            return jsonify({
                'reviews': reviews,
                'average_rating': avg or 0,
                'total_reviews': count
            }), 200
    finally:
        conn.close()
