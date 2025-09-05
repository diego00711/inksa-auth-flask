# CORS Preflight and Delivery Authentication Tests

## Overview
This document provides manual verification steps for testing CORS preflight requests and delivery endpoint authentication after implementing the fixes.

## Test Environment Setup

1. **Start the Flask server:**
   ```bash
   cd /path/to/inksa-auth-flask
   python src/main.py
   ```
   The server should start on `http://127.0.0.1:5000`

## CORS Preflight Tests

### Test 1: Basic CORS Preflight
Test that OPTIONS requests are allowed without authentication:

```bash
curl -X OPTIONS http://127.0.0.1:5000/api/delivery/orders/orders/pending \
  -H "Origin: https://entregadores.inksadelivery.com.br" \
  -H "Access-Control-Request-Method: GET" \
  -H "Access-Control-Request-Headers: Authorization" \
  -v
```

**Expected Result:**
- Status: `200 OK`
- Headers should include:
  - `Access-Control-Allow-Origin: https://entregadores.inksadelivery.com.br`
  - `Access-Control-Allow-Headers: Content-Type,Authorization,Access-Control-Allow-Credentials`
  - `Access-Control-Allow-Methods: GET,POST,PUT,DELETE,OPTIONS,PATCH`
  - `Access-Control-Allow-Credentials: true`

### Test 2: Automated CORS Tests
Run the comprehensive test script:

```bash
python /tmp/test_cors_preflight.py
```

**Expected Result:** All endpoints should return `✅ CORS preflight successful`

### Test 3: Cross-Origin Request Simulation
Test with different origins:

```bash
# Test with localhost (should work)
curl -X OPTIONS http://127.0.0.1:5000/api/delivery/profile \
  -H "Origin: http://localhost:3000" \
  -H "Access-Control-Request-Method: POST" \
  -H "Access-Control-Request-Headers: Content-Type,Authorization"

# Test with production origin (should work)
curl -X OPTIONS http://127.0.0.1:5000/api/delivery/stats/dashboard-stats \
  -H "Origin: https://entregadores.inksadelivery.com.br" \
  -H "Access-Control-Request-Method: GET" \
  -H "Access-Control-Request-Headers: Authorization"
```

## Authentication Tests

### Test 4: Unauthenticated Requests
Verify that actual API requests (non-OPTIONS) require authentication:

```bash
# Should return authentication error
curl -X GET http://127.0.0.1:5000/api/delivery/orders/orders/pending

# Should return authentication error  
curl -X GET http://127.0.0.1:5000/api/delivery/stats/dashboard-stats

# Should return authentication error
curl -X GET http://127.0.0.1:5000/api/delivery/profile
```

**Expected Result:** All should return `401` status with error message about missing authorization token.

### Test 5: Invalid Token
Test with invalid Bearer token:

```bash
curl -X GET http://127.0.0.1:5000/api/delivery/orders/orders/pending \
  -H "Authorization: Bearer invalid_token_here"
```

**Expected Result:** Should return `401` status with "Token inválido ou expirado" error.

### Test 6: Valid Delivery Token (Manual)
To test with a valid token, you would need to:

1. Create a delivery user account through the authentication system
2. Get a valid JWT token from login
3. Test the endpoint:

```bash
curl -X GET http://127.0.0.1:5000/api/delivery/orders/orders/pending \
  -H "Authorization: Bearer YOUR_VALID_DELIVERY_TOKEN_HERE" \
  -H "Content-Type: application/json"
```

**Expected Result:** Should return successful response with delivery data (or appropriate business logic response).

### Test 7: Non-Delivery User Token
Test with a token from a non-delivery user (e.g., client or restaurant):

```bash
curl -X GET http://127.0.0.1:5000/api/delivery/orders/orders/pending \
  -H "Authorization: Bearer NON_DELIVERY_USER_TOKEN" \
  -H "Content-Type: application/json"
```

**Expected Result:** Should return `403` status with "Acesso negado. Apenas usuários do tipo delivery podem acessar esta rota."

## Key Endpoints to Test

All these endpoints should support CORS preflight and require delivery authentication:

- `GET /api/delivery/orders/orders/pending` - Get pending delivery orders
- `GET /api/delivery/orders/orders` - Get delivery person's orders  
- `POST /api/delivery/orders/orders/{id}/accept` - Accept a delivery order
- `POST /api/delivery/orders/orders/{id}/complete` - Complete a delivery
- `GET /api/delivery/stats/dashboard-stats` - Get delivery dashboard stats
- `GET /api/delivery/stats/earnings-history` - Get earnings history
- `GET /api/delivery/profile` - Get delivery profile
- `POST /api/delivery/profile` - Update delivery profile

## Verification Checklist

- [ ] OPTIONS requests work without authentication for all delivery endpoints
- [ ] CORS headers are properly set for allowed origins
- [ ] GET/POST requests without tokens are rejected with 401
- [ ] GET/POST requests with invalid tokens are rejected with 401  
- [ ] GET/POST requests with non-delivery tokens are rejected with 403
- [ ] GET/POST requests with valid delivery tokens work correctly
- [ ] All endpoints under `/api/delivery/*` follow the same pattern

## Implementation Notes

The fixes implemented include:

1. **Consolidated `delivery_token_required` decorators** - Removed duplicates and ensured all use proper OPTIONS handling
2. **Enhanced CORS configuration** - Added comprehensive headers and methods support
3. **Global OPTIONS handler** - Prevents authentication from blocking preflight requests
4. **Proper user_type extraction** - Gets user type from JWT token metadata
5. **Consistent error responses** - Standardized error messages across endpoints

All changes maintain backward compatibility while fixing the CORS preflight issues.