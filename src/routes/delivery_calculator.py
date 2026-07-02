
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
            'latitude, longitude, delivery_type, delivery_fee, restaurant_name'
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
            # Restaurante faz própria entrega
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

                delivery_fee = fixed_fee
                if distance_km > free_threshold:
                    additional_km = distance_km - free_threshold
                    delivery_fee += additional_km * per_km_fee
                    calculation_method = f"Taxa base R$ {fixed_fee:.2f} + R$ {per_km_fee:.2f}/km extra"
                else:
                    calculation_method = f"Taxa base R$ {fixed_fee:.2f} (dentro do limite gratuito)"

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
        
        result = {
            "status": "success",
            "data": {
                "delivery_fee": delivery_fee,
                "delivery_distance_km": distance_km,
                "delivery_type": delivery_type,
                "calculation_method": calculation_method,
                "restaurant_name": restaurant_data.get('restaurant_name', ''),
                "message": "Cálculo de frete realizado com sucesso"
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
