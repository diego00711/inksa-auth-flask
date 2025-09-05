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

- """add daily_goal to delivery_profiles

Revision ID: xxxx_add_daily_goal
Revises: <prev_revision>
Create Date: 2025-09-05 14:10:00.000000
"""
from alembic import op
import sqlalchemy as sa

# revise these values
revision = "xxxx_add_daily_goal"
down_revision = "<prev_revision>"
branch_labels = None
depends_on = None

def upgrade():
    op.add_column(
        "delivery_profiles",
        sa.Column("daily_goal", sa.Integer(), nullable=False, server_default="0"),
    )
    # remove server_default after applying if you prefer
    op.alter_column("delivery_profiles", "daily_goal", server_default=None)

def downgrade():
    op.drop_column("delivery_profiles", "daily_goal")
