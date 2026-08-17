
from flask import Blueprint, request, jsonify
from flask_cors import CORS
import math
import logging
from ..utils.helpers import supabase
from ..utils.platform_settings import get_settings

# Configuração do logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

delivery_calculator_bp = Blueprint('delivery_calculator', __name__)
CORS(delivery_calculator_bp)  # Habilita CORS para este blueprint

def haversine_distance(lat1, lon1, lat2, lon2):
    """Calcula a distância entre duas coordenadas usando a fórmula de Haversine"""
    R = 6371  # Raio da Terra em km
    lat1_rad = math.radians(lat1)
    lon1_rad = math.radians(lon1)
    lat2_rad = math.radians(lat2)
    lon2_rad = math.radians(lon2)
    
    dlon = lon2_rad - lon1_rad
    dlat = lat2_rad - lat1_rad
    
    a = math.sin(dlat / 2)**2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon / 2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    distance = R * c
    
    return distance

@delivery_calculator_bp.before_request
def handle_preflight():
    """Handle CORS preflight requests"""
    if request.method == "OPTIONS":
        response = jsonify()
        response.headers.add("Access-Control-Allow-Origin", "*")
        response.headers.add("Access-Control-Allow-Headers", "Content-Type,Authorization")
        response.headers.add("Access-Control-Allow-Methods", "GET,PUT,POST,DELETE,OPTIONS")
        return response

def _peso_do_carrinho(itens):
    """Peso total do carrinho, lido do CATÁLOGO. Devolve float (kg).

    O peso vem do catálogo, nunca do que o app enviou: peso que o cliente
    manda é frete que o cliente escolhe.

    Falhou? Devolve 0 e loga com stack. Um erro aqui não pode impedir a pessoa
    de ver o frete — mas ELE NÃO PODE SER MUDO: peso 0 significa "cobra sem
    adicional de carga", e um pedido de 120 kg saindo com frete de bicicleta
    não tem sintoma nenhum na tela. Já aconteceu duas vezes; por isso o
    exc_info.
    """
    import psycopg2.extras
    from ..utils.carga import peso_do_pedido
    from ..utils.helpers import get_db_connection

    if not itens:
        return 0.0

    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            logger.warning("Sem banco pra ler o peso do carrinho — frete sem adicional")
            return 0.0
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            return float(peso_do_pedido(cur, itens) or 0)
    except Exception:
        logger.warning("Peso do carrinho não lido — frete sem adicional de carga",
                       exc_info=True)
        return 0.0
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def _quem_pode_entregar(peso_kg, rest_lat, rest_lng, settings):
    """Existe entregador capaz de levar ESTE peso? Responde no carrinho.

    O problema que isto resolve: o cliente monta 3 sacos de ração, paga, e o
    pedido fica parado porque ninguém na plataforma tem veículo pra 90 kg. Ele
    só descobre esperando. Dinheiro preso e confiança perdida por uma coisa
    que dava pra dizer ANTES.

    Devolve (cadastrados, online):
      • cadastrados = quantos existem com capacidade suficiente, no raio,
        aprovados e com veículo reconhecido — INDEPENDENTE de estar online.
        Zero aqui é estrutural: não adianta esperar, ninguém vai poder pegar.
      • online = quantos desses estão disponíveis agora. Zero aqui é
        temporário — enquanto a loja prepara, alguém pode entrar.

    A diferença entre os dois é o que separa "não dá" de "pode demorar", e é
    por isso que a contagem é dupla. Um número só forçaria escolher entre
    bloquear demais ou avisar de menos.

    Nunca levanta: erro devolve (None, None) e quem chama omite o aviso. Um
    carrinho que não fecha por causa desta consulta seria pior que o problema.
    """
    import psycopg2.extras
    from ..utils.carga import capacidades
    from ..utils.helpers import get_db_connection

    if rest_lat is None or rest_lng is None:
        return (None, None)

    caps = capacidades(settings)
    try:
        peso = float(peso_kg or 0)
    except (TypeError, ValueError):
        peso = 0.0

    def _raio(chave, padrao_global):
        try:
            v = float(settings.get(f'delivery_radius_{chave}_km') or 0)
        except (TypeError, ValueError):
            v = 0.0
        return v if v > 0 else padrao_global

    try:
        r_global = float(settings.get('platform_max_delivery_radius') or 15)
    except (TypeError, ValueError):
        r_global = 15.0

    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return (None, None)
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # Mesmos apelidos do motor de despacho: o CHECK da tabela aceita
            # 'motorcycle'/'car' (legado), e ignorá-los aqui faria a contagem
            # dizer "ninguém pode" com entregador capaz cadastrado.
            cur.execute("""
                SELECT
                  CASE
                    WHEN dp.vehicle_type IN ('bike','bicicleta')  THEN %s
                    WHEN dp.vehicle_type IN ('moto','motorcycle') THEN %s
                    WHEN dp.vehicle_type IN ('carro','car')       THEN %s
                    WHEN dp.vehicle_type = 'utilitario'           THEN %s
                    ELSE 0 END AS capacidade,
                  CASE
                    WHEN dp.vehicle_type IN ('bike','bicicleta')  THEN %s
                    WHEN dp.vehicle_type IN ('moto','motorcycle') THEN %s
                    WHEN dp.vehicle_type IN ('carro','car')       THEN %s
                    WHEN dp.vehicle_type = 'utilitario'           THEN %s
                    ELSE %s END AS raio_km,
                  earth_distance(
                    ll_to_earth(COALESCE(dp.current_lat, dp.latitude),
                                COALESCE(dp.current_lng, dp.longitude)),
                    ll_to_earth(%s, %s)) / 1000.0 AS dist_km,
                  COALESCE(dp.is_available, false) AS online
                FROM delivery_profiles dp
               WHERE COALESCE(dp.approved, false)
                 AND COALESCE(dp.current_lat, dp.latitude) IS NOT NULL
                 AND COALESCE(dp.current_lng, dp.longitude) IS NOT NULL
            """, (caps['bike'], caps['moto'], caps['carro'], caps['utilitario'],
                  _raio('bike', r_global), _raio('moto', r_global),
                  _raio('carro', r_global), _raio('utilitario', r_global), r_global,
                  float(rest_lat), float(rest_lng)))

            cadastrados = 0
            online = 0
            for r in cur.fetchall():
                if float(r['capacidade'] or 0) < peso:
                    continue
                if float(r['dist_km'] or 0) > float(r['raio_km'] or 0):
                    continue
                cadastrados += 1
                if r['online']:
                    online += 1
        return (cadastrados, online)
    except Exception:
        logger.warning("Não deu pra checar entregador disponível", exc_info=True)
        return (None, None)
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


@delivery_calculator_bp.route('/calculate_fee', methods=['POST', 'OPTIONS'])
def calculate_delivery_fee():
    """Calcula a taxa de entrega baseada no restaurante e localização do cliente"""
    try:
        logger.info("=== INÍCIO calculate_delivery_fee ===")
        
        if request.method == 'OPTIONS':
            return handle_preflight()
        
        data = request.get_json()
        logger.info(f"Dados recebidos: {data}")
        
        # Validação dos dados de entrada
        restaurant_id = data.get('restaurant_id')
        client_latitude = data.get('client_latitude') 
        client_longitude = data.get('client_longitude')
        # Itens do carrinho: só pra saber o PESO. O adicional de carga
        # (carro/utilitário) tem que entrar aqui, no checkout, porque é aqui
        # que o cliente vê o preço — depois de fechado, mudar frete é quebrar
        # combinado.
        itens_carrinho = data.get('items') or data.get('itens') or []
        
        if not restaurant_id:
            logger.warning("restaurant_id não fornecido")
            return jsonify({
                "status": "error",
                "error": "restaurant_id é obrigatório"
            }), 400
        
        if not client_latitude or not client_longitude:
            logger.warning("Coordenadas do cliente não fornecidas")
            return jsonify({
                "status": "error", 
                "error": "Coordenadas do cliente são obrigatórias"
            }), 400

        # Buscar dados do restaurante
        logger.info(f"Buscando restaurante: {restaurant_id}")
        
        response = supabase.table('restaurant_profiles').select(
            'latitude, longitude, delivery_type, delivery_fee, restaurant_name, '
            'own_delivery_radius_km'
        ).eq('id', restaurant_id).execute()
        
        if not response.data or len(response.data) == 0:
            logger.error(f"Restaurante não encontrado: {restaurant_id}")
            return jsonify({
                "status": "error",
                "error": "Restaurante não encontrado"
            }), 404

        restaurant_data = response.data[0]
        logger.info(f"Dados do restaurante: {restaurant_data}")
        
        delivery_type = restaurant_data.get('delivery_type', 'platform')
        distance_km = 0.0
        delivery_fee = 0.0
        calculation_method = ""

        # Calcular taxa baseada no tipo de entrega
        if delivery_type == 'own':
            # Restaurante faz própria entrega: taxa FIXA, não muda com a
            # distância. Justamente por isso o raio dele é uma trava de dinheiro
            # — sem ela, um pedido a 30 km continuaria custando o mesmo pra ele.
            # A listagem já esconde a loja fora do raio, mas link direto pro
            # cardápio pula a listagem; aqui é a barreira que vale.
            raio = restaurant_data.get('own_delivery_radius_km')
            r_lat = restaurant_data.get('latitude')
            r_lon = restaurant_data.get('longitude')
            if raio and r_lat and r_lon:
                distance_km = haversine_distance(
                    float(r_lat), float(r_lon),
                    float(client_latitude), float(client_longitude)
                )
                if distance_km > float(raio):
                    logger.info(
                        f"Fora do raio próprio: {distance_km:.2f} km > {float(raio):.2f} km")
                    return jsonify({
                        "status": "error",
                        "error": "fora_da_area",
                        "message": (
                            f"{restaurant_data.get('restaurant_name') or 'Esta loja'} entrega "
                            f"até {float(raio):.0f} km e seu endereço está a "
                            f"{distance_km:.1f} km."
                        ),
                    }), 200

            delivery_fee = float(restaurant_data.get('delivery_fee', 0.0))
            calculation_method = "Taxa fixa do restaurante"
            logger.info(f"Entrega própria: R$ {delivery_fee}")
            
        elif delivery_type == 'platform':
            # Plataforma calcula baseado na distância (lê do platform_settings com cache)
            s = get_settings()
            fixed_fee = float(s["fixed_delivery_fee"])
            per_km_fee = float(s["per_km_delivery_fee"])
            free_threshold = float(s["free_delivery_threshold_km"])

            restaurant_latitude = restaurant_data.get('latitude')
            restaurant_longitude = restaurant_data.get('longitude')

            # Se o restaurante ainda não tem coordenadas, NÃO quebra o carrinho:
            # cobra a taxa base (fixa) e sinaliza que a distância não foi calculada.
            if not restaurant_latitude or not restaurant_longitude:
                logger.warning("Restaurante sem coordenadas — usando taxa base fixa")
                delivery_fee = fixed_fee
                distance_km = 0.0
                calculation_method = f"Taxa base R$ {fixed_fee:.2f} (restaurante sem localização cadastrada)"
            else:
                # Calcular distância
                distance_km = haversine_distance(
                    float(restaurant_latitude), float(restaurant_longitude),
                    float(client_latitude), float(client_longitude)
                )
                logger.info(f"Distância calculada: {distance_km} km")

                # RAIO DA PLATAFORMA — a trava que faltava aqui.
                #
                # O ramo 'own' logo acima já recusava fora da área; o
                # 'platform' seguia direto pra conta taxa base + km × preço,
                # sem teto. Em 13 e 15/08/2026 isso produziu dois pedidos de
                # São Paulo pra Nova Iguaçu: 331,74 km, frete de R$ 501,10,
                # com o raio configurado em 50 km. Um gerou cobrança PIX de
                # R$ 553,10.
                from ..utils.area_entrega import verificar_area
                _ok, _area = verificar_area(
                    restaurant_latitude, restaurant_longitude,
                    client_latitude, client_longitude,
                    delivery_type='platform',
                    settings=s,
                    nome_loja=restaurant_data.get('restaurant_name'),
                )
                if not _ok:
                    logger.info("Fora do raio da plataforma: %.2f km > %.2f km",
                                _area["distancia_km"], _area["raio_km"])
                    return jsonify({
                        "status": "error",
                        "error": _area["error"],
                        "message": _area["message"],
                    }), 200

                # A conta mora em utils/frete.py porque o FECHAMENTO do pedido
                # refaz a mesma conta pra não confiar no que o app mandou. Duas
                # cópias divergiriam — e a divergência apareceria como frete
                # errado cobrado do cliente ou pago ao entregador.
                from ..utils.frete import calcular_frete
                peso_carrinho = _peso_do_carrinho(itens_carrinho)
                delivery_fee, calculation_method = calcular_frete(
                    distance_km, peso_carrinho, s)

            logger.info(f"Taxa calculada: R$ {delivery_fee}")
        
        else:
            logger.error(f"Tipo de entrega inválido: {delivery_type}")
            return jsonify({
                "status": "error",
                "error": "Tipo de entrega inválido"
            }), 400
        
        # Arredondar valores
        delivery_fee = round(delivery_fee, 2)
        distance_km = round(distance_km, 2)
        
        # Loja de entrega própria não depende de entregador da plataforma.
        _peso_resp = float(locals().get('peso_carrinho') or 0)
        if delivery_type == 'own':
            _cap_total, _cap_online = None, None
        else:
            _cap_total, _cap_online = _quem_pode_entregar(
                _peso_resp, restaurant_data.get('latitude'),
                restaurant_data.get('longitude'), s)

        result = {
            "status": "success",
            "data": {
                "delivery_fee": delivery_fee,
                "delivery_distance_km": distance_km,
                "delivery_type": delivery_type,
                "calculation_method": calculation_method,
                "restaurant_name": restaurant_data.get('restaurant_name', ''),
                "message": "Cálculo de frete realizado com sucesso",
                # Quem consegue levar este peso. Vai junto do frete porque o
                # carrinho já faz esta chamada e os dois dependem do MESMO
                # peso — separar em outra rota criaria a chance de divergirem.
                "peso_total_kg": _peso_resp,
                "entregadores_capazes": _cap_total,
                "entregadores_online": _cap_online
            }
        }
        
        logger.info(f"Resultado final: {result}")
        return jsonify(result), 200

    except ValueError as e:
        logger.error(f"Erro de validação: {e}")
        return jsonify({
            "status": "error",
            "error": "Dados inválidos fornecidos"
        }), 400
        
    except Exception as e:
        logger.error(f"Erro inesperado ao calcular frete: {e}", exc_info=True)
        return jsonify({
            "status": "error", 
            "error": "Erro interno ao calcular o frete"
        }), 500

@delivery_calculator_bp.route('/test', methods=['GET'])
def test_delivery_calculator():
    """Endpoint de teste para verificar se o serviço está funcionando"""
    s = get_settings()
    return jsonify({
        "status": "success",
        "message": "Serviço de cálculo de frete funcionando",
        "config": {
            "fixed_fee": float(s["fixed_delivery_fee"]),
            "free_threshold_km": float(s["free_delivery_threshold_km"]),
            "per_km_fee": float(s["per_km_delivery_fee"]),
        }
    }), 200
