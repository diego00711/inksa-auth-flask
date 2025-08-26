from flask import Blueprint, jsonify
from datetime import datetime

admin_logs_bp = Blueprint('admin_logs', __name__)

# Dados de exemplo, substitua por acesso ao banco depois
logs = [
    {
        "id": 1,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "admin": "admin@inksa.com",
        "action": "Login",
        "details": "Admin fez login no sistema"
    }
]

@admin_logs_bp.route('/api/logs', methods=['GET'])
def get_logs():
    return jsonify(logs)
