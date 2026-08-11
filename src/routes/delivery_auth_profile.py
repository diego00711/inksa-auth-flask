# inksa-auth-flask/src/routes/delivery_auth_profile.py - VERSÃO FINAL E CORRIGIDA

import os
import uuid
import traceback
import json
import logging
import re
import time
from flask import Blueprint, request, jsonify, g
import psycopg2
import psycopg2.extras
from datetime import datetime, date, time as dt_time, timedelta
from decimal import Decimal
from functools import wraps
from flask_cors import cross_origin

from ..utils.helpers import get_db_connection, get_user_id_from_token, supabase
from ..utils.geocoding_utils import geocode_address

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

delivery_auth_profile_bp = Blueprint('delivery_auth_profile', __name__)

# ==============================================
# DECORADOR DE AUTENTICAÇÃO
# ==============================================
def delivery_token_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if request.method == 'OPTIONS':
            return f(*args, **kwargs)
        auth_header = request.headers.get('Authorization')
        if not auth_header:
            return jsonify({"error": "Token de autorização ausente"}), 401
        token_result = get_user_id_from_token(auth_header)
        if isinstance(token_result, tuple) and len(token_result) == 3:
            user_auth_id, user_type, error = token_result
            if error:
                return error
        else:
            return jsonify({"error": "Resposta de validação de token inesperada"}), 500
        if user_type != 'delivery':
            return jsonify({"error": "Acesso não autorizado. Apenas para entregadores."}), 403
        g.user_auth_id = str(user_auth_id)
        return f(*args, **kwargs)
    return decorated_function

# ==============================================
# HEARTBEAT
# ==============================================
@delivery_auth_profile_bp.route('/heartbeat', methods=['POST', 'OPTIONS'])
@delivery_token_required
def delivery_heartbeat():
    """Sinal de vida do app do entregador enquanto ele está online.

    Existe porque `is_available` era uma flag PEGAJOSA: o entregador ligava o
    botão e ela só voltava a false se ele deslogasse pelo menu ou se o timer de
    inatividade (JS, dentro do layout) chegasse ao fim com o app ABERTO. App
    fechado, celular que mata o processo, bateria acabando — nada disso
    desligava. Havia entregador "online" há 25 horas.

    Um job no scheduler desliga quem parou de bater. Mesmo desenho do
    `/api/restaurant/heartbeat`, que já resolve isso do lado da loja.

    Aceita lat/lng opcionais: assim o mesmo ping que prova que ele está vivo
    também alimenta `current_lat/lng`, que é o que o motor de despacho usa pra
    achar quem está perto. Sem isso o entregador aparece online e não recebe
    nada.
    """
    if request.method == 'OPTIONS':
        return jsonify({}), 204

    body = request.get_json(silent=True) or {}
    lat, lng = body.get('latitude'), body.get('longitude')
    try:
        lat = float(lat) if lat is not None else None
        lng = float(lng) if lng is not None else None
        # Coordenada fora do mundo é lixo: ignora em vez de gravar e sumir com
        # o entregador do raio.
        if lat is not None and not (-90 <= lat <= 90):
            lat = None
        if lng is not None and not (-180 <= lng <= 180):
            lng = None
    except (TypeError, ValueError):
        lat = lng = None

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Erro de conexão com o banco"}), 500
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            if lat is not None and lng is not None:
                cur.execute("""
                    UPDATE delivery_profiles
                       SET last_heartbeat = NOW(),
                           current_lat = %s, current_lng = %s,
                           location_updated_at = NOW()
                     WHERE user_id = %s
                 RETURNING is_available
                """, (lat, lng, g.user_auth_id))
            else:
                cur.execute("""
                    UPDATE delivery_profiles
                       SET last_heartbeat = NOW()
                     WHERE user_id = %s
                 RETURNING is_available
                """, (g.user_auth_id,))
            row = cur.fetchone()
            conn.commit()
        if not row:
            return jsonify({"error": "Perfil não encontrado"}), 404
        # Devolve o estado do servidor: se o job desligou o entregador, o app
        # descobre no ping seguinte e corrige o botão sozinho.
        return jsonify({"status": "success", "data": {"is_available": row["is_available"]}}), 200
    except Exception:
        logger.exception("Erro no heartbeat do entregador")
        try:
            conn.rollback()
        except Exception:
            pass
        return jsonify({"error": "Erro ao registrar heartbeat"}), 500
    finally:
        conn.close()


# ==============================================
# CLASSES E FUNÇÕES AUXILIARES
# ==============================================
class CustomJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal): 
            return float(obj)
        if isinstance(obj, (datetime, date, dt_time, timedelta)): 
            return obj.isoformat()
        if isinstance(obj, uuid.UUID): 
            return str(obj)
        return super().default(obj)

def serialize_data(data):
    return json.loads(json.dumps(data, cls=CustomJSONEncoder))

def sanitize_text(text):
    if not text: 
        return text
    return re.sub(r'[\x00-\x1F\x7F]', '', text.strip())

# ==============================================
# ROTA DE PERFIL
# ==============================================
@delivery_auth_profile_bp.route('/profile', methods=['GET', 'PUT'])
@cross_origin()
@delivery_token_required
def handle_profile():
    conn = None
    try:
        user_id = g.user_auth_id
        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Erro de conexão com o banco de dados"}), 500

        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT * FROM delivery_profiles WHERE user_id = %s", (user_id,))
            profile = cur.fetchone()

            if not profile:
                cur.execute(
                    """INSERT INTO delivery_profiles (user_id, first_name, phone) 
                       VALUES (%s, 'Novo Entregador', '00000000000') RETURNING *""",
                    (user_id,)
                )
                profile = cur.fetchone()
                conn.commit()
                logger.info(f"Novo perfil de entregador criado para user_id: {user_id}")

            profile_id = profile['id']

            if request.method == 'GET':
                return jsonify({"data": serialize_data(dict(profile))}), 200

            elif request.method == 'PUT':
                if not request.is_json:
                    return jsonify({"error": "Content-Type deve ser application/json"}), 400
                
                data = request.get_json()
                allowed_fields = [
                    'first_name', 'last_name', 'phone', 'cpf', 'birth_date', 'vehicle_type',
                    'vehicle_plate', 'vehicle_model', 'vehicle_color', 'cnh', 'cnh_category',
                    'address_street', 'address_number', 'address_complement', 'address_neighborhood',
                    'address_city', 'address_state', 'address_zipcode', 'is_available',
                    'bank_name', 'bank_agency', 'bank_account_number', 'bank_account_type',
                    'pix_key', 'pix_key_type', 'payout_frequency', 'daily_goal',
                    'latitude', 'longitude',
                ]

                update_data = {
                    field: sanitize_text(data[field]) if isinstance(data.get(field), str) else data.get(field)
                    for field in allowed_fields if field in data
                }

                # Tipo da chave PIX: normaliza pro conjunto aceito pelo Asaas (a
                # coluna tem CHECK); inválido/vazio vira NULL (auto-pay infere).
                if 'pix_key_type' in update_data:
                    _kt = (update_data['pix_key_type'] or '').strip().upper()
                    update_data['pix_key_type'] = _kt if _kt in ('CPF', 'CNPJ', 'EMAIL', 'PHONE', 'EVP') else None

                # Coluna birth_date é DATE no Postgres: string vazia ('') quebra
                # com "invalid input syntax for type date" -- normaliza pra NULL.
                if update_data.get('birth_date') == '':
                    update_data['birth_date'] = None

                # daily_goal é INTEGER (meta de ganhos diária, em reais): valida
                # e normaliza pra evitar valor negativo ou lixo vindo do app.
                if 'daily_goal' in update_data:
                    try:
                        goal = int(float(update_data['daily_goal'] or 0))
                        update_data['daily_goal'] = max(0, min(goal, 100000))
                    except (ValueError, TypeError):
                        return jsonify({"error": "Meta diária inválida"}), 400

                # vehicle_type tem CHECK constraint (valores fixos): string vazia
                # (campo nao selecionado) nao esta na lista permitida -- normaliza
                # pra NULL em vez de deixar o Postgres rejeitar o UPDATE inteiro.
                if update_data.get('vehicle_type') == '':
                    update_data['vehicle_type'] = None

                # Geocodifica o endereço -> latitude/longitude (fallback server-side,
                # igual ao restaurante). Sem coordenadas, o dispatch não consegue
                # filtrar o entregador por raio (COALESCE(current_lat, latitude)) e
                # ele fica "online" sem receber pedido nenhum. Só roda quando algum
                # campo de endereço mudou E o app não mandou lat/lng explícitas.
                _addr_fields = ('address_street', 'address_number', 'address_neighborhood',
                                'address_city', 'address_state', 'address_zipcode')
                _addr_changed = any(f in update_data for f in _addr_fields)
                _has_new_coords = (update_data.get('latitude') is not None
                                   and update_data.get('longitude') is not None)
                if _addr_changed and not _has_new_coords:
                    def _merged(field):
                        return update_data.get(field) if field in update_data else profile.get(field)
                    _lat, _lng = geocode_address(
                        _merged('address_street'), _merged('address_number'),
                        _merged('address_neighborhood'), _merged('address_city'),
                        _merged('address_state'), _merged('address_zipcode'),
                    )
                    if _lat is not None and _lng is not None:
                        update_data['latitude'] = _lat
                        update_data['longitude'] = _lng

                if not update_data:
                    return jsonify({"error": "Nenhum campo válido para atualização"}), 400

                set_clauses = [f'"{field}" = %s' for field in update_data.keys()]
                params = list(update_data.values())

                # Ligar o botão de online É um sinal de vida — precisa carimbar
                # o heartbeat junto. Sem isto o entregador entrava online com o
                # último ping antigo e o job de varredura (que desliga quem está
                # sem sinal há 30 min) o derrubava na primeira passada, poucos
                # minutos depois. Foi exatamente o que aconteceu no primeiro
                # teste em produção.
                if update_data.get('is_available') is True:
                    set_clauses.append('"last_heartbeat" = NOW()')

                params.append(profile_id)

                query = f"UPDATE delivery_profiles SET {', '.join(set_clauses)}, updated_at = NOW() WHERE id = %s RETURNING *"
                
                cur.execute(query, params)
                updated_profile = cur.fetchone()
                conn.commit()
                
                updated_dict = dict(updated_profile)
                logger.info(f"Perfil atualizado com avatar_url: {updated_dict.get('avatar_url')}")
                return jsonify({"data": serialize_data(updated_dict)}), 200

    except Exception as e:
        logger.error(f"Erro em handle_profile: {e}", exc_info=True)
        if conn: 
            conn.rollback()
        return jsonify({"error": "Erro interno do servidor"}), 500
    finally:
        if conn: 
            conn.close()

# ==============================================
# ROTA DE UPLOAD DE AVATAR (CORRIGIDA)
# ==============================================
@delivery_auth_profile_bp.route('/upload-avatar', methods=['POST'])
@cross_origin()
@delivery_token_required
def upload_avatar():
    if 'avatar' not in request.files or not request.files['avatar'].filename:
        return jsonify({"error": "Nenhum arquivo válido enviado"}), 400

    avatar_file = request.files['avatar']
    file_ext = avatar_file.filename.rsplit('.', 1)[1].lower()
    if file_ext not in {'png', 'jpg', 'jpeg', 'gif', 'webp'}:
        return jsonify({"error": "Tipo de arquivo não permitido"}), 400

    conn = None
    try:
        user_id = g.user_auth_id
        conn = get_db_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT id FROM delivery_profiles WHERE user_id = %s", (user_id,))
            profile = cur.fetchone()
            if not profile:
                return jsonify({"error": "Perfil não encontrado"}), 404
            profile_id = profile['id']

        bucket_name = "delivery-avatars"
        
        # ✅ CORREÇÃO 1: Gerar nome único para evitar conflitos
        timestamp = str(int(time.time()))
        file_path = f"public/{profile_id}_{timestamp}.{file_ext}"
        file_content = avatar_file.read()

        # ✅ CORREÇÃO 2: Tentar remover arquivo antigo primeiro (opcional)
        try:
            # Listar arquivos existentes para este perfil
            file_list = supabase.storage.from_(bucket_name).list("public")
            old_files = [f for f in file_list if f['name'].startswith(f"{profile_id}_")]
            
            if old_files:
                old_file_paths = [f"public/{f['name']}" for f in old_files]
                supabase.storage.from_(bucket_name).remove(old_file_paths)
                logger.info(f"Arquivos antigos removidos: {old_file_paths}")
                
        except Exception as cleanup_error:
            # Se não conseguir limpar arquivos antigos, continua anyway
            logger.warning(f"Não foi possível limpar arquivos antigos: {cleanup_error}")

        # ✅ CORREÇÃO 3: Upload com retry melhorado
        max_retries = 3
        retry_count = 0
        
        while retry_count < max_retries:
            try:
                result = supabase.storage.from_(bucket_name).upload(
                    path=file_path,
                    file=file_content,
                    file_options={"content-type": avatar_file.content_type}
                )
                logger.info(f"Upload bem-sucedido: {file_path}")
                break  # Upload bem-sucedido, sair do loop
                
            except Exception as upload_error:
                retry_count += 1
                error_msg = str(upload_error).lower()
                
                if "already exists" in error_msg or "duplicate" in error_msg:
                    # Se ainda existe, gerar novo nome
                    timestamp = str(int(time.time()) + retry_count)
                    file_path = f"public/{profile_id}_{timestamp}.{file_ext}"
                    logger.warning(f"Arquivo duplicado, tentando novo nome: {file_path}")
                    
                    if retry_count >= max_retries:
                        logger.error(f"Máximo de tentativas atingido para upload")
                        return jsonify({"error": "Erro ao fazer upload - arquivo duplicado"}), 500
                else:
                    # Outro tipo de erro
                    logger.error(f"Erro no upload (tentativa {retry_count}): {upload_error}")
                    if retry_count >= max_retries:
                        return jsonify({"error": "Erro ao fazer upload da imagem"}), 500
                    
                time.sleep(1)  # Aguardar 1 segundo antes de tentar novamente

        # ✅ CORREÇÃO 4: Obter URL pública corretamente
        try:
            public_url = supabase.storage.from_(bucket_name).get_public_url(file_path)
            logger.info(f"URL pública gerada: {public_url}")
        except Exception as url_error:
            logger.error(f"Erro ao gerar URL pública: {url_error}")
            return jsonify({"error": "Erro ao gerar URL da imagem"}), 500

        # ✅ CORREÇÃO 5: Atualizar banco de dados
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE delivery_profiles SET avatar_url = %s WHERE id = %s", (public_url, profile_id))
                conn.commit()
                logger.info(f"Avatar URL atualizada no banco para profile_id: {profile_id}")
        except Exception as db_error:
            logger.error(f"Erro ao atualizar banco de dados: {db_error}")
            return jsonify({"error": "Erro ao salvar URL da imagem"}), 500

        return jsonify({"avatar_url": public_url}), 200

    except Exception as e:
        logger.error(f"Erro geral no upload de avatar: {e}", exc_info=True)
        if conn: 
            conn.rollback()
        return jsonify({"error": "Erro interno durante o upload"}), 500
    finally:
        if conn: 
            conn.close()

# ==============================================
# ROTA DE OBTER AVATAR
# ==============================================
@delivery_auth_profile_bp.route('/avatar', methods=['GET'])
@cross_origin()
@delivery_token_required
def get_avatar():
    conn = None
    try:
        user_id = g.user_auth_id
        conn = get_db_connection()
        
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT avatar_url FROM delivery_profiles WHERE user_id = %s", (user_id,))
            profile = cur.fetchone()
            
            if not profile:
                return jsonify({"error": "Perfil não encontrado"}), 404
            
            avatar_url = profile['avatar_url']
            return jsonify({"avatar_url": avatar_url}), 200

    except Exception as e:
        logger.error(f"Erro ao obter avatar: {e}", exc_info=True)
        return jsonify({"error": "Erro interno do servidor"}), 500
    finally:
        if conn: 
            conn.close()

# ==============================================
# ROTA DE DELETAR AVATAR
# ==============================================
@delivery_auth_profile_bp.route('/delete-avatar', methods=['DELETE'])
@cross_origin()
@delivery_token_required
def delete_avatar():
    conn = None
    try:
        user_id = g.user_auth_id
        conn = get_db_connection()
        
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT id, avatar_url FROM delivery_profiles WHERE user_id = %s", (user_id,))
            profile = cur.fetchone()
            
            if not profile:
                return jsonify({"error": "Perfil não encontrado"}), 404
            
            profile_id = profile['id']
            current_avatar_url = profile['avatar_url']
            
            # Remover arquivos do Supabase Storage
            try:
                bucket_name = "delivery-avatars"
                file_list = supabase.storage.from_(bucket_name).list("public")
                user_files = [f for f in file_list if f['name'].startswith(f"{profile_id}_")]
                
                if user_files:
                    file_paths = [f"public/{f['name']}" for f in user_files]
                    supabase.storage.from_(bucket_name).remove(file_paths)
                    logger.info(f"Arquivos de avatar removidos: {file_paths}")
                    
            except Exception as storage_error:
                logger.error(f"Erro ao remover arquivos do storage: {storage_error}")
                # Continua mesmo se não conseguir deletar do storage
            
            # Remover URL do banco de dados
            cur.execute("UPDATE delivery_profiles SET avatar_url = NULL WHERE id = %s", (profile_id,))
            conn.commit()
            
            return jsonify({"message": "Avatar removido com sucesso"}), 200

    except Exception as e:
        logger.error(f"Erro ao deletar avatar: {e}", exc_info=True)
        if conn: 
            conn.rollback()
        return jsonify({"error": "Erro interno do servidor"}), 500
    finally:
        if conn: 
            conn.close()
