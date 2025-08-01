# Arquivo: src/utils/geocoding_utils.py
import requests
import os

def geocode_address(street, number, neighborhood, city, state, zipcode):
    """
    Geocodifica um endereço completo para latitude e longitude usando Nominatim (OpenStreetMap).
    Requer uma chave de API para serviços comerciais como Google Maps/Mapbox em produção.
    
    Args:
        street (str): Nome da rua.
        number (str): Número do endereço.
        neighborhood (str): Bairro.
        city (str): Cidade.
        state (str): Estado (sigla, ex: "SP", "RJ").
        zipcode (str): CEP.
        
    Returns:
        tuple: (latitude, longitude) como floats, ou (None, None) se a geocodificação falhar.
    """
    address_parts = [
        f"{number} {street}" if number else street,
        neighborhood,
        city,
        state,
        zipcode
    ]
    # Filtra None e strings vazias para formar o endereço completo
    full_address = ", ".join(filter(None, address_parts)) 
    
    if not full_address:
        print("Geocodificação: Endereço vazio, retornando None, None.")
        return None, None 

    # IMPORTANTE: Para Nominatim, um User-Agent é obrigatório.
    # Use um User-Agent que identifique sua aplicação.
    headers = {
        'User-Agent': 'InksaDeliveryApp/1.0 (contact@yourdomain.com)' 
    }
    
    # Endpoint da API Nominatim
    nominatim_url = "https://nominatim.openstreetmap.org/search"
    params = {
        'q': full_address,
        'format': 'json',
        'limit': 1,
        'addressdetails': 0 
    }
    
    try:
        response = requests.get(nominatim_url, params=params, headers=headers)
        response.raise_for_status() # Lança um erro para status HTTP 4xx/5xx
        
        results = response.json()
        
        if results and len(results) > 0:
            lat = float(results[0].get('lat'))
            lon = float(results[0].get('lon'))
            print(f"Geocodificação bem-sucedida para '{full_address}': Lat={lat}, Lon={lon}")
            return lat, lon
        else:
            print(f"Geocodificação falhou para o endereço: '{full_address}'. Nenhum resultado encontrado.")
            return None, None
    except requests.exceptions.RequestException as e:
        print(f"Erro na requisição HTTP de geocodificação para '{full_address}': {e}")
        return None, None
    except ValueError as e:
        print(f"Erro ao processar resposta JSON da geocodificação para '{full_address}': {e}")
        return None, None

# Teste (opcional, pode ser removido depois)
if __name__ == "__main__":
    print("Testando geocodificação...")
    lat, lon = geocode_address("Rua XV de Novembro", "1000", "Centro", "Lages", "SC", "88501-000")
    if lat and lon:
        print(f"Coordenadas de Lages: Latitude={lat}, Longitude={lon}")
    else:
        print("Geocodificação de Lages falhou.")

    lat, lon = geocode_address("Rua sem nome", "", "", "", "", "")
    if not lat and not lon:
        print("Teste com endereço vazio funcionou como esperado.")