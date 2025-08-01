import os
import psycopg2
from dotenv import load_dotenv

print("--- INICIANDO TESTE DE CONEXÃO DIRETA ---")

print("1. Carregando variáveis do arquivo .env...")
load_dotenv()

db_url = os.getenv('DATABASE_URL')

if not db_url:
    print("❌ ERRO: A variável DATABASE_URL não foi encontrada ou está vazia no arquivo .env!")
else:
    print("✅ Variável DATABASE_URL encontrada.")
    print("\n2. Tentando conectar ao banco de dados...")

    try:
        # Tenta estabelecer a conexão
        conn = psycopg2.connect(db_url)
        print("✅ SUCESSO! A conexão com o banco de dados foi estabelecida!")
        conn.close()
        print("   Conexão fechada com sucesso.")
    except Exception as e:
        # Se falhar, imprime o erro exato
        print("❌ FALHA! Não foi possível conectar ao banco de dados.")
        print(f"\n   ERRO DETALHADO: {e}")

print("\n--- TESTE DE CONEXÃO FINALIZADO ---")