from flask import Blueprint, request, jsonify, Response
from datetime import datetime
from io import StringIO
import csv

from src.utils.helpers import supabase

admin_logs_bp = Blueprint("admin_logs", __name__)

def parse_iso_date(value: str):
    if not value:
        return None
    try:
        # aceita YYYY-MM-DD ou ISO completo
        if len(value) == 10:
            return datetime.fromisoformat(value + "T00:00:00")
        return datetime.fromisoformat(value)
    except Exception:
        return None

def build_query(params):
    search = params.get("search")
    action = params.get("action")
    admin = params.get("admin")
    start = parse_iso_date(params.get("start"))
    end = parse_iso_date(params.get("end"))
    sort = params.get("sort", "-timestamp")  # -timestamp ou timestamp

    query = supabase.table("admin_logs").select("*", count="exact")

    # Filtros
    if search:
        # Busca textual em details; se quiser, inclua action/admin também
        query = query.ilike("details", f"%{search}%")
    if action:
        query = query.eq("action", action)
    if admin:
        query = query.eq("admin", admin)
    if start:
        # timestamp >= start
        query = query.gte("timestamp", start.isoformat())
    if end:
        # timestamp <= end fim do dia, se veio só data
        if len(params.get("end", "")) == 10:
            end = end.replace(hour=23, minute=59, second=59, microsecond=999999)
        query = query.lte("timestamp", end.isoformat())

    # Ordenação
    desc = False
    field = "timestamp"
    if sort.startswith("-"):
        desc = True
        field = sort[1:]
    elif sort:
        field = sort
    query = query.order(field, desc=desc)

    return query

@admin_logs_bp.route("/api/logs", methods=["GET", "HEAD"])
def get_logs():
    if request.method == "HEAD":
        return ("", 200)

    # Paginação
    try:
        page = int(request.args.get("page", "1"))
        page_size = int(request.args.get("page_size", "20"))
    except ValueError:
        return jsonify({"error": "Parâmetros de paginação inválidos"}), 400

    page = max(page, 1)
    page_size = max(min(page_size, 100), 1)  # limite de 100/pg

    start_idx = (page - 1) * page_size
    end_idx = start_idx + page_size - 1

    query = build_query(request.args)

    # Executa com paginação
    res = query.range(start_idx, end_idx).execute()
    data = res.data or []
    total = res.count or 0

    return jsonify({
        "items": data,
        "page": page,
        "page_size": page_size,
        "total": total,
        "has_next": (start_idx + len(data)) < total
    })

@admin_logs_bp.route("/api/logs/export", methods=["GET"])
def export_logs_csv():
    # Exporta com os mesmos filtros, sem paginação (limite de segurança)
    limit = int(request.args.get("limit", "5000"))
    limit = max(min(limit, 20000), 1)

    query = build_query(request.args)
    res = query.limit(limit).execute()
    rows = res.data or []

    # Gera CSV em memória
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "timestamp", "admin", "action", "details"])
    for r in rows:
        writer.writerow([
            r.get("id", ""),
            r.get("timestamp", ""),
            r.get("admin", ""),
            r.get("action", ""),
            r.get("details", "")
        ])

    csv_bytes = output.getvalue().encode("utf-8-sig")  # BOM p/ Excel
    headers = {
        "Content-Disposition": "attachment; filename=logs.csv",
        "Content-Type": "text/csv; charset=utf-8"
    }
    return Response(csv_bytes, headers=headers)
