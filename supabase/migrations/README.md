# Admin Logs Migration and Usage

This migration creates the `admin_logs` table required for the `/api/logs` endpoints to function properly.

## Migration File

`supabase/migrations/2025-08-26_admin_logs.sql`

## What it creates

1. **Table**: `public.admin_logs` with columns:
   - `id` (uuid, primary key, auto-generated)
   - `timestamp` (timestamptz, default now())
   - `admin` (text, required)
   - `action` (text, required) 
   - `details` (text, required)

2. **Indexes** for performance:
   - `idx_admin_logs_timestamp` - for date range filtering
   - `idx_admin_logs_admin` - for admin filtering
   - `idx_admin_logs_action` - for action filtering
   - `idx_admin_logs_details_trgm` - for full-text search in details

3. **Security**: Row Level Security (RLS) enabled

## Backend environment requirements

The backend uses the Supabase Service Role key to bypass RLS for server-side inserts into `admin_logs`.

Set these variables in your backend environment (e.g., Render):

- `SUPABASE_URL` = `https://<your-project>.supabase.co`
- `SUPABASE_SERVICE_KEY` = `<your-service-role-key>`
- (Optional) `SUPABASE_KEY` = anon key, used only as a fallback if `SUPABASE_SERVICE_KEY` is not set (not recommended for production)

If the Service Role key is not configured, attempts to insert into `admin_logs` will fail with 403: `new row violates row-level security policy`.

## API Endpoints

### GET `/api/logs`

Admin-only. Requires `Authorization: Bearer <JWT>`.

Query params:
- `page` (default: 1)
- `limit` (default: 50, max: 200)
- `page_size` (alias for `limit`, kept for backward compatibility)
- `sort` (default: `-timestamp` for DESC; `timestamp` for ASC)

Response:
```json
{
  "data": [
    { "id": "...", "timestamp": "...", "admin": "...", "action": "...", "details": "..." }
  ],
  "pagination": { "page": 1, "per_page": 50, "total": 123 }
}
```

### GET `/api/logs/health`

Admin-only health check. Returns:
```json
{ "status": "ok" }
```

## How to apply the migration

### Option 1: Supabase Dashboard
1. Go to your Supabase project dashboard
2. Navigate to SQL Editor
3. Copy and paste the contents of `supabase/migrations/2025-08-26_admin_logs.sql`
4. Run the migration

### Option 2: Supabase CLI
```bash
supabase db push
```

### Option 3: Direct PostgreSQL
```bash
psql $DATABASE_URL -f supabase/migrations/2025-08-26_admin_logs.sql
```

## Validation

After applying the migration and configuring environment variables, test the endpoints:

```bash
# Health (must include a valid admin JWT)
curl -H "Authorization: Bearer $ADMIN_TOKEN" -I https://your-app.com/api/logs/health

# List logs (must include a valid admin JWT)
curl -H "Authorization: Bearer $ADMIN_TOKEN" "https://your-app.com/api/logs?limit=50&page=1&sort=-timestamp"
```

Both should return 200 when properly configured.