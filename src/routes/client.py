# src/routes/client.py

from flask import Blueprint

# Este blueprint pode ser usado no futuro para rotas específicas de clientes,
# como /api/client/my-orders ou /api/client/my-reviews.
# Por enquanto, ele está vazio para não causar conflitos.
client_bp = Blueprint('client_bp', __name__)

# Exemplo de rota futura:
# @client_bp.route('/my-orders', methods=['GET'])
# def get_my_orders():
#     # ... lógica para buscar pedidos do cliente ...
#     return jsonify({"message": "Meus pedidos"})
