# inksa-auth-flask/src/routes/delivery_auth_profile.py

import os
import uuid
import traceback
import json
import logging
import re
from flask import Blueprint, request, jsonify, g
import psycopg2
import psycopg2.extras
from datetime import datetime, date, time, timedelta
from decimal import Decimal
from functools import wraps
from flask_cors import cross_origin

from ..utils.helpers import get_db_connection, get_user_id_from_token, supabase

# Configuração de logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

delivery_auth_profile_bp = Blueprint('delivery_auth_profile', __name__)

# ==============================================
# DECORATOR DE AUTENTICAÇÃO
# ==============================================
def delivery_token_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        conn = None
        try:
            # Verifica o header de autorização
            auth_header = request.headers.get('Authorization')
            if not auth_header:
                logger.error("Token de autorização não fornecido")
                return jsonify({
                    "status": "error",
                    "code": "missing_auth_token",
                    "message": "Token de autorização não fornecido"
                }), 401
            
            # Obtém o ID do usuário do token JWT
            user_auth_id, user_type, error_response = get_user_id_from_token(auth_header)
            if error_response:
                return error_response
            
            # Verifica se o usuário é um entregador
            if user_type != 'delivery':
                logger.error(f"Usuário não é entregador: {user_type}")
                return jsonify({
                    "status": "error",
                    "code": "unauthorized_access",
                    "message": "Acesso não autorizado. Apenas para entregadores."
                }), 403
            
            # Conecta ao banco de dados
            conn = get_db_connection()
            if not conn:
                logger.error("Falha na conexão com o banco de dados")
                return jsonify({
                    "status": "error",
                    "code": "database_connection_error",
                    "message": "Erro de conexão com o banco de dados"
                }), 500
            
            # Verifica ou cria o perfil do entregador
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute("SELECT id FROM delivery_profiles WHERE user_id = %s", (user_auth_id,))
                profile = cur.fetchone()
                
                if not profile:
                    # Cria um perfil mínimo se não existir
                    cur.execute(
                        """INSERT INTO delivery_profiles 
                        (user_id, first_name, phone) 
                        VALUES (%s, 'Novo Entregador', '00000000000') 
                        RETURNING id""",
                        (user_auth_id,)
                    )
                    profile = cur.fetchone()
                    conn.commit()
                    logger.info(f"Novo perfil criado para user_id: {user_auth_id}")
                
                # Armazena no contexto global do Flask
                g.profile_id = str(profile['id'])
                g.user_auth_id = str(user_auth_id)
            
            return f(*args, **kwargs)
        
        except psycopg2.Error as e:
            logger.error(f"Erro de banco de dados: {str(e)}")
            traceback.print_exc()
            return jsonify({
                "status": "error",
                "code": "database_error",
                "message": "Erro de banco de dados",
                "detail": str(e)
            }), 500
        except Exception as e:
            logger.error(f"Erro inesperado: {str(e)}")
            traceback.print_exc()
            return jsonify({
                "status": "error",
                "code": "internal_server_error",
                "message": "Erro interno do servidor",
                "detail": str(e)
            }), 500
        finally:
            if conn:
                conn.close()
    
    return decorated_function

# ==============================================
# HELPERS
# ==============================================
class CustomJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        if isinstance(obj, (datetime, date, time, timedelta)):
            return obj.isoformat()
        if isinstance(obj, uuid.UUID):
            return str(obj)
        return super().default(obj)

def serialize_data(data):
    return json.loads(json.dumps(data, cls=CustomJSONEncoder))

def validate_phone(phone):
    """Valida formato de telefone (11 dígitos, apenas números)"""
    phone = re.sub(r'[^0-9]', '', str(phone))
    return len(phone) == 11

def validate_cpf(cpf):
    """Valida formato básico de CPF (11 dígitos)"""
    if not cpf:
        return True  # CPF é opcional
    cpf = re.sub(r'[^0-9]', '', str(cpf))
    return len(cpf) == 11

def sanitize_text(text):
    """Remove espaços extras e caracteres potencialmente perigosos"""
    if not text:
        return text
    return re.sub(r'[\x00-\x1F\x7F]', '', text.strip())

# ==============================================
# ROTAS DE PERFIL
# ==============================================
@delivery_auth_profile_bp.route('/profile', methods=['GET', 'PUT'])
@delivery_token_required
def handle_profile():
    conn = None
    try:
        # Método GET - Retorna o perfil do entregador
        if request.method == 'GET':
            conn = get_db_connection()
            if not conn:
                return jsonify({
                    "status": "error",
                    "code": "database_connection_error",
                    "message": "Erro de conexão com o banco de dados"
                }), 500

            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute("SELECT * FROM delivery_profiles WHERE id = %s", (g.profile_id,))
                profile = cur.fetchone()
                
                if not profile:
                    return jsonify({
                        "status": "error",
                        "code": "profile_not_found",
                        "message": "Perfil não encontrado"
                    }), 404
                
                return jsonify({
                    "status": "success",
                    "data": serialize_data(dict(profile))
                }), 200

        # Método PUT - Atualiza o perfil do entregador
        elif request.method == 'PUT':
            # Verificação dos dados de entrada
            if not request.is_json:
                return jsonify({
                    "status": "error",
                    "code": "invalid_content_type",
                    "message": "Content-Type deve ser application/json"
                }), 400

            data = request.get_json()
            logger.debug(f"Dados recebidos para atualização: {data}")
            
            # Campos obrigatórios com mensagens personalizadas
            required_fields = {
                'first_name': {
                    "message": "Nome é obrigatório",
                    "sanitize": True
                },
                'phone': {
                    "message": "Telefone é obrigatório",
                    "validate": validate_phone,
                    "error_message": "Telefone inválido (deve conter 11 dígitos)"
                }
            }
            
            # Validação dos campos
            errors = []
            sanitized_data = {}
            
            for field, config in required_fields.items():
                value = data.get(field, '')
                
                # Sanitização
                if config.get('sanitize', False):
                    value = sanitize_text(value)
                
                # Verifica se o campo está presente e não vazio
                if not str(value).strip():
                    errors.append({
                        "field": field,
                        "code": "missing_required_field",
                        "message": config["message"]
                    })
                    continue
                
                # Validação específica do campo
                if 'validate' in config and not config['validate'](value):
                    errors.append({
                        "field": field,
                        "code": "invalid_field_format",
                        "message": config.get("error_message", f"Formato inválido para {field}")
                    })
                    continue
                
                sanitized_data[field] = value
            
            # Validação do CPF (se fornecido)
            if 'cpf' in data:
                if not validate_cpf(data['cpf']):
                    errors.append({
                        "field": "cpf",
                        "code": "invalid_cpf_format",
                        "message": "CPF inválido (deve conter 11 dígitos)"
                    })
                else:
                    sanitized_data['cpf'] = re.sub(r'[^0-9]', '', str(data['cpf']))
            
            # Validação do vehicle_type
            if 'vehicle_type' in data:
                valid_vehicle_types = ['bike', 'motorcycle', 'car']
                input_type = str(data['vehicle_type']).strip().lower()
                if input_type not in valid_vehicle_types:
                    errors.append({
                        "field": "vehicle_type",
                        "code": "invalid_vehicle_type",
                        "message": "Tipo de veículo inválido",
                        "valid_types": valid_vehicle_types
                    })
                else:
                    sanitized_data['vehicle_type'] = input_type
            
            if errors:
                return jsonify({
                    "status": "error",
                    "code": "validation_error",
                    "message": "Erros de validação encontrados",
                    "errors": errors
                }), 400

            # Conexão com o banco de dados
            conn = get_db_connection()
            if not conn:
                return jsonify({
                    "status": "error",
                    "code": "database_connection_error",
                    "message": "Erro de conexão com o banco de dados"
                }), 500

            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                # Lista de campos permitidos para atualização
                allowed_fields = [
                    'first_name', 'last_name', 'phone', 'cpf', 'birth_date',
                    'vehicle_type', 'address_street', 'address_number',
                    'address_complement', 'address_neighborhood', 'address_city',
                    'address_state', 'address_zipcode', 'latitude', 'longitude',
                    'bank_name', 'bank_agency', 'bank_account_number',
                    'bank_account_type', 'pix_key', 'payout_frequency', 'mp_account_id',
                    'is_available'
                ]
                
                # Filtra e sanitiza os dados
                update_data = {}
                for field in allowed_fields:
                    if field in data:
                        # Sanitiza campos de texto
                        if isinstance(data[field], str):
                            update_data[field] = sanitize_text(data[field])
                        # Converte strings vazias para None em campos opcionais
                        elif data[field] == '':
                            update_data[field] = None
                        else:
                            update_data[field] = data[field]

                # Combina com os campos obrigatórios já sanitizados
                update_data.update(sanitized_data)

                # Verifica se há campos válidos para atualizar
                if not update_data:
                    return jsonify({
                        "status": "error",
                        "code": "no_valid_fields",
                        "message": "Nenhum campo válido para atualização",
                        "allowed_fields": allowed_fields
                    }), 400

                # Prepara a query dinamicamente
                set_clauses = []
                params = []
                for field, value in update_data.items():
                    set_clauses.append(f'"{field}" = %s')
                    params.append(value)
                params.append(g.profile_id)
                
                query = f"""
                    UPDATE delivery_profiles
                    SET {', '.join(set_clauses)}, updated_at = NOW()
                    WHERE id = %s
                    RETURNING *
                """
                
                logger.debug(f"Query de atualização: {query}")
                logger.debug(f"Parâmetros: {params}")
                
                # Executa a atualização
                cur.execute(query, params)
                updated_profile = cur.fetchone()
                conn.commit()
                
                if not updated_profile:
                    conn.rollback()
                    return jsonify({
                        "status": "error",
                        "code": "profile_not_updated",
                        "message": "Perfil não encontrado ou não modificado"
                    }), 404
                
                logger.info(f"Perfil atualizado com sucesso: {g.profile_id}")
                return jsonify({
                    "status": "success",
                    "message": "Perfil atualizado com sucesso",
                    "data": serialize_data(dict(updated_profile))
                }), 200

    except psycopg2.Error as e:
        if conn:
            conn.rollback()
        logger.error(f"Erro de banco de dados: {str(e)}")
        return jsonify({
            "status": "error",
            "code": "database_error",
            "message": "Erro de banco de dados",
            "detail": str(e)
        }), 500
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Erro inesperado: {str(e)}")
        return jsonify({
            "status": "error",
            "code": "internal_server_error",
            "message": "Erro interno do servidor",
            "detail": str(e)
        }), 500
    finally:
        if conn:
            conn.close()

# ==============================================
# ROTA DE UPLOAD DE AVATAR (mantido igual)
# ==============================================
@delivery_auth_profile_bp.route('/upload-avatar', methods=['POST'])
@cross_origin()
@delivery_token_required
def upload_avatar():
    # Verificação robusta do arquivo
    if 'avatar' not in request.files:
        return jsonify({
            "status": "error",
            "message": "Nenhum arquivo enviado. Use o campo 'avatar' no FormData"
        }), 400
    
    avatar_file = request.files['avatar']
    
    if not avatar_file or avatar_file.filename == '':
        return jsonify({
            "status": "error",
            "message": "Nome de arquivo inválido ou arquivo não selecionado"
        }), 400

    # Verificação de extensão
    allowed_extensions = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
    try:
        file_ext = avatar_file.filename.rsplit('.', 1)[1].lower()
        if '.' not in avatar_file.filename or file_ext not in allowed_extensions:
            return jsonify({
                "status": "error",
                "message": "Tipo de arquivo não permitido",
                "allowed_extensions": list(allowed_extensions),
                "received": file_ext if '.' in avatar_file.filename else 'nenhuma extensão'
            }), 400
    except Exception as e:
        logger.error(f"Erro ao verificar extensão: {str(e)}")
        return jsonify({
            "status": "error",
            "message": "Erro ao processar arquivo"
        }), 400

    try:
        profile_id = g.profile_id
        bucket_name = "delivery-avatars"
        file_path = f"public/{profile_id}.{file_ext}"
        
        # Leitura segura do arquivo
        try:
            file_content = avatar_file.read()
            if not file_content:
                return jsonify({
                    "status": "error",
                    "message": "Arquivo vazio ou corrompido"
                }), 400
        except Exception as e:
            logger.error(f"Erro ao ler arquivo: {str(e)}")
            return jsonify({
                "status": "error",
                "message": "Erro ao processar arquivo"
            }), 400

        # Configurações de upload
        upload_options = {
            "content-type": avatar_file.content_type or f"image/{file_ext}",
            "upsert": True,
            "cache-control": "public, max-age=31536000",
            "x-upsert": "true"
        }

        # Remove arquivo existente
        try:
            supabase.storage.from_(bucket_name).remove([file_path])
        except Exception as e:
            logger.info(f"Arquivo anterior não encontrado ou não removido: {str(e)}")

        # Upload para o Supabase
        try:
            upload_response = supabase.storage.from_(bucket_name).upload(
                path=file_path,
                file=file_content,
                options=upload_options
            )

            if hasattr(upload_response, 'error') and upload_response.error:
                error_msg = str(upload_response.error)
                logger.error(f"Erro no Supabase Storage: {error_msg}")
                return jsonify({
                    "status": "error",
                    "message": "Falha no armazenamento do arquivo",
                    "detail": error_msg
                }), 500

            # Obtém URL pública
            try:
                public_url = supabase.storage.from_(bucket_name).get_public_url(file_path)
            except Exception as e:
                logger.error(f"Erro ao obter URL pública: {str(e)}")
                return jsonify({
                    "status": "error",
                    "message": "Erro ao gerar URL de acesso"
                }), 500

            # Atualiza o banco de dados
            conn = None
            try:
                conn = get_db_connection()
                if not conn:
                    raise Exception("Falha na conexão com o banco de dados")

                with conn.cursor() as cur:
                    cur.execute(
                        """UPDATE delivery_profiles 
                        SET avatar_url = %s 
                        WHERE id = %s 
                        RETURNING avatar_url""",
                        (public_url, profile_id)
                    )
                    conn.commit()
                    
                    if not cur.fetchone():
                        conn.rollback()
                        raise Exception("Falha ao atualizar URL no banco de dados")
                    
                    return jsonify({
                        "status": "success",
                        "message": "Avatar atualizado com sucesso",
                        "avatar_url": public_url,
                        "file_info": {
                            "name": avatar_file.filename,
                            "size": len(file_content),
                            "type": avatar_file.content_type
                        }
                    }), 200
            except Exception as db_error:
                if conn:
                    conn.rollback()
                logger.error(f"Erro no banco de dados: {str(db_error)}")
                return jsonify({
                    "status": "error",
                    "message": "Falha ao atualizar perfil",
                    "detail": str(db_error)
                }), 500
            finally:
                if conn:
                    conn.close()

        except Exception as upload_error:
            logger.error(f"Erro durante upload: {str(upload_error)}", exc_info=True)
            return jsonify({
                "status": "error",
                "message": "Falha no processamento do upload",
                "detail": str(upload_error)
            }), 500

    except Exception as e:
        logger.error(f"Erro geral no endpoint: {str(e)}", exc_info=True)
        return jsonify({
            "status": "error",
            "message": "Erro interno no servidor",
            "detail": str(e)
        }), 500