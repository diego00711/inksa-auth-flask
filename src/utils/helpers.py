*** src/utils/helpers.py
@@
 def get_user_id_from_token(auth_header):
     token = _extract_bearer_token(auth_header)
     if not token:
         return None, None, (jsonify({"error": "Authorization ausente ou inválido"}), 401)
 
     conn = None
     try:
         if not supabase:
             raise RuntimeError("Supabase client não inicializado.")
 
         # Valida o JWT no Supabase e extrai o user.id (UUID do auth)
         user_resp = supabase.auth.get_user(token)
         user = getattr(user_resp, "user", None)
         if not user:
             return None, None, (jsonify({"error": "Token inválido ou expirado"}), 401)
 
         user_id = str(user.id)
 
         conn = get_db_connection()
         if not conn:
             return None, None, (jsonify({"error": "Falha ao conectar para verificar permissões"}), 500)
 
         with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
-            # cobre cenários com coluna id OU uuid (alguns projetos mantêm as duas)
-            cur.execute("""
-                SELECT user_type
-                FROM public.users
-                WHERE id = %s OR uuid = %s
-                LIMIT 1
-            """, (user_id, user_id))
-            row = cur.fetchone()
+            # ✅ versão segura: consulta SOMENTE por 'id'
+            cur.execute("""
+                SELECT user_type
+                FROM public.users
+                WHERE id = %s
+                LIMIT 1
+            """, (user_id,))
+            row = cur.fetchone()
+
+            # (Opcional) Fallback: se sua permissão fica em outra tabela,
+            # tente localizar o usuário no catálogo do Supabase Auth
+            if not row:
+                try:
+                    cur.execute("""
+                        SELECT id
+                        FROM auth.users
+                        WHERE id = %s
+                        LIMIT 1
+                    """, (user_id,))
+                    auth_row = cur.fetchone()
+                    # Se existe no auth mas não tem registro de permissão,
+                    # trate como "sem permissão" (403) para evitar 500.
+                    if auth_row:
+                        return None, None, (jsonify({"error": "Permissão não encontrada para este usuário"}), 403)
+                except Exception:
+                    # Se o role do banco não permite ler auth.users, apenas ignore o fallback.
+                    pass
 
-        if not row or not row.get("user_type"):
+        if not row or not row.get("user_type"):
             return None, None, (jsonify({"error": "Permissão não encontrada para este usuário"}), 403)
 
         return user_id, row["user_type"], None
 
     except Exception as e:
         msg = str(e)
         logger.error(f"Erro ao processar token: {msg}", exc_info=True)
         if "invalid" in msg.lower() or "jwt" in msg.lower() or "token" in msg.lower():
             return None, None, (jsonify({"error": f"Erro de autenticação: {msg}"}), 401)
         return None, None, (jsonify({"error": "Erro interno ao validar token"}), 500)
     finally:
         if conn:
             conn.close()
