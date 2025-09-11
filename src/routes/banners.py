# src/routes/banners.py
import uuid
import json
import logging
from datetime import datetime
from flask import Blueprint, request, jsonify
import psycopg2
import psycopg2.extras
from ..utils.helpers import get_db_connection, get_user_id_from_token

# Configuração do logging
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s'
)
logger = logging.getLogger(__name__)

banners_bp = Blueprint('banners', __name__)

# --- Handler para requisições OPTIONS ---
@banners_bp.before_request
def handle_options():
    if request.method == "OPTIONS":
        response = jsonify()
        response.headers.add("Access-Control-Allow-Origin", request.headers.get("Origin", "*"))
        response.headers.add("Access-Control-Allow-Headers", "Content-Type,Authorization")
        response.headers.add("Access-Control-Allow-Methods", "GET,PUT,POST,DELETE,OPTIONS")
        response.headers.add("Access-Control-Allow-Credentials", "true")
        return response

# --- Rotas da API ---

@banners_bp.route('/', methods=['GET'])
def get_banners():
    """Listar todos os banners (públicos para clientes, completos para admin)."""
    logger.info("=== INÍCIO get_banners ===")
    conn = None
    
    try:
        # Verificar se há token de autenticação
        auth_header = request.headers.get('Authorization')
        is_admin = False
        
        if auth_header:
            user_auth_id, user_type, error = get_user_id_from_token(auth_header)
            # CORREÇÃO: Aceitar tanto admin quanto restaurant
            if not error and user_type in ['admin', 'restaurant']:
                is_admin = True
        
        conn = get_db_connection()
        if not conn:
            logger.error("Falha na conexão com o banco de dados")
            return jsonify({"error": "Erro de conexão com o banco de dados"}), 500

        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            if is_admin:
                # Admin vê todos os banners
                query = """
                    SELECT id, title, subtitle, image_url, link_url, is_active, 
                           display_order, created_at, updated_at
                    FROM banners 
                    ORDER BY display_order ASC, created_at DESC
                """
                cur.execute(query)
            else:
                # Clientes veem apenas banners ativos
                query = """
                    SELECT id, title, subtitle, image_url, link_url, display_order
                    FROM banners 
                    WHERE is_active = true 
                    ORDER BY display_order ASC, created_at DESC
                """
                cur.execute(query)
            
            banners = [dict(row) for row in cur.fetchall()]
            
            logger.info(f"Encontrados {len(banners)} banners")
            return jsonify({"status": "success", "data": banners}), 200

    except Exception as e:
        logger.error(f"Erro inesperado em get_banners: {e}", exc_info=True)
        return jsonify({"error": "Erro interno no servidor"}), 500
    finally:
        if conn:
            conn.close()
            logger.info("Conexão com banco fechada em get_banners")


@banners_bp.route('/', methods=['POST'])
def create_banner():
    """Criar um novo banner (apenas admin)."""
    logger.info("=== INÍCIO create_banner ===")
    conn = None
    
    try:
        user_auth_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
        if error:
            logger.warning(f"Erro de autenticação: {error}")
            return error
        
        # CORREÇÃO: Aceitar tanto admin quanto restaurant
        if user_type not in ['admin', 'restaurant']:
            return jsonify({"error": "Apenas administradores podem criar banners"}), 403
        
        data = request.get_json()
        required_fields = ['title', 'image_url']
        if any(field not in data for field in required_fields):
            return jsonify({"error": "Campos obrigatórios: title, image_url"}), 400

        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Erro de conexão com o banco de dados"}), 500

        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # Obter o próximo display_order
            cur.execute("SELECT COALESCE(MAX(display_order), -1) + 1 FROM banners")
            next_order = cur.fetchone()[0]
            
            banner_data = {
                'id': str(uuid.uuid4()),
                'title': data['title'],
                'subtitle': data.get('subtitle'),
                'image_url': data['image_url'],
                'link_url': data.get('link_url'),
                'is_active': data.get('is_active', True),
                'display_order': data.get('display_order', next_order),
                'created_at': datetime.now(),
                'updated_at': datetime.now()
            }
            
            columns = ', '.join(banner_data.keys())
            placeholders = ', '.join(['%s'] * len(banner_data))
            
            cur.execute(
                f"INSERT INTO banners ({columns}) VALUES ({placeholders}) RETURNING *",
                list(banner_data.values())
            )
            new_banner = cur.fetchone()
            conn.commit()
            
            logger.info(f"Banner criado com sucesso: {new_banner['id']}")
            return jsonify({
                "status": "success", 
                "message": "Banner criado com sucesso", 
                "data": dict(new_banner)
            }), 201

    except Exception as e:
        logger.error(f"Erro inesperado em create_banner: {e}", exc_info=True)
        if conn:
            conn.rollback()
        return jsonify({"error": "Erro interno no servidor"}), 500
    finally:
        if conn:
            conn.close()
            logger.info("Conexão com banco fechada em create_banner")


@banners_bp.route('/<uuid:banner_id>', methods=['GET'])
def get_banner(banner_id):
    """Obter um banner específico."""
    logger.info(f"=== INÍCIO get_banner para {banner_id} ===")
    conn = None
    
    try:
        # Verificar se é admin para mostrar dados completos
        auth_header = request.headers.get('Authorization')
        is_admin = False
        
        if auth_header:
            user_auth_id, user_type, error = get_user_id_from_token(auth_header)
            # CORREÇÃO: Aceitar tanto admin quanto restaurant
            if not error and user_type in ['admin', 'restaurant']:
                is_admin = True

        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Erro de conexão com o banco de dados"}), 500

        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            if is_admin:
                query = "SELECT * FROM banners WHERE id = %s"
            else:
                query = "SELECT id, title, subtitle, image_url, link_url, display_order FROM banners WHERE id = %s AND is_active = true"
            
            cur.execute(query, (str(banner_id),))
            banner = cur.fetchone()
            
            if not banner:
                return jsonify({"error": "Banner não encontrado"}), 404
            
            return jsonify({"status": "success", "data": dict(banner)}), 200

    except Exception as e:
        logger.error(f"Erro em get_banner: {e}", exc_info=True)
        return jsonify({"error": "Erro interno no servidor"}), 500
    finally:
        if conn:
            conn.close()


@banners_bp.route('/<uuid:banner_id>', methods=['PUT'])
def update_banner(banner_id):
    """Atualizar um banner (apenas admin)."""
    logger.info(f"=== INÍCIO update_banner para {banner_id} ===")
    conn = None
    
    try:
        user_auth_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
        if error:
            return error
        
        # CORREÇÃO: Aceitar tanto admin quanto restaurant
        if user_type not in ['admin', 'restaurant']:
            return jsonify({"error": "Apenas administradores podem atualizar banners"}), 403

        data = request.get_json()
        if not data:
            return jsonify({"error": "Dados não fornecidos"}), 400

        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Erro de conexão com o banco de dados"}), 500

        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # Verificar se o banner existe
            cur.execute("SELECT * FROM banners WHERE id = %s", (str(banner_id),))
            banner = cur.fetchone()
            
            if not banner:
                return jsonify({"error": "Banner não encontrado"}), 404

            # Campos que podem ser atualizados
            update_fields = []
            update_values = []
            
            updatable_fields = ['title', 'subtitle', 'image_url', 'link_url', 'is_active', 'display_order']
            
            for field in updatable_fields:
                if field in data:
                    update_fields.append(f"{field} = %s")
                    update_values.append(data[field])
            
            if not update_fields:
                return jsonify({"error": "Nenhum campo válido para atualização"}), 400
            
            # Adicionar updated_at
            update_fields.append("updated_at = %s")
            update_values.append(datetime.now())
            update_values.append(str(banner_id))
            
            query = f"UPDATE banners SET {', '.join(update_fields)} WHERE id = %s RETURNING *"
            cur.execute(query, update_values)
            updated_banner = cur.fetchone()
            conn.commit()
            
            logger.info(f"Banner {banner_id} atualizado com sucesso")
            return jsonify({
                "status": "success", 
                "message": "Banner atualizado com sucesso", 
                "data": dict(updated_banner)
            }), 200

    except Exception as e:
        logger.error(f"Erro em update_banner: {e}", exc_info=True)
        if conn:
            conn.rollback()
        return jsonify({"error": "Erro interno no servidor"}), 500
    finally:
        if conn:
            conn.close()


@banners_bp.route('/<uuid:banner_id>', methods=['DELETE'])
def delete_banner(banner_id):
    """Deletar um banner (apenas admin)."""
    logger.info(f"=== INÍCIO delete_banner para {banner_id} ===")
    conn = None
    
    try:
        user_auth_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
        if error:
            return error
        
        # CORREÇÃO: Aceitar tanto admin quanto restaurant
        if user_type not in ['admin', 'restaurant']:
            return jsonify({"error": "Apenas administradores podem deletar banners"}), 403

        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Erro de conexão com o banco de dados"}), 500

        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # Verificar se o banner existe
            cur.execute("SELECT id FROM banners WHERE id = %s", (str(banner_id),))
            banner = cur.fetchone()
            
            if not banner:
                return jsonify({"error": "Banner não encontrado"}), 404
            
            # Deletar o banner
            cur.execute("DELETE FROM banners WHERE id = %s", (str(banner_id),))
            conn.commit()
            
            logger.info(f"Banner {banner_id} deletado com sucesso")
            return jsonify({"status": "success", "message": "Banner deletado com sucesso"}), 200

    except Exception as e:
        logger.error(f"Erro em delete_banner: {e}", exc_info=True)
        if conn:
            conn.rollback()
        return jsonify({"error": "Erro interno no servidor"}), 500
    finally:
        if conn:
            conn.close()


@banners_bp.route('/<uuid:banner_id>/toggle-status', methods=['PUT'])
def toggle_banner_status(banner_id):
    """Ativar/Desativar um banner (apenas admin)."""
    logger.info(f"=== INÍCIO toggle_banner_status para {banner_id} ===")
    conn = None
    
    try:
        user_auth_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
        if error:
            return error
        
        # CORREÇÃO: Aceitar tanto admin quanto restaurant
        if user_type not in ['admin', 'restaurant']:
            return jsonify({"error": "Apenas administradores podem alterar status de banners"}), 403

        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Erro de conexão com o banco de dados"}), 500

        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # Obter status atual
            cur.execute("SELECT is_active FROM banners WHERE id = %s", (str(banner_id),))
            banner = cur.fetchone()
            
            if not banner:
                return jsonify({"error": "Banner não encontrado"}), 404
            
            # Inverter o status
            new_status = not banner['is_active']
            
            cur.execute(
                "UPDATE banners SET is_active = %s, updated_at = %s WHERE id = %s RETURNING *",
                (new_status, datetime.now(), str(banner_id))
            )
            updated_banner = cur.fetchone()
            conn.commit()
            
            status_text = "ativado" if new_status else "desativado"
            logger.info(f"Banner {banner_id} {status_text} com sucesso")
            
            return jsonify({
                "status": "success", 
                "message": f"Banner {status_text} com sucesso", 
                "data": dict(updated_banner)
            }), 200

    except Exception as e:
        logger.error(f"Erro em toggle_banner_status: {e}", exc_info=True)
        if conn:
            conn.rollback()
        return jsonify({"error": "Erro interno no servidor"}), 500
    finally:
        if conn:
            conn.close()


@banners_bp.route('/reorder', methods=['PUT'])
def reorder_banners():
    """Reordenar banners (apenas admin)."""
    logger.info("=== INÍCIO reorder_banners ===")
    conn = None
    
    try:
        user_auth_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
        if error:
            return error
        
        # CORREÇÃO: Aceitar tanto admin quanto restaurant
        if user_type not in ['admin', 'restaurant']:
            return jsonify({"error": "Apenas administradores podem reordenar banners"}), 403

        data = request.get_json()
        if not data or 'banner_orders' not in data:
            return jsonify({"error": "Campo 'banner_orders' é obrigatório"}), 400
        
        banner_orders = data['banner_orders']
        if not isinstance(banner_orders, list):
            return jsonify({"error": "banner_orders deve ser uma lista"}), 400

        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Erro de conexão com o banco de dados"}), 500

        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # Atualizar a ordem de cada banner
            for item in banner_orders:
                if 'id' not in item or 'display_order' not in item:
                    return jsonify({"error": "Cada item deve conter 'id' e 'display_order'"}), 400
                
                cur.execute(
                    "UPDATE banners SET display_order = %s, updated_at = %s WHERE id = %s",
                    (item['display_order'], datetime.now(), item['id'])
                )
            
            conn.commit()
            logger.info("Banners reordenados com sucesso")
            
            return jsonify({"status": "success", "message": "Banners reordenados com sucesso"}), 200

    except Exception as e:
        logger.error(f"Erro em reorder_banners: {e}", exc_info=True)
        if conn:
            conn.rollback()
        return jsonify({"error": "Erro interno no servidor"}), 500
    finally:
        if conn:
            conn.close()


@banners_bp.route('/stats', methods=['GET'])
def get_banner_stats():
    """Obter estatísticas dos banners (apenas admin)."""
    logger.info("=== INÍCIO get_banner_stats ===")
    conn = None
    
    try:
        user_auth_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
        if error:
            return error
        
        # CORREÇÃO: Aceitar tanto admin quanto restaurant
        if user_type not in ['admin', 'restaurant']:
            return jsonify({"error": "Apenas administradores podem ver estatísticas"}), 403

        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Erro de conexão com o banco de dados"}), 500

        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # Contar banners por status
            cur.execute("""
                SELECT 
                    COUNT(*) as total,
                    COUNT(*) FILTER (WHERE is_active = true) as active,
                    COUNT(*) FILTER (WHERE is_active = false) as inactive,
                    MIN(created_at) as oldest_banner,
                    MAX(created_at) as newest_banner
                FROM banners
            """)
            stats = cur.fetchone()
            
            return jsonify({"status": "success", "data": dict(stats)}), 200

    except Exception as e:
        logger.error(f"Erro em get_banner_stats: {e}", exc_info=True)
        return jsonify({"error": "Erro interno no servidor"}), 500
    finally:
        if conn:
            conn.close()
