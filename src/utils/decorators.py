# src/utils/decorators.py - NOVO ARQUIVO

from functools import wraps
from flask import request, jsonify
from .helpers import get_user_id_from_token # Importa a função auxiliar

def admin_required(f):
    """
    Decorator que verifica se o token é válido e pertence a um usuário do tipo 'admin'.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if request.method == 'OPTIONS':
            return jsonify(), 200
            
        auth_header = request.headers.get('Authorization')
        user_id, user_type, error_response = get_user_id_from_token(auth_header)
        
        if error_response:
            return error_response
        
        if user_type != 'admin':
            return jsonify({"error": "Acesso negado. Apenas administradores."}), 403
        
        # Adiciona user_id ao contexto da requisição para uso posterior, se necessário
        request.user_id = user_id
        
        return f(*args, **kwargs)
    
    return decorated_function

def delivery_token_required(f):
    """
    Decorator que verifica se o token é válido e pertence a um usuário do tipo 'delivery'.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if request.method == 'OPTIONS':
            return jsonify(), 200
            
        auth_header = request.headers.get('Authorization')
        user_id, user_type, error_response = get_user_id_from_token(auth_header)
        
        if error_response:
            return error_response
        
        if user_type != 'delivery':
            return jsonify({"error": "Acesso negado. Apenas entregadores."}), 403
        
        # Adiciona user_id e user_type ao contexto da requisição
        request.user_id = user_id
        request.user_type = user_type
        
        return f(*args, **kwargs)
    
    return decorated_function

def user_token_required(f):
    """
    Decorator genérico que apenas valida o token e anexa as informações do usuário à requisição.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if request.method == 'OPTIONS':
            return jsonify(), 200
            
        auth_header = request.headers.get('Authorization')
        user_id, user_type, error_response = get_user_id_from_token(auth_header)
        
        if error_response:
            return error_response
        
        request.user_id = user_id
        request.user_type = user_type
        
        return f(*args, **kwargs)
    
    return decorated_function
