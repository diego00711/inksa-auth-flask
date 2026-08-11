# src/routes/restaurant.py - VERSÃO CORRIGIDA COM CARDÁPIO

from flask import request, jsonify
from ..utils.helpers import get_db_connection, get_user_id_from_token
import os
import traceback
from flask import Blueprint
import psycopg2
import psycopg2.extras
from ..utils.helpers import supabase
from functools import wraps
import uuid
from datetime import datetime, date, time

restaurant_bp = Blueprint('restaurant_bp', __name__)


def _geocode_address(street, number, neighborhood, city, state):
    """Geocodifica o endereço via Nominatim (best-effort, timeout curto).

    Fallback server-side para quando o geocode do frontend falha — sem coordenadas
    o restaurante não consegue abrir (gate) nem ter frete calculado.
    """
    try:
        import requests as _rq
        q = ", ".join([p for p in [
            f"{street}, {number}" if street and number else street,
            neighborhood, city, state, "Brasil",
        ] if p])
        resp = _rq.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": q, "format": "json", "limit": 1, "countrycodes": "br"},
            headers={"User-Agent": "InksaDelivery/1.0 (suporte@inksadelivery.com.br)"},
            timeout=4,
        )
        arr = resp.json()
        if isinstance(arr, list) and arr:
            lat, lon = float(arr[0]["lat"]), float(arr[0]["lon"])
            return lat, lon
    except Exception:
        pass
    return None, None

def make_serializable(data):
    """Converte dados para JSON serializável"""
    if isinstance(data, dict): 
        return {k: make_serializable(v) for k, v in data.items()}
    if isinstance(data, list): 
        return [make_serializable(item) for item in data]
    if isinstance(data, (datetime, date, time)): 
        return data.isoformat()
    return data

def handle_db_errors(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        conn = None
        try:
            conn = get_db_connection()
            if not conn:
                return jsonify({"status": "error", "error": "Database connection failed"}), 500
            return f(conn, *args, **kwargs)
        except psycopg2.Error as e:
            traceback.print_exc()
            return jsonify({"status": "error", "error": "Database operation failed"}), 500
        except Exception as e:
            traceback.print_exc()
            return jsonify({"status": "error", "error": str(e)}), 500
        finally:
            if conn:
                conn.close()
    return wrapper

@restaurant_bp.route('/', methods=['GET'])
@handle_db_errors
def get_all_restaurants_public(conn):
    user_lat = request.args.get('user_lat', type=float)
    user_lon = request.args.get('user_lon', type=float)
    
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        if user_lat and user_lon:
            cur.execute("""
                SELECT id, restaurant_name, logo_url, category, rating, delivery_time, 
                delivery_fee, minimum_order, is_open, delivery_type,
                ROUND((earth_distance(ll_to_earth(latitude, longitude), ll_to_earth(%s, %s)) / 1000)::numeric, 2) AS distance_km
                FROM restaurant_profiles
                WHERE latitude IS NOT NULL AND longitude IS NOT NULL
                ORDER BY distance_km
            """, (user_lat, user_lon))
        else:
            cur.execute("""
                SELECT id, restaurant_name, logo_url, category, rating, delivery_time,
                delivery_fee, minimum_order, is_open, delivery_type
                FROM restaurant_profiles
            """)
        restaurants = [dict(row) for row in cur.fetchall()]
        return jsonify({"status": "success", "data": restaurants})

# ✅ ROTA DE DETALHES CORRIGIDA - AGORA INCLUI O CARDÁPIO
@restaurant_bp.route('/<uuid:restaurant_id>', methods=['GET'])
@handle_db_errors
def get_restaurant_details(conn, restaurant_id):
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        # 1. Buscar dados básicos do restaurante
        cur.execute("SELECT * FROM restaurant_profiles WHERE id = %s", (str(restaurant_id),))
        restaurant = cur.fetchone()
        
        if not restaurant:
            return jsonify({"status": "error", "error": "Restaurant not found"}), 404
        
        # 2. Buscar itens do cardápio
        cur.execute("""
            SELECT id, name, description, price, category, is_available, image_url, created_at
            FROM menu_items 
            WHERE restaurant_id = %s 
            ORDER BY category, name
        """, (str(restaurant_id),))
        
        menu_items = [make_serializable(dict(row)) for row in cur.fetchall()]
        
        # 3. Combinar dados do restaurante com o cardápio
        restaurant_data = make_serializable(dict(restaurant))
        restaurant_data['menu_items'] = menu_items
        
        return jsonify({
            "status": "success",
            "data": restaurant_data
        })

@restaurant_bp.route('/profile', methods=['GET', 'PUT'])
def handle_profile():
    user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
    if error:
        return error
    if user_type != 'restaurant':
        return jsonify({"status": "error", "error": "Unauthorized"}), 403

    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({"status": "error", "error": "Database connection failed"}), 500

        if request.method == 'GET':
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute("SELECT * FROM restaurant_profiles WHERE user_id = %s", (user_id,))
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
                        cur.execute(
                            """INSERT INTO restaurant_profiles (id, user_id, restaurant_name, phone, is_open)
                               VALUES (%s, %s, %s, %s, FALSE) RETURNING *""",
                            (user_id, user_id, name_meta or 'Meu Restaurante', phone_meta or None)
                        )
                        profile = cur.fetchone()
                        conn.commit()
                    except Exception as create_err:
                        traceback.print_exc()
                        return jsonify({"status": "error", "error": "Profile not found"}), 404
                if not profile:
                    return jsonify({"status": "error", "error": "Profile not found"}), 404
                return jsonify({"status": "success", "data": dict(profile)})

        elif request.method == 'PUT':
            data = request.get_json()
            if not data:
                return jsonify({"status": "error", "error": "No data provided"}), 400

            allowed_fields = [
                'restaurant_name', 'business_name', 'cnpj', 'phone', 'logo_url', 'address_street',
                'address_number', 'address_complement', 'address_neighborhood', 'address_city',
                'address_state', 'address_zipcode', 'latitude', 'longitude', 'category',
                'segment',
                'delivery_time', 'cuisine_type', 'description', 'is_open', 'delivery_fee',
                'minimum_order', 'payout_frequency', 'bank_name', 'bank_agency',
                'bank_account_number', 'bank_account_type', 'pix_key', 'pix_key_type',
                'mp_account_id', 'delivery_type', 'own_delivery_radius_km',
                'max_order_items',
                # accepts_cash faltava aqui: o app mandava, o filtro descartava
                # sem avisar, a tela dizia "salvo" e o cliente continuava vendo
                # a opção de dinheiro. Whitelist que engole campo em silêncio é
                # pior que erro — ninguém tem como perceber.
                'accepts_cash',
                'opening_hours', 'hours_auto'
            ]
            updates = {k: v for k, v in data.items() if k in allowed_fields}

            # Raio da entrega própria: campo em branco = sem limite próprio
            # (vale o raio da plataforma). String vazia iria pro banco como ''
            # e quebraria o NUMERIC.
            if 'own_delivery_radius_km' in updates:
                _r = updates['own_delivery_radius_km']
                try:
                    updates['own_delivery_radius_km'] = (
                        float(_r) if _r not in (None, '', 0, '0') and float(_r) > 0 else None)
                except (TypeError, ValueError):
                    updates['own_delivery_radius_km'] = None

            # Limite de itens: em branco/zero = sem limite. Sem isso, '' iria
            # pro banco e quebraria o INTEGER.
            if 'max_order_items' in updates:
                _m = updates['max_order_items']
                try:
                    updates['max_order_items'] = (
                        int(_m) if _m not in (None, '', 0, '0') and int(_m) > 0 else None)
                except (TypeError, ValueError):
                    updates['max_order_items'] = None
            if not updates:
                return jsonify({"status": "error", "error": "No valid fields to update"}), 400

            # Tipo da chave PIX: normaliza pro conjunto aceito pelo Asaas (a coluna
            # tem CHECK). Valor inválido/vazio vira NULL (auto-pay cai na inferência).
            if 'pix_key_type' in updates:
                _kt = (updates['pix_key_type'] or '').strip().upper()
                updates['pix_key_type'] = _kt if _kt in ('CPF', 'CNPJ', 'EMAIL', 'PHONE', 'EVP') else None

            # opening_hours é jsonb: adapta o dict para evitar erro de tipo
            if 'opening_hours' in updates and updates['opening_hours'] is not None:
                updates['opening_hours'] = psycopg2.extras.Json(updates['opening_hours'])

            # Geocode server-side: se o endereço está sendo salvo sem coordenadas,
            # resolve lat/lng aqui (fallback para quando o front não conseguiu).
            if ('address_street' in updates or 'address_city' in updates) and \
               not (updates.get('latitude') and updates.get('longitude')):
                with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as _c:
                    _c.execute(
                        "SELECT address_street, address_number, address_neighborhood, address_city, address_state, latitude, longitude FROM restaurant_profiles WHERE user_id = %s",
                        (user_id,),
                    )
                    _cur_prof = _c.fetchone() or {}
                _street = updates.get('address_street', _cur_prof.get('address_street'))
                _number = updates.get('address_number', _cur_prof.get('address_number'))
                _neigh = updates.get('address_neighborhood', _cur_prof.get('address_neighborhood'))
                _city = updates.get('address_city', _cur_prof.get('address_city'))
                _state = updates.get('address_state', _cur_prof.get('address_state'))
                if _street and _city:
                    _lat, _lng = _geocode_address(_street, _number, _neigh, _city, _state)
                    if _lat is not None:
                        updates['latitude'] = _lat
                        updates['longitude'] = _lng

            # Ao ABRIR o restaurante, registra heartbeat (o job de limpeza fecha
            # restaurantes abertos sem heartbeat recente — sessão abandonada/token expirado)
            if updates.get('is_open') is True or updates.get('is_open') == 'true':
                updates['last_heartbeat'] = datetime.now()
                # GATE server-side: não deixa ABRIR sem coordenadas. Sem elas o
                # frete quebra e o pedido pode vazar pra entregadores de qualquer
                # cidade (o app já bloqueia; isto é a defesa no backend).
                _has_new_coords = (updates.get('latitude') is not None
                                   and updates.get('longitude') is not None)
                if not _has_new_coords:
                    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as _cc:
                        _cc.execute(
                            "SELECT latitude, longitude FROM restaurant_profiles WHERE user_id = %s",
                            (user_id,))
                        _row = _cc.fetchone()
                    if not _row or _row['latitude'] is None or _row['longitude'] is None:
                        return jsonify({
                            "status": "error",
                            "error": "Complete o endereço (com localização no mapa) antes de abrir."
                        }), 400

            set_clause = ", ".join([f"{k} = %s" for k in updates.keys()])
            values = list(updates.values()) + [user_id]

            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                # UPSERT: cria o perfil se ainda não existir, depois aplica as atualizações
                cur.execute(
                    """INSERT INTO restaurant_profiles (id, user_id, restaurant_name, is_open)
                       VALUES (%s, %s, %s, FALSE)
                       ON CONFLICT (user_id) DO NOTHING""",
                    (user_id, user_id, updates.get('restaurant_name', 'Meu Restaurante'))
                )
                cur.execute(
                    f"UPDATE restaurant_profiles SET {set_clause} WHERE user_id = %s RETURNING *",
                    values
                )
                updated = cur.fetchone()
                conn.commit()
                if not updated:
                    return jsonify({"status": "error", "error": "Profile not found"}), 404
                return jsonify({"status": "success", "data": dict(updated)})
    
    except Exception as e:
        if conn: 
            conn.rollback()
        traceback.print_exc()
        return jsonify({"status": "error", "error": str(e)}), 500
    finally:
        if conn: 
            conn.close()

@restaurant_bp.route('/heartbeat', methods=['POST'])
@handle_db_errors
def restaurant_heartbeat(conn):
    """Sinal de vida do painel do restaurante.

    O app envia a cada poucos minutos enquanto estiver aberto. Um job no
    scheduler fecha (is_open=false) restaurantes sem heartbeat recente —
    cobre sessão abandonada, token expirado ou app fechado sem 'Fechar'.
    """
    user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
    if error:
        return error
    if user_type != 'restaurant':
        return jsonify({"status": "error", "error": "Unauthorized"}), 403

    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(
            """UPDATE restaurant_profiles
               SET last_heartbeat = NOW()
               WHERE user_id = %s
               RETURNING is_open""",
            (user_id,),
        )
        row = cur.fetchone()
        conn.commit()
    if not row:
        return jsonify({"status": "error", "error": "Profile not found"}), 404
    return jsonify({"status": "success", "data": {"is_open": row["is_open"]}}), 200


@restaurant_bp.route('/upload-logo', methods=['POST'])
def upload_logo():
    conn = None
    try:
        user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
        if error:
            return error
        if user_type != 'restaurant':
            return jsonify({"status": "error", "error": "Unauthorized"}), 403
        
        if 'logo' not in request.files:
            return jsonify({"status": "error", "error": "No file provided"}), 400

        file = request.files['logo']
        if not file.filename:
            return jsonify({"status": "error", "error": "Empty filename"}), 400

        allowed_extensions = ['.jpg', '.jpeg', '.png', '.gif']
        file_ext = os.path.splitext(file.filename)[1].lower()
        if file_ext not in allowed_extensions:
            return jsonify({"status": "error", "error": "Invalid file type. Only JPG, PNG and GIF allowed"}), 400

        unique_filename = f"{user_id}_{str(uuid.uuid4())}{file_ext}"
        
        upload_result = supabase.storage.from_("logos").upload(
            path=unique_filename,
            file=file.read(),
            file_options={"content-type": file.mimetype, "upsert": "true"}
        )
        
        public_url = supabase.storage.from_("logos").get_public_url(unique_filename)
        
        conn = get_db_connection()
        if not conn:
            return jsonify({"status": "error", "error": "Database connection failed"}), 500
            
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE restaurant_profiles SET logo_url = %s WHERE user_id = %s RETURNING logo_url",
                (public_url, user_id)
            )
            updated_row = cur.fetchone()
            if not updated_row:
                return jsonify({"status": "error", "error": "Restaurant profile not found"}), 404
                
            conn.commit()
            return jsonify({
                "status": "success", 
                "data": {
                    "logo_url": public_url,
                    "message": "Logo uploaded successfully"
                }
            })
    
    except Exception as e:
        if conn:
            conn.rollback()
        traceback.print_exc()
        return jsonify({"status": "error", "error": str(e)}), 500
    finally:
        if conn:
            conn.close()


@restaurant_bp.route('/payouts', methods=['GET'])
def get_my_payouts():
    """GET /api/restaurant/payouts — repasses do proprio restaurante autenticado.

    A pagina Financeiro do app do restaurante chamava /api/admin/payouts, que
    e admin-only e sempre retornava 403 -- saldo/proximo repasse/historico
    ficavam permanentemente vazios. Este endpoint devolve os mesmos dados,
    mas escopados ao restaurante do token (sem acesso a dados de terceiros).
    """
    conn = None
    try:
        user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
        if error:
            return error
        if user_type != 'restaurant':
            return jsonify({"status": "error", "error": "Acesso negado."}), 403

        conn = get_db_connection()
        if not conn:
            return jsonify({"status": "error", "error": "Erro de conexão com banco de dados"}), 500

        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT id FROM restaurant_profiles WHERE user_id = %s", (user_id,))
            profile = cur.fetchone()
            if not profile:
                return jsonify({"status": "error", "error": "Perfil de restaurante não encontrado."}), 404
            restaurant_id = profile['id']

            limit = min(int(request.args.get('limit') or 20), 100)

            cur.execute("""
                SELECT id, period_start, period_end, total_gross, commission_fee,
                       total_net, status, payment_method, payment_ref, created_at, updated_at
                  FROM payouts
                 WHERE partner_type = 'restaurant' AND partner_id = %s
              ORDER BY created_at DESC
                 LIMIT %s
            """, (restaurant_id, limit))
            rows = [dict(r) for r in cur.fetchall()]
            for r in rows:
                for key in ('period_start', 'period_end', 'created_at', 'updated_at'):
                    if r.get(key) and hasattr(r[key], 'isoformat'):
                        r[key] = r[key].isoformat()

            pending_rows = [r for r in rows if r['status'] in ('pending', 'pending_transfer')]
            balance_payouts = sum(float(r['total_net'] or 0) for r in pending_rows)
            next_payout_date = min((r['period_end'] for r in pending_rows), default=None) if pending_rows else None

            # Pendente REAL: pedidos entregues que ainda NAO entraram em nenhum
            # repasse (restaurant_payout_id IS NULL). Usa o valor_repassado_restaurante
            # ja gravado no pedido (exato, sem recalcular comissao). Assim o "A
            # Receber" sobe na hora que o pedido e entregue, sem esperar o
            # scheduler das 6h fechar o repasse.
            cur.execute("""
                SELECT COALESCE(SUM(valor_repassado_restaurante), 0) AS total,
                       COUNT(*) AS cnt
                  FROM orders
                 WHERE restaurant_id = %s
                   AND status = 'delivered'
                   AND restaurant_payout_id IS NULL
                   AND valor_repassado_restaurante IS NOT NULL
            """, (restaurant_id,))
            prow = cur.fetchone()
            pending_orders_total = float(prow['total'] or 0)
            pending_orders_count = int(prow['cnt'] or 0)

            # Total a receber = repasses ja gerados e nao pagos + pedidos entregues
            # ainda nao fechados (conjuntos disjuntos: os do payout tem payout_id).
            a_receber = balance_payouts + pending_orders_total

            cur.execute("""
                SELECT COALESCE(SUM(total_net), 0) AS month_total
                  FROM payouts
                 WHERE partner_type = 'restaurant' AND partner_id = %s
                   AND status = 'paid'
                   AND date_trunc('month', updated_at AT TIME ZONE 'America/Sao_Paulo')
                       = date_trunc('month', now() AT TIME ZONE 'America/Sao_Paulo')
            """, (restaurant_id,))
            month_total = float(cur.fetchone()['month_total'] or 0)

            # Dívida de comissão: só existe em loja de ENTREGA PRÓPRIA que
            # aceita dinheiro. Nesses pedidos o motoboy dela recolhe tudo, então
            # a comissão da Inksa vira dívida — abatida do próximo repasse
            # online. Sem isso o parceiro veria "A Receber" cheio e levaria um
            # susto quando o PIX viesse menor.
            cur.execute(
                "SELECT COALESCE(commission_debt, 0) AS divida FROM restaurant_profiles WHERE id = %s",
                (restaurant_id,))
            commission_debt = float((cur.fetchone() or {}).get('divida') or 0)

            return jsonify({
                "status": "success",
                "balance": round(a_receber, 2),
                "a_receber": round(a_receber, 2),
                "commission_debt": round(commission_debt, 2),
                "a_receber_liquido": round(max(0.0, a_receber - commission_debt), 2),
                "pendente_pedidos": round(pending_orders_total, 2),
                "pendente_pedidos_count": pending_orders_count,
                "next_payout_date": next_payout_date,
                "month_total": round(month_total, 2),
                "payouts": [
                    {
                        "id": str(r['id']),
                        "date": r.get('updated_at') if r['status'] == 'paid' else r.get('period_end'),
                        "amount": float(r['total_net'] or 0),
                        "status": r['status'],
                        "reference": r.get('payment_ref'),
                    }
                    for r in rows
                ],
            }), 200

    except Exception as e:
        traceback.print_exc()
        return jsonify({"status": "error", "error": str(e)}), 500
    finally:
        if conn:
            conn.close()
