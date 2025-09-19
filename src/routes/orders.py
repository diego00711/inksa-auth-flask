# Em src/routes/orders.py, substitua esta função:

@orders_bp.route('/<uuid:order_id>/status', methods=['PUT'])
def update_order_status(order_id):
    # =================== MODO DE DEPURAÇÃO ATIVADO ===================
    logger.info(f"--- INÍCIO DA DEPURAÇÃO UPDATE_ORDER_STATUS para {order_id} ---")
    conn = None
    try:
        # 1. LOG: Cabeçalhos da Requisição
        logger.info(f"DEBUG: Cabeçalhos recebidos: {dict(request.headers)}")

        # 2. LOG: Corpo da Requisição (antes de tentar parsear como JSON)
        raw_body = request.get_data(as_text=True)
        logger.info(f"DEBUG: Corpo da requisição (raw): '{raw_body}'")

        user_auth_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
        if error: 
            logger.error(f"DEBUG: Falha na autenticação: {error}")
            return error

        if user_type != 'restaurant':
            logger.warning(f"DEBUG: Permissão negada. User type é '{user_type}', não 'restaurant'.")
            return jsonify({"error": "Apenas restaurantes podem alterar o status de um pedido"}), 403

        data = request.get_json()
        # 3. LOG: Corpo da Requisição (depois de parsear como JSON)
        logger.info(f"DEBUG: Corpo da requisição (JSON parseado): {data}")

        if not data or 'new_status' not in data:
            logger.error("DEBUG: Falha na validação. 'data' é None ou 'new_status' não está no JSON.")
            return jsonify({"error": "Campo 'new_status' é obrigatório no corpo do JSON"}), 400

        new_status_internal = data['new_status']
        logger.info(f"DEBUG: 'new_status' recebido do payload: '{new_status_internal}'")

        if new_status_internal not in VALID_STATUSES_INTERNAL:
            logger.error(f"DEBUG: Status '{new_status_internal}' não está na lista de status válidos.")
            return jsonify({"error": f"Status interno inválido: '{new_status_internal}'"}), 400
        
        if new_status_internal in ['delivering', 'delivered']:
            logger.warning("DEBUG: Tentativa de usar rota errada para 'delivering' ou 'delivered'.")
            return jsonify({"error": "Use o endpoint de verificação de código para esta transição."}), 400

        conn = get_db_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT o.status FROM orders o JOIN restaurant_profiles rp ON o.restaurant_id = rp.id WHERE o.id = %s AND rp.user_id = %s", (str(order_id), user_auth_id))
            order = cur.fetchone()
            
            if not order:
                logger.error("DEBUG: Pedido não encontrado no banco de dados para este restaurante.")
                return jsonify({"error": "Pedido não encontrado ou não pertence a este restaurante"}), 404

            current_status = order['status'].strip()
            # 4. LOG: Status atual vs. novo status
            logger.info(f"DEBUG: Comparando transição: de '{current_status}' para '{new_status_internal}'")

            if not is_valid_status_transition(current_status, new_status_internal):
                error_message = f"Transição de status de '{current_status}' para '{new_status_internal}' não permitida"
                logger.error(f"DEBUG: Validação de transição FALHOU. {error_message}")
                return jsonify({"error": error_message}), 400
            
            logger.info("DEBUG: Validação de transição OK. Atualizando o banco de dados...")
            cur.execute("UPDATE orders SET status = %s, updated_at = NOW() WHERE id = %s RETURNING *", (new_status_internal, str(order_id)))
            updated_order = dict(cur.fetchone())
            conn.commit()

            updated_order.pop('pickup_code', None)
            updated_order.pop('delivery_code', None)
            
            logger.info(f"--- FIM DA DEPURAÇÃO: SUCESSO. Status do pedido {order_id} atualizado para {new_status_internal} ---")
            return jsonify(updated_order), 200

    except Exception as e:
        logger.error(f"--- FIM DA DEPURAÇÃO: ERRO INESPERADO. {e} ---", exc_info=True)
        if conn: conn.rollback()
        return jsonify({"error": "Erro interno no servidor"}), 500
    finally:
        if conn: conn.close()
