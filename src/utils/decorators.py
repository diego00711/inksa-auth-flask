from functools import wraps
from flask import request, jsonify, g
from config import (
    supabase,
)  # Assume que a tua inicialização do Supabase está em config.py


def delivery_token_required(f):
    """
    Verifica se o token JWT é válido e se o utilizador é do tipo 'delivery'.
    """

    @wraps(f)
    def decorated_function(*args, **kwargs):
        token = None
        if "Authorization" in request.headers:
            # O cabeçalho deve ser 'Bearer <token>'
            token = request.headers["Authorization"].split(" ")[1]

        if not token:
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "Token de autenticação está em falta!",
                    }
                ),
                401,
            )

        try:
            # Valida o token com o Supabase e obtém os dados do utilizador
            user_response = supabase.auth.get_user(token)
            user = user_response.user

            if not user:
                raise Exception("Token inválido ou expirado.")

            # Verifica se o tipo de utilizador é 'delivery' (armazenado nos metadados do Supabase Auth)
            if (
                "user_type" not in user.user_metadata
                or user.user_metadata["user_type"] != "delivery"
            ):
                return (
                    jsonify(
                        {
                            "status": "error",
                            "message": "Acesso não autorizado para este tipo de utilizador.",
                        }
                    ),
                    403,
                )

            # Anexa os dados do utilizador ao contexto global da requisição (g)
            g.user = user

        except Exception as e:
            return (
                jsonify(
                    {"status": "error", "message": f"Erro de autenticação: {str(e)}"}
                ),
                401,
            )

        return f(*args, **kwargs)

    return decorated_function
