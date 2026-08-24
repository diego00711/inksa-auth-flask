# src/routes/menu.py - VERSÃO FINAL CORRIGIDA

from flask import request, jsonify, Blueprint
import os
import uuid
import traceback
import psycopg2
import psycopg2.extras
from datetime import datetime, date, time
import logging
from ..utils.helpers import get_db_connection, get_user_id_from_token, supabase
from functools import wraps
from flask_cors import CORS

logging.basicConfig(level=logging.INFO)
menu_bp = Blueprint('menu_bp', __name__)
CORS(menu_bp) 

# Segmentos que vendem coisa PESADA. Neles o peso é obrigatório: sem ele o
# pedido calcula 0 kg, o adicional de carga nunca dispara e a trava de veículo
# (fail-open em peso zero) deixa 60 kg de ração irem de moto. Restaurante,
# padaria e farmácia ficam de fora porque comida não chega perto de 20 kg —
# exigir peso ali seria atrito sem ganho.
_SEGMENTOS_COM_PESO = {'pet', 'mercado', 'agropecuaria', 'agropecuária', 'bebidas'}


def _exige_peso(cur, restaurant_id):
    """A loja é de um segmento onde o peso importa?"""
    try:
        cur.execute("SELECT segment FROM restaurant_profiles WHERE id = %s", (str(restaurant_id),))
        row = cur.fetchone()
        seg = (row[0] if row and not isinstance(row, dict) else (row or {}).get('segment')) or ''
        return str(seg).strip().lower() in _SEGMENTOS_COM_PESO
    except Exception:
        # Não conseguiu saber o segmento: NÃO bloqueia o cadastro. Recusar item
        # por causa de uma consulta que falhou seria trocar um problema de
        # precificação por um de "não consigo cadastrar meu produto".
        logging.warning("Não deu pra ler o segmento da loja %s — peso não exigido", restaurant_id)
        return False


def _peso_kg(data):
    """Peso do produto em kg, tolerante e sem inventar valor.

    Existe porque o catálogo deixou de ser só comida: com pet shop, mercado e
    agropecuária, o peso decide se o pedido pode ir de moto ou precisa de
    carro. Ausente/vazio vira NULL (conta como 0 no somatório) — comida não
    tem por que preencher isso.

    Aceita vírgula ("1,5") porque é assim que se digita peso em português, e
    recusa negativo em silêncio virando NULL em vez de poluir a soma.
    """
    if data is None:
        return None
    bruto = data.get('peso_kg', data.get('peso'))
    if bruto in (None, ''):
        return None
    try:
        valor = float(str(bruto).replace(',', '.'))
    except (TypeError, ValueError):
        return None
    return valor if valor > 0 else None


def make_serializable(data):
    if isinstance(data, dict): return {k: make_serializable(v) for k, v in data.items()}
    if isinstance(data, list): return [make_serializable(item) for item in data]
    if isinstance(data, (datetime, date, time)): return data.isoformat()
    return data

def handle_db_errors(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        conn = None
        try:
            conn = get_db_connection()
            if not conn: return jsonify({"status": "error", "error": "Database connection failed"}), 500
            return f(conn, *args, **kwargs)
        except Exception as e:
            traceback.print_exc()
            return jsonify({"status": "error", "error": str(e)}), 500
        finally:
            if conn: conn.close()
    return wrapper

# ✅ FUNÇÃO CORRIGIDA
@menu_bp.route('/', methods=['GET'])
@handle_db_errors
def get_menu_items(conn):
    user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
    if error: return error
    if user_type != 'restaurant': return jsonify({"status": "error", "error": "Unauthorized"}), 403
    
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        # 1. Primeiro, buscar o ID do perfil do restaurante usando o user_id do token.
        cur.execute("SELECT id FROM restaurant_profiles WHERE user_id = %s", (user_id,))
        restaurant_profile = cur.fetchone()
        
        if not restaurant_profile:
            return jsonify({"status": "error", "error": "Restaurant profile not found for this user"}), 404
        
        restaurant_id = restaurant_profile['id']
        
        # 2. Agora, usar o restaurant_id para buscar os itens do cardápio.
        cur.execute(
            "SELECT id, name, description, price, category, is_available, image_url FROM menu_items WHERE restaurant_id = %s ORDER BY category, name", 
            (restaurant_id,)
        )
        items = [make_serializable(dict(row)) for row in cur.fetchall()]
        return jsonify({"status": "success", "data": items})

# ✅ FUNÇÃO CORRIGIDA (para usar restaurant_id)
@menu_bp.route('/', methods=['POST'])
@handle_db_errors
def add_menu_item(conn):
    user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
    if error: return error
    if user_type != 'restaurant': return jsonify({"status": "error", "error": "Unauthorized"}), 403
    
    data = request.get_json()
    required = ['name', 'price', 'category']
    if not all(field in data for field in required):
        return jsonify({"status": "error", "error": f"Missing required fields: {required}"}), 400
        
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # Buscar o ID do perfil do restaurante
            cur.execute("SELECT id FROM restaurant_profiles WHERE user_id = %s", (user_id,))
            restaurant_profile = cur.fetchone()
            if not restaurant_profile:
                return jsonify({"status": "error", "error": "Restaurant profile not found"}), 404
            restaurant_id = restaurant_profile['id']

            # Peso obrigatório onde ele decide o frete e o veículo. Bloquear na
            # ORIGEM é o que faz a regra valer: um aviso depois viraria uma
            # lista de pendências que ninguém limpa.
            peso = _peso_kg(data)
            if peso is None and _exige_peso(cur, restaurant_id):
                return jsonify({
                    "status": "error",
                    "error": "peso_obrigatorio",
                    "message": "Informe o peso em kg. É ele que define o frete e "
                               "se o pedido cabe numa moto ou precisa de carro.",
                }), 400

            # Inserir o item com o restaurant_id correto
            cur.execute(
                "INSERT INTO menu_items (user_id, restaurant_id, name, description, price, category, is_available, image_url, peso_kg) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
                (user_id, restaurant_id, data['name'], data.get('description', ''), float(data['price']), data['category'], data.get('is_available', True), data.get('image_url', None), peso)
            )
            new_item = make_serializable(dict(cur.fetchone()))
            conn.commit()
            return jsonify({"status": "success", "data": new_item}), 201
    except psycopg2.Error as e:
        conn.rollback()
        return jsonify({"status": "error", "error": "Database error"}), 500

# ✅ ROTA DE UPDATE ADICIONADA E CORRIGIDA
@menu_bp.route('/<uuid:item_id>', methods=['PUT'])
@handle_db_errors
def update_menu_item(conn, item_id):
    user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
    if error: return error
    if user_type != 'restaurant': return jsonify({"status": "error", "error": "Unauthorized"}), 403
    
    data = request.get_json()
    
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        # Verificar se o item pertence ao restaurante do usuário antes de atualizar
        cur.execute("SELECT rp.id FROM restaurant_profiles rp JOIN menu_items mi ON rp.id = mi.restaurant_id WHERE mi.id = %s AND rp.user_id = %s", (str(item_id), user_id))
        dono = cur.fetchone()
        if not dono:
            return jsonify({"status": "error", "error": "Item not found or you are not authorized to edit it"}), 404

        # Mesma exigência da criação. Sem isto, bastava criar o item antes de a
        # loja virar 'pet' — ou salvar de novo apagando o peso — pra escapar.
        peso = _peso_kg(data)
        if peso is None and _exige_peso(cur, dono['id']):
            return jsonify({
                "status": "error",
                "error": "peso_obrigatorio",
                "message": "Informe o peso em kg. É ele que define o frete e "
                           "se o pedido cabe numa moto ou precisa de carro.",
            }), 400

        # Atualizar o item
        cur.execute(
            """
            UPDATE menu_items
            SET name = %s, description = %s, price = %s, category = %s, is_available = %s, image_url = %s,
                peso_kg = %s
            WHERE id = %s
            RETURNING *
            """,
            (data['name'], data.get('description'), float(data['price']), data['category'], data.get('is_available', True), data.get('image_url'), peso, str(item_id))
        )
        updated_item = make_serializable(dict(cur.fetchone()))
        conn.commit()
        return jsonify({"status": "success", "data": updated_item})

# ✅ ROTA DE DELETE CORRIGIDA (usando UUID)
@menu_bp.route('/<uuid:item_id>', methods=['DELETE'])
@handle_db_errors
def delete_menu_item(conn, item_id):
    user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
    if error: return error
    if user_type != 'restaurant': return jsonify({"status": "error", "error": "Unauthorized"}), 403
    
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM menu_items WHERE id = %s AND user_id = %s RETURNING id", (str(item_id), user_id))
            deleted_id = cur.fetchone()
            conn.commit()
            
            if deleted_id:
                return jsonify({"status": "success", "message": f"Item com ID {item_id} excluído com sucesso."}), 200
            else:
                return jsonify({"status": "error", "message": "Item não encontrado ou não autorizado."}), 404
                
    except psycopg2.Error as e:
        conn.rollback()
        logging.error(f"Erro de banco de dados ao excluir item: {e}")
        return jsonify({"status": "error", "message": "Erro de banco de dados"}), 500

@menu_bp.route('/upload-image', methods=['POST'])
def upload_menu_item_image():
    user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
    if error: return error
    if user_type != 'restaurant': return jsonify({"status": "error", "error": "Unauthorized"}), 403
    if 'file' not in request.files: return jsonify({"status": "error", "error": "Nenhum ficheiro de imagem enviado com o campo 'file'."}), 400
    file = request.files['file']
    if file.filename == '': return jsonify({"status": "error", "error": "Nome de ficheiro vazio."}), 400
    try:
        file_ext = os.path.splitext(file.filename)[1]
        unique_filename = f"{user_id}-{uuid.uuid4()}{file_ext}"
        path_on_storage = f"public/{unique_filename}"
        supabase.storage.from_("menu-images").upload(path=path_on_storage, file=file.read(), file_options={"content-type": file.mimetype})
        public_url = supabase.storage.from_("menu-images").get_public_url(path_on_storage)
        return jsonify({"status": "success", "data": {"image_url": public_url}}), 200
    except Exception as e:
        traceback.print_exc()
        return jsonify({"status": "error", "error": str(e)}), 500


# ─────────────────────────────────────────────────────────────────────────────
# IMPORTAÇÃO DE CATÁLOGO POR PLANILHA
# Existe por causa de farmácia/mercado/pet: 3 mil a 8 mil itens não se cadastram
# um a um — o dono desiste no item 40 e o parceiro se perde. O app manda a
# planilha já parseada (JSON), então aqui só valida e grava.
# ─────────────────────────────────────────────────────────────────────────────

# Teto por chamada. O app fatia em lotes: evita request gigante, timeout do
# Render e transação longa segurando a tabela.
_MAX_POR_LOTE = 500


def _preco_para_float(valor):
    """Aceita '12,50', '12.50', 'R$ 12,50' e 1250 (centavos não é suportado
    de propósito — ambíguo demais). Devolve None se não der pra entender."""
    if valor is None:
        return None
    if isinstance(valor, (int, float)):
        return float(valor)
    t = str(valor).strip().replace("R$", "").replace(" ", "")
    if not t:
        return None
    # "1.234,56" (BR) vs "1234.56" (US): se tem vírgula, ela é o decimal.
    if "," in t:
        t = t.replace(".", "").replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return None


@menu_bp.route('/import', methods=['POST'])
@handle_db_errors
def importar_catalogo(conn):
    """POST /api/menu/import
    Body: { "items": [ {name, price, category?, ean?, stock?, description?} ], "dry_run": bool }

    Reconciliação por EAN quando existir; senão por NOME (dentro da loja).
    Sem isso, reimportar a mesma planilha duplicaria o catálogo inteiro.
    """
    user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
    if error:
        return error
    if user_type != 'restaurant':
        return jsonify({"status": "error", "error": "Apenas parceiros podem importar catálogo"}), 403

    corpo = request.get_json(silent=True) or {}
    itens = corpo.get('items') or []
    dry_run = bool(corpo.get('dry_run'))

    if not isinstance(itens, list) or not itens:
        return jsonify({"status": "error", "error": "Nenhum item recebido"}), 400
    if len(itens) > _MAX_POR_LOTE:
        return jsonify({"status": "error",
                        "error": f"Máximo {_MAX_POR_LOTE} itens por lote"}), 400

    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT id FROM restaurant_profiles WHERE user_id = %s", (user_id,))
        perfil = cur.fetchone()
        if not perfil:
            return jsonify({"status": "error", "error": "Perfil de parceiro não encontrado"}), 404
        restaurant_id = perfil['id']

        criados = atualizados = 0
        ignorados = []

        for i, it in enumerate(itens):
            nome = str(it.get('name') or '').strip()
            preco = _preco_para_float(it.get('price'))
            if not nome:
                ignorados.append({"linha": i + 1, "motivo": "sem nome"})
                continue
            if preco is None or preco < 0:
                ignorados.append({"linha": i + 1, "nome": nome[:40],
                                  "motivo": f"preço inválido ({it.get('price')!r})"})
                continue

            ean = (str(it.get('ean') or '').strip() or None)
            categoria = (str(it.get('category') or '').strip() or 'Geral')
            descricao = (str(it.get('description') or '').strip() or '')
            try:
                estoque = int(it['stock']) if str(it.get('stock', '')).strip() != '' else None
            except (TypeError, ValueError):
                estoque = None
            # Estoque zerado NÃO some do cardápio: fica visível e indisponível,
            # senão o cliente nunca sabe que a loja trabalha com o produto.
            disponivel = True if estoque is None else estoque > 0

            if dry_run:
                continue

            existente = None
            if ean:
                cur.execute("SELECT id FROM menu_items WHERE restaurant_id = %s AND ean = %s",
                            (restaurant_id, ean))
                existente = cur.fetchone()
            if not existente:
                cur.execute("""SELECT id FROM menu_items
                                WHERE restaurant_id = %s AND LOWER(TRIM(name)) = LOWER(%s)
                                LIMIT 1""", (restaurant_id, nome))
                existente = cur.fetchone()

            if existente:
                # Não sobrescreve a descrição nem a imagem com vazio: o parceiro
                # pode ter caprichado nelas no app, e a planilha do ERP não tem.
                cur.execute("""
                    UPDATE menu_items
                       SET name = %s, price = %s, category = %s,
                           description = CASE WHEN %s <> '' THEN %s ELSE description END,
                           ean = COALESCE(%s, ean), stock = %s,
                           is_available = %s, updated_at = NOW()
                     WHERE id = %s
                """, (nome, preco, categoria, descricao, descricao, ean, estoque,
                      disponivel, existente['id']))
                atualizados += 1
            else:
                cur.execute("""
                    INSERT INTO menu_items
                        (user_id, restaurant_id, name, description, price, category,
                         is_available, ean, stock)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (user_id, restaurant_id, nome, descricao, preco, categoria,
                      disponivel, ean, estoque))
                criados += 1

        if not dry_run:
            conn.commit()

    return jsonify({
        "status": "success",
        "dry_run": dry_run,
        "recebidos": len(itens),
        "criados": criados,
        "atualizados": atualizados,
        "ignorados": ignorados[:50],
        "total_ignorados": len(ignorados),
    }), 200


# ---------------------------------------------------------------------------
# Opções do item: "escolha o corte", "escolha o molho", "adicionais"
# ---------------------------------------------------------------------------

def _dono_do_item(cur, item_id, user_id):
    """O item é mesmo desta loja? Sem isto, um parceiro editaria o cardápio do
    outro só trocando o id na chamada."""
    cur.execute("""SELECT 1 FROM menu_items mi
                     JOIN restaurant_profiles rp ON rp.id = mi.restaurant_id
                    WHERE mi.id = %s AND rp.user_id = %s""", (str(item_id), str(user_id)))
    return cur.fetchone() is not None


@menu_bp.route('/items/<uuid:item_id>/opcoes', methods=['GET', 'OPTIONS'])
@handle_db_errors
def listar_opcoes_do_item(conn, item_id):
    """Grupos e opções de um item. PÚBLICO, sem token.

    É cardápio: nome e preço de opção, a mesma natureza do que já aparece na
    vitrine pra quem nem tem conta. Exigir token aqui só quebraria a vitrine
    sem esconder nada.

    Quem ESCREVE é outra história — o POST abaixo exige o parceiro dono do
    item. Ler o cardápio dos outros é vitrine; mexer nele é invasão.
    """
    if request.method == 'OPTIONS':
        return jsonify({}), 204
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("""
            SELECT g.id, g.nome, g.min_escolhas, g.max_escolhas, g.ordem
              FROM menu_item_option_groups g
             WHERE g.item_id = %s
             ORDER BY g.ordem, g.created_at
        """, (str(item_id),))
        grupos = [dict(r) for r in cur.fetchall()]
        if grupos:
            cur.execute("""
                SELECT id, group_id, nome, preco_extra, disponivel, ordem
                  FROM menu_item_options
                 WHERE group_id = ANY(%s::uuid[])
                 ORDER BY ordem, created_at
            """, ([g['id'] for g in grupos],))
            por_grupo = {}
            for r in cur.fetchall():
                d = dict(r)
                d['preco_extra'] = float(d['preco_extra'] or 0)
                por_grupo.setdefault(str(d['group_id']), []).append(d)
            for g in grupos:
                g['id'] = str(g['id'])
                g['opcoes'] = por_grupo.get(g['id'], [])
    return jsonify({"status": "success", "grupos": grupos}), 200


@menu_bp.route('/items/<uuid:item_id>/opcoes', methods=['POST'])
@handle_db_errors
def salvar_opcoes_do_item(conn, item_id):
    """Substitui os grupos/opções do item de uma vez.

    SUBSTITUI em vez de editar peça por peça, e isso é escolha: a tela do
    parceiro monta a lista inteira e manda. Com edição incremental, cada
    campo viraria uma chamada e a tela precisaria orquestrar ordem de
    criação/remoção — mais código, mais chance de o cardápio ficar meio
    salvo. Aqui ou salva tudo, ou não muda nada.

    Body: {"grupos": [{nome, min_escolhas, max_escolhas,
                       opcoes:[{nome, preco_extra, disponivel}]}]}
    """
    user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
    if error:
        return error
    if user_type != 'restaurant':
        return jsonify({"error": "Apenas o parceiro edita o próprio cardápio."}), 403

    grupos = (request.get_json(silent=True) or {}).get('grupos', [])
    if not isinstance(grupos, list):
        return jsonify({"error": "Formato inválido."}), 400
    if len(grupos) > 12:
        return jsonify({"error": "Máximo de 12 grupos por item."}), 400

    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        if not _dono_do_item(cur, item_id, user_id):
            return jsonify({"error": "Item não encontrado."}), 404

        # ON DELETE CASCADE limpa as opções junto.
        cur.execute("DELETE FROM menu_item_option_groups WHERE item_id = %s", (str(item_id),))

        for i, g in enumerate(grupos):
            nome = (g.get('nome') or '').strip()[:80]
            if not nome:
                continue
            opcoes = [o for o in (g.get('opcoes') or []) if (o.get('nome') or '').strip()]
            if not opcoes:
                # Grupo sem opção é armadilha: apareceria pro cliente como uma
                # escolha obrigatória impossível de satisfazer.
                continue
            if len(opcoes) > 30:
                return jsonify({"error": f"Máximo de 30 opções em '{nome}'."}), 400

            maxe = max(1, min(int(g.get('max_escolhas') or 1), len(opcoes)))
            mine = max(0, min(int(g.get('min_escolhas') or 0), maxe))

            cur.execute("""
                INSERT INTO menu_item_option_groups (item_id, nome, min_escolhas, max_escolhas, ordem)
                VALUES (%s, %s, %s, %s, %s) RETURNING id
            """, (str(item_id), nome, mine, maxe, i))
            gid = cur.fetchone()['id']

            for j, o in enumerate(opcoes):
                try:
                    preco = max(0.0, round(float(o.get('preco_extra') or 0), 2))
                except (TypeError, ValueError):
                    preco = 0.0
                cur.execute("""
                    INSERT INTO menu_item_options (group_id, nome, preco_extra, disponivel, ordem)
                    VALUES (%s, %s, %s, %s, %s)
                """, (gid, (o.get('nome') or '').strip()[:80], preco,
                      bool(o.get('disponivel', True)), j))

    conn.commit()
    return jsonify({"status": "success"}), 200
