# cleanup_supabase_users.py - VERSÃO CORRIGIDA
import os
from supabase import create_client, Client
from dotenv import load_dotenv

# Carrega as variáveis de ambiente do seu arquivo .env
load_dotenv()

# Pega as credenciais do .env
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    print(
        "❌ Erro: SUPABASE_URL e SUPABASE_SERVICE_KEY devem estar no seu arquivo .env"
    )
    exit()

print("Conectando ao Supabase para limpeza...")
# Cria um cliente com privilégios de administrador
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def cleanup_users():
    """Lista e deleta todos os usuários do sistema de autenticação do Supabase."""
    try:
        # 1. Listar todos os usuários
        response = supabase.auth.admin.list_users()

        # ✅✅✅ CORREÇÃO AQUI ✅✅✅
        # A resposta da API já é a lista de usuários, não um objeto contendo a lista.
        users = response

        if not users:
            print(
                "✅ Nenhum usuário encontrado no sistema de autenticação. Tudo limpo!"
            )
            return

        print(f"Encontrados {len(users)} usuários para deletar:")
        for user in users:
            print(f"  - Deletando usuário: {user.email} (ID: {user.id})")
            # 2. Deletar cada usuário pelo seu ID
            supabase.auth.admin.delete_user(user.id)

        print(
            "\n✅ Todos os usuários do sistema de autenticação foram deletados com sucesso!"
        )

    except Exception as e:
        print(f"\n❌ Ocorreu um erro durante a limpeza: {e}")


if __name__ == "__main__":
    cleanup_users()
