# src/routes/upload.py - VERSÃO CORRIGIDA
import os
import uuid
import logging
from datetime import datetime
from flask import Blueprint, request, jsonify
from werkzeug.utils import secure_filename
from ..utils.helpers import supabase, get_user_id_from_token

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

upload_bp = Blueprint('upload', __name__)

# Configurações de upload
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
MAX_FILE_SIZE = 5 * 1024 * 1024  # 5MB

def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def get_file_extension(filename):
    return filename.rsplit('.', 1)[1].lower() if '.' in filename else ''

@upload_bp.route('/banner-image', methods=['POST'])
def upload_banner_image():
    """Upload de imagem para banners."""
    logger.info("=== INÍCIO upload_banner_image ===")
    
    try:
        # Verificar autenticação
        user_auth_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
        if error:
            return error
        
        if user_type not in ['admin', 'restaurant']:
            return jsonify({"error": "Apenas administradores podem fazer upload de imagens"}), 403
        
        # Verificar se foi enviado um arquivo
        if 'image' not in request.files:
            return jsonify({"error": "Nenhum arquivo foi enviado"}), 400
        
        file = request.files['image']
        
        if file.filename == '':
            return jsonify({"error": "Nenhum arquivo selecionado"}), 400
        
        # Verificar tipo de arquivo
        if not allowed_file(file.filename):
            return jsonify({"error": f"Tipo de arquivo não permitido. Use: {', '.join(ALLOWED_EXTENSIONS)}"}), 400
        
        # Verificar tamanho do arquivo
        file.seek(0, os.SEEK_END)
        file_size = file.tell()
        file.seek(0)
        
        if file_size > MAX_FILE_SIZE:
            return jsonify({"error": f"Arquivo muito grande. Máximo: {MAX_FILE_SIZE // (1024*1024)}MB"}), 400
        
        # Gerar nome único para o arquivo
        file_extension = get_file_extension(file.filename)
        unique_filename = f"banner_{uuid.uuid4().hex}_{int(datetime.now().timestamp())}.{file_extension}"
        
        # Upload para o Supabase Storage
        try:
            # Ler conteúdo do arquivo
            file_content = file.read()
            logger.info(f"Arquivo lido: {len(file_content)} bytes")
            
            # CORREÇÃO: Upload para o bucket 'banner-images' com a sintaxe correta
            response = supabase.storage.from_('banner-images').upload(
                path=unique_filename,
                file=file_content,
                file_options={
                    "content-type": f"image/{file_extension}"
                }
            )
            
            logger.info(f"Resposta do upload: {response}")
            
            # Verificar se houve erro no upload
            if hasattr(response, 'error') and response.error:
                logger.error(f"Erro no upload para Supabase: {response.error}")
                return jsonify({"error": f"Erro ao fazer upload: {response.error}"}), 500
            
            # Obter URL pública da imagem
            try:
                public_url_response = supabase.storage.from_('banner-images').get_public_url(unique_filename)
                
                if hasattr(public_url_response, 'error') and public_url_response.error:
                    logger.error(f"Erro ao obter URL pública: {public_url_response.error}")
                    return jsonify({"error": "Erro ao gerar URL pública da imagem"}), 500
                
                # A URL pode estar em diferentes formatos dependendo da versão do Supabase
                if hasattr(public_url_response, 'data'):
                    public_url = public_url_response.data
                elif hasattr(public_url_response, 'publicURL'):
                    public_url = public_url_response.publicURL
                elif isinstance(public_url_response, str):
                    public_url = public_url_response
                else:
                    # Construir URL manualmente se necessário
                    supabase_url = os.environ.get('SUPABASE_URL', '').rstrip('/')
                    public_url = f"{supabase_url}/storage/v1/object/public/banner-images/{unique_filename}"
                
                logger.info(f"Upload realizado com sucesso: {unique_filename}")
                logger.info(f"URL pública: {public_url}")
                
                return jsonify({
                    "status": "success",
                    "message": "Imagem enviada com sucesso",
                    "data": {
                        "filename": unique_filename,
                        "url": public_url,
                        "size": file_size
                    }
                }), 200
                
            except Exception as url_error:
                logger.error(f"Erro ao gerar URL pública: {url_error}")
                # Tentar construir URL manualmente
                supabase_url = os.environ.get('SUPABASE_URL', '').rstrip('/')
                public_url = f"{supabase_url}/storage/v1/object/public/banner-images/{unique_filename}"
                
                return jsonify({
                    "status": "success",
                    "message": "Imagem enviada com sucesso",
                    "data": {
                        "filename": unique_filename,
                        "url": public_url,
                        "size": file_size
                    }
                }), 200
            
        except Exception as storage_error:
            logger.error(f"Erro no storage do Supabase: {storage_error}", exc_info=True)
            return jsonify({"error": f"Erro interno no serviço de upload: {str(storage_error)}"}), 500
        
    except Exception as e:
        logger.error(f"Erro inesperado em upload_banner_image: {e}", exc_info=True)
        return jsonify({"error": "Erro interno no servidor"}), 500


@upload_bp.route('/banner-image/<filename>', methods=['DELETE'])
def delete_banner_image(filename):
    """Deletar imagem de banner."""
    logger.info(f"=== INÍCIO delete_banner_image: {filename} ===")
    
    try:
        # Verificar autenticação
        user_auth_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
        if error:
            return error
        
        if user_type not in ['admin', 'restaurant']:
            return jsonify({"error": "Apenas administradores podem deletar imagens"}), 403
        
        # Verificar se o filename é seguro
        if not filename or '..' in filename or '/' in filename:
            return jsonify({"error": "Nome de arquivo inválido"}), 400
        
        try:
            # Deletar do Supabase Storage
            response = supabase.storage.from_('banner-images').remove([filename])
            
            if hasattr(response, 'error') and response.error:
                logger.error(f"Erro ao deletar do Supabase: {response.error}")
                return jsonify({"error": "Erro ao deletar imagem"}), 500
            
            logger.info(f"Imagem deletada com sucesso: {filename}")
            
            return jsonify({
                "status": "success",
                "message": "Imagem deletada com sucesso"
            }), 200
            
        except Exception as storage_error:
            logger.error(f"Erro no storage do Supabase: {storage_error}")
            return jsonify({"error": "Erro interno no serviço de storage"}), 500
        
    except Exception as e:
        logger.error(f"Erro inesperado em delete_banner_image: {e}", exc_info=True)
        return jsonify({"error": "Erro interno no servidor"}), 500
