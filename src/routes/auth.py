# src/routes/auth.py

@auth_bp.route('/login', methods=['POST'])
def login():
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "Dados não fornecidos"}), 400
            
        email = data.get('email')
        password = data.get('password')
        
        if not email or not password:
            return jsonify({"error": "Email e senha são obrigatórios"}), 400

        # Autenticação com Supabase
        auth_response = supabase.auth.sign_in_with_password({
            "email": email,
            "password": password
        })
        
        user = auth_response.user
        session = auth_response.session
        
        if not user or not session:
            return jsonify({"error": "Falha na autenticação"}), 401
            
        # Buscar informações adicionais do usuário na SUA tabela personalizada
        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Erro de conexão com o banco"}), 500
            
        try:
            with conn.cursor() as cur:
                # BUSCAR DA SUA TABELA PERSONALIZADA public.users
                cur.execute(
                    "SELECT id, user_type, email, created_at FROM public.users WHERE id = %s",
                    (str(user.id),)
                )
                user_data = cur.fetchone()
                
            if not user_data:
                return jsonify({"error": "Usuário não encontrado no sistema"}), 404
                
            # Extrair os dados da sua tabela
            user_id, user_type, user_email, created_at = user_data
            
            return jsonify({
                "message": "Login realizado com sucesso",
                "user": {
                    "id": user_id,
                    "email": user_email,
                    "name": user_email.split('@')[0],  # Nome temporário do email
                    "user_type": user_type,
                    "created_at": created_at.isoformat() if created_at else None
                },
                "session": {
                    "access_token": session.access_token,
                    "refresh_token": session.refresh_token,
                    "expires_at": session.expires_at
                }
            }), 200
                
        finally:
            conn.close()
            
    except Exception as e:
        logger.error(f"Erro no login: {str(e)}")
        return jsonify({"error": "Erro interno no servidor"}), 500
