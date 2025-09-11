# src/utils/decorators.py (ou o nome do seu arquivo) - CÓDIGO CORRIGIDO E COMPLETO

from functools import wraps
from flask import request, jsonify
# A importação agora vem de helpers, que é o nosso padrão
from .helpers import get_user_id_from_token 

def admin_required(f):
    """
    Decorator que verifica se o token é válido e se o usuário é do tipo 'admin',
    buscando a permissão do banco de dados.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Permite requisições OPTIONS (para o CORS funcionar)
        if request.method == 'OPTIONS':
            return jsonify(), 200
            
        auth_header = request.headers.get('Authorization')
        user_id, user_type, error_response = get_user_id_from_token(auth_header)
        
        if error_response:
            return error_response
        
        if user_type != 'admin':
            return jsonify({"error": "Acesso negado. Apenas administradores."}), 403
        
        # Anexa o user_id à requisição para uso posterior, se necessário
        request.user_id = user_id
        
        return f(*args, **kwargs)
    
    return decorated_function

def delivery_token_required(f):
    """
    Decorator que verifica se o token é válido e se o usuário é do tipo 'delivery',
    buscando a permissão do banco de dados.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Permite requisições OPTIONS (para o CORS funcionar)
        if request.method == 'OPTIONS':
            return jsonify(), 200
            
        auth_header = request.headers.get('Authorization')
        user_id, user_type, error_response = get_user_id_from_token(auth_header)
        
        if error_response:
            return error_response
        
        if user_type != 'delivery':
            return jsonify({"error": "Acesso negado. Apenas entregadores."}), 403
        
        # Anexa as informações à requisição para uso nas rotas
        request.user_id = user_id
        request.user_type = user_type
        
        return f(*args, **kwargs)
    
    return decorated_function

def user_token_required(f):
    """
    Decorator genérico que apenas valida o token e anexa as informações do usuário à requisição.
    Útil para rotas que qualquer usuário logado pode acessar.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Permite requisições OPTIONS (para o CORS funcionar)
        if request.method == 'OPTIONS':
            return jsonify(), 200
            
        auth_header = request.headers.get('Authorization')
        user_id, user_type, error_response = get_user_id_from_token(auth_header)
        
        if error_response:
            return error_response
        
        # Anexa as informações à requisição
        request.user_id = user_id
        request.user_type = user_type
        
        return f(*args, **kwargs)
    
    return decorated_function
