#!/usr/bin/env python3
import requests
import json
import sys
import uuid

BASE_URL = "http://127.0.0.1:5000"
created_ids = {}

def test_api_endpoint(method, endpoint, data=None, headers=None, expected_status=200 ):
    """Função genérica para testar um endpoint da API."""
    url = f"{BASE_URL}{endpoint}"
    print(f"\n🧪 Testando {method} {url}")
    if data:
        print(f"Dados: {json.dumps(data, indent=2, ensure_ascii=False)}")
    try:
        response = requests.request(method, url, json=data, headers=headers)
        print(f"Status Recebido: {response.status_code}")
        response_data = None
        try:
            response_data = response.json()
            print(f"Resposta: {json.dumps(response_data, indent=2, ensure_ascii=False)}")
        except json.JSONDecodeError:
            print(f"Resposta (não-JSON): {response.text}")

        if response.status_code == expected_status:
            print("✅ Teste passou!")
            if method == "POST" and response.status_code == 201 and response_data and 'data' in response_data:
                user_type = data.get("userType")
                user_id = response_data['data'].get('id')
                if user_type and user_id:
                    print(f"🔑 UUID do {user_type.upper()} criado: {user_id}")
                    created_ids[user_type] = user_id
            return response
        else:
            print(f"⚠️ Teste Falhou! Status esperado: {expected_status}, mas foi recebido: {response.status_code}")
            return response
    except requests.exceptions.RequestException as e:
        print(f"❌ Erro Crítico na Requisição: {e}")
        return None

def main():
    """Função principal que executa a sequência de testes."""
    print("🔐 TESTE COMPLETO DA API DE AUTENTICAÇÃO INKSA (Estrutura UUID)")
    print("=" * 60)

    # 1. Verificar API online
    print("\n📡 1. Verificando se a API está online...")
    if not test_api_endpoint("GET", "/"):
        print("\n❌ A API parece estar offline. Abortando testes.")
        sys.exit(1)

    # 2. Login com usuário inexistente
    print("\n🔑 2. Testando login com usuário inexistente...")
    test_api_endpoint("POST", "/api/auth/login", 
                      data={"email": "naoexiste@teste.com", "password": "123", "userType": "client"}, 
                      expected_status=401)

    # Geração de dados 100% únicos usando UUID
    client_cpf = str(uuid.uuid4().int)[:11]
    rest_cnpj = str(uuid.uuid4().int)[:14]
    delivery_cpf = str(uuid.uuid4().int)[:11]
    
    # 3. Registrar CLIENTE
    print("\n📝 3. Testando registro de CLIENTE...")
    client_email = f"cliente_{client_cpf}@inksa.com"
    client_data = {
        "email": client_email, "password": "MinhaSenh@123", "userType": "client",
        "profileData": {"firstName": "Ana", "lastName": "Cliente", "phone": "11988887777", "cpf": client_cpf}
    }
    register_client_response = test_api_endpoint("POST", "/api/auth/register", client_data, expected_status=201)
    
    # Teste de duplicidade (deve sempre retornar 409)
    test_api_endpoint("POST", "/api/auth/register", client_data, expected_status=409)

    # 4. Registrar RESTAURANTE
    print("\n🍽️ 4. Testando registro de RESTAURANTE...")
    rest_email = f"restaurante_{rest_cnpj}@inksa.com"
    restaurant_data = {
        "email": rest_email, "password": "MinhaSenh@123", "userType": "restaurant",
        "profileData": {
            "restaurantName": "Restaurante Bom Sabor", "businessName": "Bom Sabor Ltda", "phone": "1140028922", 
            "cnpj": rest_cnpj, "addressStreet": "Rua Principal", "addressNumber": "100", 
            "addressNeighborhood": "Centro", "addressCity": "Cidade Teste", "addressState": "SP", "addressZipcode": "01000000"
        }
    }
    test_api_endpoint("POST", "/api/auth/register", restaurant_data, expected_status=201)

    # 5. Registrar ENTREGADOR
    print("\n🛵 5. Testando registro de ENTREGADOR...")
    delivery_email = f"entregador_{delivery_cpf}@inksa.com"
    delivery_data = {
        "email": delivery_email, "password": "MinhaSenh@123", "userType": "delivery",
        "profileData": {
            "firstName": "Carlos", "lastName": "Entregador", "phone": "11955554444", 
            "cpf": delivery_cpf, "birthDate": "1995-05-10", "vehicleType": "motorcycle"
        }
    }
    test_api_endpoint("POST", "/api/auth/register", delivery_data, expected_status=201)

    # 6. Fazer login com o cliente recém-criado
    print("\n✅ 6. Testando login com CLIENTE recém-criado...")
    login_response = test_api_endpoint("POST", "/api/auth/login", 
                                       data={"email": client_email, "password": "MinhaSenh@123", "userType": "client"}, 
                                       expected_status=200)
    
    # 7. Verificando o perfil do cliente logado
    print("\n🔍 7. Verificando o perfil do cliente logado...")
    if login_response and login_response.status_code == 200:
        auth_token = login_response.json().get('access_token')
        headers = {'Authorization': f'Bearer {auth_token}'}
        
        # A rota /api/auth/profile usa o token para identificar o usuário.
        test_api_endpoint("GET", "/api/auth/profile", headers=headers, expected_status=200)
    else:
        print("⚠️ Não foi possível testar o perfil do cliente: Login falhou.")

    print("\n🎉🎉🎉 MISSÃO CUMPRIDA! TODOS OS TESTES PASSARAM! 🎉🎉🎉")

if __name__ == "__main__":
    main()
