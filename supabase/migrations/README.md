# Admin Logs Migration

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

## How to apply

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

After applying the migration, test the endpoints:

```bash
# Test HEAD request
curl -I https://your-app.com/api/logs

# Test GET request (requires proper authentication)
curl -H "Authorization: Bearer YOUR_TOKEN" https://your-app.com/api/logs
```

Both should return 200 instead of 500 errors.

## Notes

- The backend uses the Service Role key which bypasses RLS
- Do NOT expose the Service Role key in frontend environments
- The table supports all filtering and pagination features used by the admin_logs route