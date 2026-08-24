# src/routes/client.py - VERSÃO COM UPLOAD DE AVATAR

import logging
from flask import Blueprint, jsonify, request
import psycopg2.extras
from ..utils.helpers import get_db_connection, get_user_id_from_token, supabase
from functools import wraps
import os
import uuid

client_bp = Blueprint('client_bp', __name__)
logging.basicConfig(level=logging.INFO)


@client_bp.route('/heartbeat', methods=['POST', 'OPTIONS'])
def client_heartbeat():
    """Sinal de vida do app do cliente + FOTO do carrinho dele.

    O carrinho vive no localStorage do aparelho, então até agora o servidor
    nunca soube que alguém montou um pedido e desistiu. É o pior tipo de falha:
    a que o cliente NÃO reclama — ele fecha o app e some.

    Foi exatamente o que aconteceu com o bug do frete (coordenada 0 → "não foi
    possível calcular"): pro Diego só apareceu porque ele testou na mão. Pro
    cliente era invisível.

    Com a foto do carrinho, carrinho parado sem pedido novo vira alarme.
    Best-effort: falhar aqui NUNCA pode atrapalhar o app do cliente.
    """
    if request.method == 'OPTIONS':
        return jsonify({}), 204

    user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
    if error:
        return error
    if user_type != 'client':
        return jsonify({"error": "Apenas clientes"}), 403

    body = request.get_json(silent=True) or {}
    try:
        itens = max(0, min(int(body.get('cart_items') or 0), 999))
    except (TypeError, ValueError):
        itens = 0
    try:
        valor = round(float(body.get('cart_value') or 0), 2)
        if valor < 0 or valor > 1_000_000:
            valor = 0.0
    except (TypeError, ValueError):
        valor = 0.0

    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "ok"}), 200  # nunca atrapalha o app
    try:
        with conn.cursor() as cur:
            # cart_updated_at só avança quando o carrinho MUDA — assim o "parado
            # há X min" mede o abandono de verdade, não o último ping.
            cur.execute("""
                UPDATE client_profiles
                   SET last_seen = NOW(),
                       cart_updated_at = CASE
                           WHEN COALESCE(cart_items_count, 0) IS DISTINCT FROM %s
                             OR COALESCE(cart_value, 0) IS DISTINCT FROM %s
                           THEN NOW() ELSE cart_updated_at END,
                       cart_items_count = %s,
                       cart_value = %s
                 WHERE user_id = %s
            """, (itens, valor, itens, valor, str(user_id)))
            conn.commit()
    except Exception:
        logging.warning("heartbeat do cliente falhou", exc_info=True)
        try: conn.rollback()
        except Exception: pass
    finally:
        conn.close()
    return jsonify({"status": "ok"}), 200


def _geocode_client_address(street, neighborhood, city, state):
    """Geocodifica um endereço (Nominatim) com fallback rua+bairro -> bairro+cidade
    -> cidade. Retorna (lat, lng) ou (None, None). Best-effort — nunca lança."""
    import requests
    if not city or not state:
        return None, None
    variants = [
        [street, neighborhood, city, state],
        [neighborhood, city, state],
        [city, state],
    ]
    seen = set()
    for parts in variants:
        query = ", ".join([str(p).strip() for p in parts if p] + ["Brasil"])
        if query in seen:
            continue
        seen.add(query)
        try:
            r = requests.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": query, "format": "json", "limit": 1, "countrycodes": "br"},
                headers={"User-Agent": "InksaDelivery/1.0 (suporte@inksadelivery.com.br)",
                         "Accept-Language": "pt-BR"},
                timeout=8,
            )
            arr = r.json()
            if isinstance(arr, list) and arr:
                return float(arr[0]["lat"]), float(arr[0]["lon"])
        except Exception:
            continue
    return None, None

def handle_db_errors(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        conn = None
        try:
            conn = get_db_connection()
            if not conn:
                return jsonify({"status": "error", "error": "Database connection failed"}), 500
            return f(conn, *args, **kwargs)
        except Exception as e:
            logging.error(f"Client Route DB Error: {e}", exc_info=True)
            return jsonify({"status": "error", "error": str(e)}), 500
        finally:
            if conn:
                conn.close()
    return wrapper

@client_bp.route('/profile', methods=['GET', 'PUT'])
@handle_db_errors
def handle_client_profile(conn):
    user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
    if error: return error
    if user_type != 'client': return jsonify({"status": "error", "error": "Unauthorized access"}), 403

    if request.method == 'GET':
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT * FROM client_profiles WHERE user_id = %s LIMIT 1", (user_id,))
            profile = cur.fetchone()
            if not profile:
                # Auto-cria o perfil na primeira requisição autenticada
                try:
                    name_meta, phone_meta = '', ''
                    if supabase:
                        auth_header = request.headers.get('Authorization')
                        token = auth_header.split()[-1] if auth_header else None
                        if token:
                            try:
                                ur = supabase.auth.get_user(token)
                                if ur and ur.user:
                                    m = ur.user.user_metadata or {}
                                    name_meta = m.get('name', '')
                                    phone_meta = m.get('phone', '')
                            except Exception:
                                pass
                    name_parts = (name_meta or '').split(' ', 1)
                    first_name = name_parts[0] or ''
                    last_name = name_parts[1] if len(name_parts) > 1 else ''
                    cur.execute(
                        """INSERT INTO client_profiles (user_id, first_name, last_name, phone)
                           VALUES (%s, %s, %s, %s) RETURNING *""",
                        (user_id, first_name, last_name, phone_meta or None)
                    )
                    profile = cur.fetchone()
                    conn.commit()
                    logging.info(f"Perfil de cliente auto-criado para user_id={user_id}")
                except Exception as create_err:
                    logging.error(f"Erro ao auto-criar perfil de cliente: {create_err}")
                    return jsonify({"status": "error", "error": "Client profile not found"}), 404
            if not profile:
                return jsonify({"status": "error", "error": "Client profile not found"}), 404
            return jsonify({"status": "success", "data": dict(profile)})

    if request.method == 'PUT':
        data = request.get_json()
        if not data:
            return jsonify({"status": "error", "error": "No data provided"}), 400

        allowed_fields = [
            'first_name', 'last_name', 'phone', 'cpf', 'birth_date',
            'avatar_url',
            'address_street', 'address_number', 'address_complement',
            'address_neighborhood', 'address_city', 'address_state', 'address_zipcode'
        ]
        updates = {k: v for k, v in data.items() if k in allowed_fields}
        if not updates:
            return jsonify({"status": "error", "error": "No valid fields to update"}), 400

        # Converte strings vazias em None (evita erro de cast em campos date/etc.)
        # Mantém first_name/last_name como estão (NOT NULL na tabela)
        for k in list(updates.keys()):
            if updates[k] == '' and k not in ('first_name', 'last_name'):
                updates[k] = None
        # Remove first_name/last_name se vazios para não violar NOT NULL
        for k in ('first_name', 'last_name'):
            if k in updates and not updates[k]:
                del updates[k]

        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # Garante que a linha existe antes de atualizar
            cur.execute("SELECT id FROM client_profiles WHERE user_id = %s LIMIT 1", (user_id,))
            if not cur.fetchone():
                cur.execute(
                    """INSERT INTO client_profiles (user_id, first_name, last_name)
                       VALUES (%s, %s, %s)""",
                    (user_id, updates.get('first_name', ''), updates.get('last_name', ''))
                )

            set_clause = ", ".join([f"{k} = %s" for k in updates.keys()])
            values = list(updates.values()) + [user_id]
            cur.execute(
                f"UPDATE client_profiles SET {set_clause} WHERE user_id = %s RETURNING *",
                values
            )
            updated = cur.fetchone()
            conn.commit()
            if not updated:
                return jsonify({"status": "error", "error": "Client profile not found"}), 404

            # Se o cliente ainda não tem endereço na agenda, cria um padrão a
            # partir do endereço do perfil (geocodificado). Assim o endereço do
            # perfil JÁ vira endereço de entrega, sem precisar recadastrar — antes
            # o checkout ficava sem coords porque a agenda estava vazia.
            try:
                prof = dict(updated)
                street = (prof.get('address_street') or '').strip()
                city = (prof.get('address_city') or '').strip()
                state = (prof.get('address_state') or '').strip()
                if street and city and state:
                    cur.execute("SELECT COUNT(*) AS c FROM client_addresses WHERE user_id = %s", (user_id,))
                    if cur.fetchone()['c'] == 0:
                        lat, lng = _geocode_client_address(
                            street, prof.get('address_neighborhood'), city, state)
                        cur.execute(
                            """INSERT INTO client_addresses
                                 (user_id, label, street, number, complement, neighborhood,
                                  city, state, zipcode, latitude, longitude, is_default)
                               VALUES (%s, 'Casa', %s, %s, %s, %s, %s, %s, %s, %s, %s, true)""",
                            (user_id, street, prof.get('address_number'), prof.get('address_complement'),
                             prof.get('address_neighborhood'), city, state, prof.get('address_zipcode'), lat, lng))
                        conn.commit()
                        logging.info(f"Endereço padrão criado do perfil p/ user_id={user_id} (coords={lat},{lng})")
            except Exception as _addr_err:
                try:
                    conn.rollback()
                except Exception:
                    pass
                logging.warning(f"Falha ao sincronizar endereço do perfil para a agenda: {_addr_err}")

            return jsonify({"status": "success", "data": dict(updated)})


# ✅ ROTA ADICIONADA: Rota para upload de avatar do cliente
@client_bp.route('/profile/upload-avatar', methods=['POST'])
@handle_db_errors
def upload_avatar(conn):
    user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
    if error: return error
    if user_type != 'client': return jsonify({"status": "error", "error": "Unauthorized"}), 403

    if 'file' not in request.files:
        return jsonify({"status": "error", "error": "Nenhum arquivo enviado com o campo 'file'."}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({"status": "error", "error": "Nome de arquivo vazio."}), 400

    try:
        file_ext = os.path.splitext(file.filename)[1]
        # Cria um nome de arquivo único para evitar conflitos
        unique_filename = f"avatar_{user_id}_{uuid.uuid4()}{file_ext}"
        
        # Faz o upload para o bucket 'avatars' no Supabase Storage
        supabase.storage.from_("avatars").upload(
            path=unique_filename,
            file=file.read(),
            file_options={"content-type": file.mimetype, "upsert": "true"}
        )
        
        # Obtém a URL pública do arquivo que acabamos de enviar
        public_url = supabase.storage.from_("avatars").get_public_url(unique_filename)
        
        # Atualiza a coluna 'avatar_url' na tabela 'client_profiles'
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE client_profiles SET avatar_url = %s WHERE user_id = %s",
                (public_url, user_id)
            )
            conn.commit()

        return jsonify({"status": "success", "data": {"avatar_url": public_url}}), 200

    except Exception as e:
        logging.error(f"Avatar Upload Error: {e}", exc_info=True)
        return jsonify({"status": "error", "error": str(e)}), 500


# ─── Endereços do cliente (múltiplos) ────────────────────────────────────────
ADDRESS_FIELDS = [
    'label', 'street', 'number', 'complement', 'neighborhood',
    'city', 'state', 'zipcode', 'reference', 'latitude', 'longitude',
]


def _auth_client():
    """Retorna (user_id, None) se for um cliente válido, ou (None, error_response)."""
    user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
    if error:
        return None, error
    if user_type != 'client':
        return None, (jsonify({"status": "error", "error": "Unauthorized access"}), 403)
    return user_id, None


@client_bp.route('/addresses', methods=['GET'])
@handle_db_errors
def list_addresses(conn):
    user_id, err = _auth_client()
    if err:
        return err
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(
            "SELECT * FROM client_addresses WHERE user_id = %s ORDER BY is_default DESC, created_at DESC",
            (user_id,),
        )
        rows = [dict(r) for r in cur.fetchall()]
    return jsonify({"status": "success", "data": rows}), 200


@client_bp.route('/addresses', methods=['POST'])
@handle_db_errors
def create_address(conn):
    user_id, err = _auth_client()
    if err:
        return err
    data = request.get_json() or {}
    payload = {k: data.get(k) for k in ADDRESS_FIELDS if k in data}
    payload.setdefault('label', 'Endereço')

    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        # Primeiro endereço do cliente vira o padrão automaticamente
        cur.execute("SELECT COUNT(*) AS c FROM client_addresses WHERE user_id = %s", (user_id,))
        is_first = cur.fetchone()['c'] == 0
        make_default = bool(data.get('is_default')) or is_first
        if make_default:
            cur.execute("UPDATE client_addresses SET is_default = false WHERE user_id = %s", (user_id,))

        cols = ['user_id'] + list(payload.keys()) + ['is_default']
        vals = [user_id] + list(payload.values()) + [make_default]
        placeholders = ', '.join(['%s'] * len(cols))
        cur.execute(
            f"INSERT INTO client_addresses ({', '.join(cols)}) VALUES ({placeholders}) RETURNING *",
            vals,
        )
        row = dict(cur.fetchone())
        conn.commit()
    return jsonify({"status": "success", "data": row}), 201


@client_bp.route('/addresses/<uuid:address_id>', methods=['PUT'])
@handle_db_errors
def update_address(conn, address_id):
    user_id, err = _auth_client()
    if err:
        return err
    data = request.get_json() or {}
    updates = {k: data.get(k) for k in ADDRESS_FIELDS if k in data}
    if not updates:
        return jsonify({"status": "error", "error": "No valid fields to update"}), 400

    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        set_clause = ", ".join([f"{k} = %s" for k in updates.keys()])
        values = list(updates.values()) + [str(address_id), user_id]
        cur.execute(
            f"UPDATE client_addresses SET {set_clause} WHERE id = %s AND user_id = %s RETURNING *",
            values,
        )
        row = cur.fetchone()
        conn.commit()
        if not row:
            return jsonify({"status": "error", "error": "Endereço não encontrado"}), 404
    return jsonify({"status": "success", "data": dict(row)}), 200


@client_bp.route('/addresses/<uuid:address_id>', methods=['DELETE'])
@handle_db_errors
def delete_address(conn, address_id):
    user_id, err = _auth_client()
    if err:
        return err
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(
            "DELETE FROM client_addresses WHERE id = %s AND user_id = %s RETURNING is_default",
            (str(address_id), user_id),
        )
        deleted = cur.fetchone()
        if not deleted:
            return jsonify({"status": "error", "error": "Endereço não encontrado"}), 404
        # Se removeu o padrão, promove o endereço mais recente a padrão
        if deleted['is_default']:
            cur.execute(
                """UPDATE client_addresses SET is_default = true
                   WHERE id = (SELECT id FROM client_addresses WHERE user_id = %s
                               ORDER BY created_at DESC LIMIT 1)""",
                (user_id,),
            )
        conn.commit()
    return jsonify({"status": "success"}), 200


@client_bp.route('/addresses/<uuid:address_id>/default', methods=['POST'])
@handle_db_errors
def set_default_address(conn, address_id):
    user_id, err = _auth_client()
    if err:
        return err
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT id FROM client_addresses WHERE id = %s AND user_id = %s", (str(address_id), user_id))
        if not cur.fetchone():
            return jsonify({"status": "error", "error": "Endereço não encontrado"}), 404
        cur.execute("UPDATE client_addresses SET is_default = false WHERE user_id = %s", (user_id,))
        cur.execute("UPDATE client_addresses SET is_default = true WHERE id = %s AND user_id = %s", (str(address_id), user_id))
        conn.commit()
    return jsonify({"status": "success"}), 200


# ---------------------------------------------------------------------------
# Sugestão de restaurante
# ---------------------------------------------------------------------------
def _chave_do_nome(nome: str) -> str:
    """Agrupa 'Pizzaria do Zé', 'pizzaria do ze' e 'Pizzaria  do  Ze' no mesmo balde.

    Sem isto o contador nunca sobe — e o contador é a ÚNICA coisa que faz esta
    tabela valer algo. "7 pessoas pediram a Pizzaria X" é argumento de venda;
    sete linhas de uma pessoa cada não é nada.
    """
    import re
    import unicodedata
    s = unicodedata.normalize('NFKD', nome or '')
    s = ''.join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r'[^a-z0-9 ]', ' ', s.lower())
    return re.sub(r'\s+', ' ', s).strip()


@client_bp.route('/suggestions', methods=['POST', 'OPTIONS'])
def sugerir_restaurante():
    """O cliente diz qual loja ele queria encontrar aqui.

    Nasce de um problema concreto: o app mostra pouquíssimas lojas, e quem abre
    e não acha nada desinstala sem dizer nada. Este endpoint transforma essa
    saída silenciosa em duas coisas úteis — a pessoa se sente ouvida, e o Diego
    ganha uma fila de prospecção ordenada por demanda real.

    NÃO promete prazo. "Vamos atrás" é verdade; "em 7 dias" não seria.
    """
    if request.method == 'OPTIONS':
        return jsonify({}), 204

    user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
    if error:
        return error
    if user_type != 'client':
        return jsonify({"error": "Apenas clientes podem sugerir."}), 403

    body = request.get_json(silent=True) or {}
    nome = (body.get('nome') or '').strip()[:120]
    contato = (body.get('contato') or '').strip()[:120] or None
    cidade = (body.get('cidade') or '').strip()[:80] or None

    if len(nome) < 3:
        return jsonify({"error": "Escreva o nome do restaurante."}), 400
    chave = _chave_do_nome(nome)
    if not chave:
        return jsonify({"error": "Escreva o nome do restaurante."}), 400

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Serviço indisponível no momento."}), 503
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT id FROM client_profiles WHERE user_id = %s LIMIT 1", (user_id,))
            perfil = cur.fetchone()
            if not perfil:
                return jsonify({"error": "Perfil não encontrado."}), 404

            # ON CONFLICT: sugerir a mesma loja duas vezes não vira dois votos.
            #
            # `xmax = 0` distingue linha NOVA de linha que já existia — é o
            # jeito do Postgres de dizer se o INSERT virou UPDATE. Sem isso a
            # resposta era idêntica nos dois casos, e o app dizia "anotado,
            # você foi a primeira pessoa" pra quem já tinha pedido antes.
            # Parecia que o pedido não tinha contado; na verdade tinha contado
            # da primeira vez. Confundiu na primeira semana de uso.
            cur.execute("""
                INSERT INTO restaurant_suggestions (client_id, nome, nome_chave, contato, cidade)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (client_id, nome_chave) WHERE client_id IS NOT NULL
                DO UPDATE SET contato = COALESCE(EXCLUDED.contato, restaurant_suggestions.contato)
                RETURNING id, (xmax = 0) AS nova
            """, (perfil['id'], nome, chave, contato, cidade))
            nova = bool((cur.fetchone() or {}).get('nova'))

            # Quantas pessoas já pediram esta mesma loja — devolve pro app, que
            # usa pra dizer "você e mais 6". Prova social de graça, e verdadeira.
            cur.execute("SELECT count(*)::int AS n FROM restaurant_suggestions WHERE nome_chave = %s", (chave,))
            n = int((cur.fetchone() or {}).get('n') or 1)
            conn.commit()

        return jsonify({"status": "ok", "pedidos": n, "ja_tinha": not nova}), 201
    except Exception:
        logging.warning("Sugestão de restaurante falhou", exc_info=True)
        try: conn.rollback()
        except Exception: pass
        return jsonify({"error": "Não consegui registrar agora. Tente de novo."}), 500
    finally:
        try: conn.close()
        except Exception: pass


@client_bp.route('/suggestions', methods=['GET'])
def listar_sugestoes_publicas():
    """O que outras pessoas já pediram, pro cliente escolher em vez de digitar.

    Nasce de um problema real do primeiro dia de uso: a mesma padaria entrou
    duas vezes, como "Padaria muller" e "Pqdaria mullre". A normalização junta
    maiúscula, acento e espaço a mais — não conserta letra trocada. E adivinhar
    por semelhança é pior: no dia em que o palpite errar, junta duas lojas
    diferentes num registro só e o número que o Diego leva pro dono da loja
    passa a estar errado.

    A saída é não adivinhar. Mostra o que já existe e deixa a PESSOA escolher.
    Quem toca numa opção manda exatamente o mesmo nome que já está lá, e o
    contador sobe em vez de nascer uma linha nova.

    De quebra vira prova social: ver "Padaria Muller — 6 pessoas já pediram"
    convence mais a mandar a sua do que um campo vazio.
    """
    user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
    if error:
        return error
    if user_type != 'client':
        return jsonify({"error": "Apenas clientes."}), 403

    conn = get_db_connection()
    if not conn:
        # Falha aqui não pode travar a caixa de sugestão: sem a lista o app
        # mostra só o campo de texto, que é o comportamento antigo.
        return jsonify({"sugestoes": []}), 200
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                SELECT MIN(nome)   AS nome,
                       nome_chave,
                       count(*)::int AS pedidos
                  FROM restaurant_suggestions
                 GROUP BY nome_chave
                 ORDER BY count(*) DESC, max(created_at) DESC
                 LIMIT 200
            """)
            linhas = [dict(r) for r in cur.fetchall()]
        return jsonify({"sugestoes": linhas}), 200
    except Exception:
        logging.warning("Listagem de sugestões falhou", exc_info=True)
        return jsonify({"sugestoes": []}), 200
    finally:
        try:
            conn.close()
        except Exception:
            pass
