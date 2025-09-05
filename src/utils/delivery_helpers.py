import json
from datetime import date, datetime, timedelta, time
from decimal import Decimal
import uuid
from functools import wraps
from flask import jsonify, g, request  # Adicionei 'request' aqui
import traceback
import psycopg2.extras
from ..utils.helpers import get_db_connection, get_user_id_from_token  # Adicionei estas importações

class DeliveryJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        if isinstance(obj, (datetime, date, timedelta, time)):
            return obj.isoformat()
        if isinstance(obj, uuid.UUID):
            return str(obj)
        return super().default(obj)

def serialize_delivery_data(data):
    return json.loads(json.dumps(data, cls=DeliveryJSONEncoder))

def delivery_token_required(f):
    """
    Decorator específico para delivery que inclui verificação de perfil.
    Para uso geral com apenas verificação de tipo de usuário, use helpers.delivery_token_required.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Permitir requisições OPTIONS sem autenticação
        if request.method == 'OPTIONS':
            return jsonify(), 200
            
        conn = None 
        try:
            auth_header = request.headers.get('Authorization')
            if not auth_header:
                return jsonify({"status": "error", "message": "Token de autorização não fornecido"}), 401
                
            user_auth_id, user_type, error_response = get_user_id_from_token(auth_header)
            
            if error_response:
                return error_response
            
            if user_type != 'delivery':
                return jsonify({"status": "error", "message": "Acesso não autorizado. Apenas para entregadores."}), 403
            
            conn = get_db_connection()
            if not conn:
                return jsonify({"status": "error", "message": "Erro de conexão com o banco de dados"}), 500
            
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute("SELECT id, user_id FROM delivery_profiles WHERE user_id = %s", (user_auth_id,))
                profile = cur.fetchone()
            
            if not profile:
                return jsonify({"status": "error", "message": "Perfil de entregador não encontrado para este usuário"}), 404
            
            g.profile_id = str(profile['id']) 

            if 'profile_id' in kwargs and kwargs['profile_id'] != g.profile_id:
                return jsonify({"status": "error", "message": "ID do perfil na URL não corresponde ao token."}), 403

            return f(*args, **kwargs)

        except psycopg2.Error as e:
            traceback.print_exc()
            return jsonify({"status": "error", "message": "Erro de banco de dados", "detail": str(e)}), 500
        except Exception as e:
            traceback.print_exc()
            return jsonify({"status": "error", "message": "Erro interno do servidor", "detail": str(e)}), 500
        finally:
            if conn:
                conn.close()
    
    return decorated_function