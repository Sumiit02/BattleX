# Cashfree API v2023-08-01 Integration Guide

## Overview
Your BattleX application uses Cashfree for payment processing and wallet payouts. This document captures the integration details for **v2023-08-01**.

---

## Current Integration Points

### 1. **Payment Gateway** (PG API)
- **Endpoint**: `POST /orders`
- **Usage**: Create payment orders for event registrations
- **Response Handling**: [app.py](../app.py#L3264)
  - Extracts: `payment_session_id`, `payment_link`, `order_amount`, `order_currency`
  - Stores in database and sends to frontend

### 2. **Order Verification** (PG API)  
- **Endpoint**: `GET /orders/{order_id}`
- **Usage**: Verify payment status before crediting wallet
- **Response Handling**: [app.py](../app.py#L1128)
  - Checks: `order_status` == 'PAID'

### 3. **Webhooks** (PG API)
- **Endpoint**: `POST /api/cashfree/webhook`
- **Usage**: Receive payment completion notifications
- **Response Handling**: [app.py](../app.py#L3364)
  - Validates signature
  - Extracts `order_id` from `data.order.order_id`
  - Checks `order_status` for payment completion

### 4. **Wallet Deposits** (PG API)
- **Endpoint**: Same as payment orders
- **Usage**: Top-up wallet balance
- **Response Handling**: Similar to payment processing

### 5. **Payouts** (Separate API)
- **Endpoint**: `POST /payout/v1/requestTransfer`
- **Usage**: Withdraw funds to bank/UPI
- **Response Handling**: [app.py](../app.py#L900)
  - Extracts: `transfer_id`, `transfer_status`, `reference_id`
  - Note: Uses `/payout/v1` endpoint (different from PG API)

---

## Compatibility Notes for v2023-08-01

### ✅ LIKELY PRESERVED
- `order_id` field and format
- `order_amount` field (in INR)
- `order_currency` (INR)
- `customer_details` structure
- `payment_session_id` (critical for SDK)
- Webhook signature validation method
- Status PAID/FAILED values

### ⚠️ POTENTIALLY CHANGED

#### 1. Order Status Values
```python
# Current code expects (line 1024):
order_status = str(payload.get('order_status') or '').upper()
if order_status != 'PAID':
    # Not yet paid

# Changes to watch for:
# - Status naming convention (e.g., "ACTIVE" → "INITIATED")
# - New status values: "PROCESSING", "PENDING", "HELD"
# - Case sensitivity (already handled by .upper())
```

#### 2. Response Field Renaming
```python
# Current extraction method (line 1208):
payment_session_id = order_data.get('payment_session_id')
payment_link = order_data.get('payment_link')

# Possible v2023-08-01 changes:
# - payment_session_id → checkout_session_id
# - payment_link → checkout_url
# - payment_link → order_meta.payment_link
```

#### 3. Nested Response Structure
```python
# Current handling (line 1208):
return_url = (order_data.get('order_meta') or {}).get('return_url')

# Possible changes in v2023-08-01:
# - Flattened structure
# - Renamed nesting levels
# - New metadata container
```

#### 4. Error Response Format
```python
# Current handling (line 1138):
payload.get('message') or 'Unable to verify payment status'

# Potential changes:
# - error_description instead of message
# - error_code addition/changes
# - error object structure
```

#### 5. Webhook Payload Changes
```python
# Current extraction (line 1150):
order_obj = data.get('order') or {}
order_id = order_obj.get('order_id')

# Possible v2023-08-01 changes:
# - Flattened payload: data.order_id instead of data.order.order_id
# - New webhook event types
# - Different signature header names
```

---

## Action Items

### 🔴 CRITICAL - Test These Immediately
1. **Payment Session ID Extraction**
   - [ ] Verify `payment_session_id` is present in response
   - [ ] Test Cashfree SDK initialization with new session ID
   - [ ] Check payment flow completion

2. **Order Status Verification**
   - [ ] Confirm `order_status` field exists
   - [ ] Test `order_status == 'PAID'` condition
   - [ ] Test status for failed payments

3. **Webhook Payload Structure**
   - [ ] Verify webhook contains order_id
   - [ ] Test signature validation
   - [ ] Confirm payment completion updates database

### 🟡 IMPORTANT - Review These
4. **Response Error Handling**
   - [ ] Test error message extraction
   - [ ] Check HTTP status codes
   - [ ] Validate error scenarios

5. **Field Name Changes**
   - [ ] Search codebase for hardcoded field names
   - [ ] Review field access with `.get()` (already safe)
   - [ ] Check test data matches real responses

### 🟢 NICE-TO-HAVE - Monitor These
6. **Payout API Compatibility**
   - [ ] Test transfer initiation
   - [ ] Verify transfer status checks
   - [ ] Check webhook payload structure

---

## Code Locations That Need Review

### Response Parsing Functions
| Function | Location | Critical Fields |
|----------|----------|-----------------|
| `_cashfree_order_to_api_response` | [app.py#L1198](../app.py#L1198) | `payment_session_id`, `payment_link` |
| `_cashfree_fetch_order` | [app.py#L1128](../app.py#L1128) | `order_status` |
| `_extract_cashfree_order_id` | [app.py#L1150](../app.py#L1150) | Order ID nesting |
| `cashfree_webhook` | [app.py#L3364](../app.py#L3364) | `order_status`, payload structure |
| `_cashfree_create_payout_transfer` | [app.py#L900](../app.py#L900) | `transfer_id`, `status` |

### Frontend Integration Points
| File | Location | Usage |
|------|----------|-------|
| `payment_page.html` | Line 143 | SDK initialization with `payment_session_id` |
| `payment_page.html` | Line 158 | Fallback to `payment_link` |

---

## Testing Strategy

### Phase 1: Sandbox Testing (Recommended First)
1. **Set to Sandbox Mode**
   ```bash
   CASHFREE_ENV=sandbox
   ```

2. **Run Test Suite**
   ```bash
   python tools/test_cashfree_v2023_integration.py
   ```

3. **Manual Payment Flow Test**
   - Create event registration
   - Initiate payment
   - Complete payment in Cashfree UI
   - Verify webhook receipt
   - Check database update

4. **Verify Responses**
   - Print actual API responses to logs
   - Compare with expected structure
   - Document any missing fields

### Phase 2: Staging/QA Testing
1. Test full registration → payment → completion flow
2. Test error scenarios (failed payments, timeouts)
3. Test wallet deposits and withdrawals
4. Monitor logs for any parsing errors

### Phase 3: Production Rollout
1. Deploy with v2023-08-01 configuration
2. Monitor payment success rate
3. Watch logs for response parsing issues
4. Have rollback plan ready

---

## Common Response Examples

### Payment Order Creation Response (v2023-08-01)
```json
{
  "cf_payment_id": 12345,
  "created_at": "2025-01-15T10:30:00Z",
  "customer_details": {
    "customer_email": "player@example.com",
    "customer_id": "player_1",
    "customer_name": "Player One",
    "customer_phone": "9876543210"
  },
  "order_amount": 100.00,
  "order_currency": "INR",
  "order_id": "BXR123456",
  "order_meta": {
    "return_url": "https://battlex.co.in/payment/callback"
  },
  "order_note": "BattleX registration",
  "order_status": "ACTIVE",
  "payment_link": "https://checkout.cashfree.com/pay/...",
  "payment_session_id": "session_abcd1234efgh5678"
}
```

### Order Status Verification Response (v2023-08-01)
```json
{
  "cf_payment_id": 12345,
  "created_at": "2025-01-15T10:30:00Z",
  "order_amount": 100.00,
  "order_currency": "INR",
  "order_id": "BXR123456",
  "order_status": "PAID",
  "payment_method": "upi",
  "payment_amount": 100.00,
  "payment_time": "2025-01-15T10:31:00Z",
  "settlement_id": "SETTLEMENT123"
}
```

### Webhook Payload (v2023-08-01)
```json
{
  "type": "PAYMENT_SUCCESS",
  "event_id": "evt_123456",
  "event_time": "2025-01-15T10:31:00Z",
  "data": {
    "order": {
      "order_id": "BXR123456",
      "order_amount": 100.00,
      "order_currency": "INR",
      "order_status": "PAID"
    },
    "payment": {
      "cf_payment_id": 12345,
      "payment_method": "upi",
      "payment_amount": 100.00
    }
  }
}
```

---

## Debugging Tips

### Enable Debug Logging
```python
# Add to app.py temporarily
import logging
logging.basicConfig(level=logging.DEBUG)

# Before API calls:
print(f"Request Headers: {_cashfree_headers()}")
print(f"Request Payload: {json.dumps(order_payload, indent=2)}")

# After API calls:
print(f"Response Status: {resp.status_code}")
print(f"Response Body: {json.dumps(resp.json(), indent=2)}")
```

### Test Response Parsing Directly
```python
from app import _cashfree_order_to_api_response

sample_response = {
    "order_id": "BXR123456",
    "order_amount": 100.00,
    "payment_session_id": "session_xyz789",
    # ... add actual response from API
}

result = _cashfree_order_to_api_response(
    order_id="BXR123456",
    order_data=sample_response
)

print(json.dumps(result, indent=2))
```

---

## Rollback Plan

If issues occur after upgrade:
1. **Revert API Version**
   ```bash
   CASHFREE_API_VERSION=2023-08-01
   ```
2. **Verify Payments Still Work**
3. **Contact Cashfree Support** for v2023-08-01 migration assistance
4. **Post-Analysis**: Review actual vs expected response formats

---

## References
- Cashfree v2023-08-01 API Documentation: https://docs.cashfree.com/
- Payment Gateway API: https://docs.cashfree.com/api-reference/payments/
- Payouts API: https://docs.cashfree.com/api-reference/payouts/
- Webhook Documentation: https://docs.cashfree.com/docs/webhooks/

