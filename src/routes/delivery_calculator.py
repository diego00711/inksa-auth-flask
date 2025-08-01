# src/routes/delivery_calculator.py

from flask import Blueprint, request, jsonify
from supabase import create_client, Client
import os
import math

delivery_calculator_bp = Blueprint('delivery_calculator', __name__)

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
supabase_client: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

def haversine_distance(lat1, lon1, lat2, lon2):
    R = 6371
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

FIXED_DELIVERY_FEE = 5.00
PER_KM_DELIVERY_FEE = 2.50
FREE_DELIVERY_THRESHOLD_KM = 3.0

@delivery_calculator_bp.route('/delivery/calculate_fee', methods=['POST'])
def calculate_delivery_fee():
    try:
        data = request.json
        restaurant_id = data.get('restaurant_id')
        client_latitude = data.get('client_latitude')
        client_longitude = data.get('client_longitude')

        if not all([restaurant_id, client_latitude, client_longitude]):
            return jsonify({"error": "Dados incompletos."}), 400

        # --- LÓGICA ALTERADA ---
        # 1. Buscar o perfil do restaurante para verificar o tipo de entrega
        response_restaurant = supabase_client.table('restaurant_profiles').select(
            'latitude, longitude, delivery_type, delivery_fee'
        ).eq('id', restaurant_id).single().execute()

        if not response_restaurant.data:
            return jsonify({"error": "Restaurante não encontrado."}), 404

        restaurant_data = response_restaurant.data
        delivery_type = restaurant_data.get('delivery_type', 'platform')
        distance_km = 0.0
        delivery_fee = 0.0

        # 2. Decidir qual cálculo de frete usar
        if delivery_type == 'own':
            # Usa a taxa de entrega fixa definida pelo restaurante
            print(f"INFO: Calculando frete com base na taxa própria do restaurante {restaurant_id}.")
            delivery_fee = float(restaurant_data.get('delivery_fee', 0.0))
        
        elif delivery_type == 'platform':
            # Usa a lógica de cálculo dinâmico da plataforma
            print(f"INFO: Calculando frete dinamicamente para o restaurante {restaurant_id}.")
            restaurant_latitude = restaurant_data.get('latitude')
            restaurant_longitude = restaurant_data.get('longitude')

            if restaurant_latitude is None or restaurant_longitude is None:
                return jsonify({"error": "Coordenadas do restaurante incompletas."}), 400

            distance_km = haversine_distance(
                float(restaurant_latitude), float(restaurant_longitude),
                float(client_latitude), float(client_longitude)
            )

            fee = FIXED_DELIVERY_FEE
            if distance_km > FREE_DELIVERY_THRESHOLD_KM:
                additional_km_cost = (distance_km - FREE_DELIVERY_THRESHOLD_KM) * PER_KM_DELIVERY_FEE
                fee += additional_km_cost
            delivery_fee = fee
        
        # Arredondamentos
        delivery_fee = round(delivery_fee, 2)
        distance_km = round(distance_km, 2)
        # --- FIM DA LÓGICA ALTERADA ---

        return jsonify({
            "delivery_fee": delivery_fee,
            "delivery_distance_km": distance_km,
            "message": "Cálculo de frete realizado com sucesso."
        }), 200

    except Exception as e:
        print(f"Erro ao calcular frete: {e}")
        return jsonify({"error": "Erro interno ao calcular o frete."}), 500