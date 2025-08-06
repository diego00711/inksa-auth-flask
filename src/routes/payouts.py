# src/routes/payouts.py

import logging
from flask import Blueprint, request, jsonify
from ..utils.helpers import get_user_id_from_token
from ..logic.payout_processor import process_payouts_for_cycle

payouts_bp = Blueprint('payouts_bp', __name__)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

@payouts_bp.route('/payouts/process', methods=['POST'])
def handle_process_payouts():
    """
    Endpoint para acionar o processamento de lotes de pagamento (payouts).
    Requer autenticação de administrador.
    """
    # 1. Segurança: Verificar se o usuário é um administrador
    user_auth_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
    if error:
        return error
    
    if user_type != 'admin':
        logging.warning(f"Tentativa de acesso não autorizado à rota de processamento de pagamentos pelo usuário {user_auth_id}.")
        return jsonify({"error": "Acesso não autorizado."}), 403

    # 2. Obter os parâmetros da requisição
    data = request.get_json()
    if not data:
        return jsonify({"error": "Corpo da requisição não fornecido."}), 400

    partner_type = data.get('partner_type')
    cycle_type = data.get('cycle_type')

    # 3. Validar os parâmetros
    if not partner_type or partner_type not in ['restaurant', 'delivery']:
        return jsonify({"error": "Parâmetro 'partner_type' é obrigatório e deve ser 'restaurant' ou 'delivery'."}), 400
    
    if not cycle_type or cycle_type not in ['weekly', 'bi-weekly', 'monthly']:
        return jsonify({"error": "Parâmetro 'cycle_type' é obrigatório e deve ser 'weekly', 'bi-weekly' ou 'monthly'."}), 400

    logging.info(f"Administrador {user_auth_id} iniciou o processamento de pagamentos para '{partner_type}' no ciclo '{cycle_type}'.")

    # 4. Chamar a função de lógica que criámos
    try:
        generated_payouts, count = process_payouts_for_cycle(cycle_type, partner_type)
        
        if "error" in generated_payouts: # Verifica se a função retornou um erro
             return jsonify({"error": "Ocorreu um erro durante o processamento.", "details": generated_payouts["error"]}), 500

        # 5. Retornar uma resposta de sucesso para o administrador
        return jsonify({
            "status": "success",
            "message": f"Processamento concluído. {count} lotes de pagamento foram gerados.",
            "cycle_processed": cycle_type,
            "partner_type_processed": partner_type,
            "payouts_generated": generated_payouts
        }), 200
        
    except Exception as e:
        logging.error(f"Erro inesperado na rota de processamento de pagamentos: {e}", exc_info=True)
        return jsonify({"error": "Erro interno inesperado no servidor."}), 500