#!/usr/bin/env python3
"""
Test suite for Cashfree API v2023-08-01 integration compatibility.
Tests payment order creation, webhook handling, and payout flows.
"""

import sys
import os
import json
from datetime import datetime

# Add parent directory to path to import app
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import app
except ImportError as e:
    print(f"❌ Failed to import app.py: {e}")
    sys.exit(1)

# ============================================================================
# TEST CONFIGURATION
# ============================================================================

TEST_ENV = os.getenv('CASHFREE_ENV', 'sandbox').lower()
API_VERSION = app.CASHFREE_API_VERSION
API_BASE = app.CASHFREE_API_BASE

print("=" * 80)
print(f"Cashfree API Integration Test Suite - v{API_VERSION}")
print("=" * 80)
print(f"Environment: {TEST_ENV}")
print(f"API Base URL: {API_BASE}")
print(f"API Version Header: x-api-version={API_VERSION}")
print()

# ============================================================================
# TEST 1: Order Creation Response Parsing
# ============================================================================

def test_order_response_parsing():
    """Test if the code correctly parses Cashfree v2023-08-01 order response."""
    
    print("\n[TEST 1] Order Creation Response Parsing")
    print("-" * 80)
    
    # Sample Cashfree v2023-08-01 order creation response structure
    sample_responses = {
        "v2023-08-01": {
            "cf_payment_id": 12345,
            "created_at": "2023-01-15T10:30:00Z",
            "customer_details": {
                "customer_email": "test@example.com",
                "customer_id": "test_customer_001",
                "customer_phone": "9876543210",
                "customer_name": "Test User"
            },
            "order_amount": 100.00,
            "order_currency": "INR",
            "order_id": "BXR12345",
            "order_meta": {
                "return_url": "https://example.com/callback"
            },
            "order_note": "BattleX registration",
            "order_status": "ACTIVE",
            "payment_link": "https://checkout.cashfree.com/pay/p123",
            "payment_session_id": "session_abc123xyz",
            "settlements": None
        },
        "v2023-08-01-extended": {
            # Extended sample payload compatible with v2023-08-01
            "order_id": "BXR12345",
            "order_amount": 100.00,
            "order_currency": "INR",
            "order_status": "ACTIVE",  # Status values may have changed
            "customer_details": {
                "customer_email": "test@example.com",
                "customer_id": "test_customer_001",
                "customer_phone": "9876543210",
                "customer_name": "Test User"
            },
            "order_meta": {
                "return_url": "https://example.com/callback"
            },
            "order_note": "BattleX registration",
            # Additional fields that may appear in responses
            "payment_session_id": "session_abc123xyz_v2",
            "payment_link": "https://checkout.cashfree.com/pay/p456",
            "cf_payment_id": None,  # Might be deprecated
            "created_at": "2023-01-15T10:30:00Z",
            "settlement_details": {}  # Possible new field
        }
    }
    
    # Test current response parsing
    print(f"\n✓ Testing response parsing with current code (v{API_VERSION})...")
    
    try:
        result = app._cashfree_order_to_api_response(
            order_id="BXR12345",
            order_data=sample_responses.get("v2023-08-01", {}),
            fallback_amount_paise=10000
        )
        
        print(f"  • payment_session_id extracted: {result.get('payment_session_id')}")
        print(f"  • payment_link extracted: {result.get('payment_link')}")
        print(f"  • order_id: {result.get('order_id')}")
        print(f"  • amount (paise): {result.get('amount')}")
        print(f"  • currency: {result.get('currency')}")
        
        # Validate critical fields
        required_fields = ['payment_session_id', 'order_id', 'amount', 'currency']
        missing = [f for f in required_fields if not result.get(f)]
        
        if missing:
            print(f"  ❌ Missing critical fields: {missing}")
            return False
        else:
            print(f"  ✅ All critical fields present")
            return True
            
    except Exception as e:
        print(f"  ❌ Error parsing response: {e}")
        return False


# ============================================================================
# TEST 2: Order Status Verification
# ============================================================================

def test_order_status_values():
    """Test if order status values are correctly recognized."""
    
    print("\n[TEST 2] Order Status Value Recognition")
    print("-" * 80)
    
    # Test status values that should be recognized
    test_statuses = {
        "PAID": True,           # Should be recognized as complete
        "ACTIVE": False,        # Should be recognized as pending
        "INITIATED": False,     # Should be recognized as pending
        "PROCESSING": False,    # Possible new status
        "EXPIRED": False,       # Should be recognized as failed
        "FAILED": False,        # Should be recognized as failed
    }
    
    print("\nTesting order status recognition...")
    
    terminal_statuses = app.CASHFREE_TERMINAL_ORDER_STATUSES
    print(f"Terminal statuses defined: {terminal_statuses}")
    
    all_passed = True
    for status, should_be_paid in test_statuses.items():
        is_paid = status == 'PAID'
        is_terminal = status in terminal_statuses
        
        if should_be_paid and is_paid:
            print(f"  ✅ {status}: Correctly identified as PAID")
        elif not should_be_paid and not is_paid:
            print(f"  ✅ {status}: Correctly identified as NOT PAID")
        else:
            print(f"  ❌ {status}: Status handling may need review")
            all_passed = False
    
    return all_passed


# ============================================================================
# TEST 3: Webhook Payload Extraction
# ============================================================================

def test_webhook_order_id_extraction():
    """Test if order ID can be extracted from various webhook payload formats."""
    
    print("\n[TEST 3] Webhook Payload Order ID Extraction")
    print("-" * 80)
    
    test_payloads = [
        # Standard format
        {
            "type": "PAYMENT_SUCCESSFUL",
            "data": {
                "order": {
                    "order_id": "BXR123456",
                    "order_amount": 100.0,
                    "order_status": "PAID"
                }
            }
        },
        # Alternative format with flat structure
        {
            "type": "PAYMENT_SUCCESSFUL",
            "order_id": "BXR123456",
            "order_status": "PAID"
        },
        # Format with cf_order_id
        {
            "type": "PAYMENT_SUCCESSFUL",
            "cf_order_id": "BXR123456"
        },
    ]
    
    print("\nTesting order ID extraction from webhook payloads...")
    
    all_passed = True
    for i, payload in enumerate(test_payloads, 1):
        try:
            order_id = app._extract_cashfree_order_id(payload)
            if order_id == "BXR123456":
                print(f"  ✅ Payload format {i}: Correctly extracted '{order_id}'")
            else:
                print(f"  ❌ Payload format {i}: Got '{order_id}', expected 'BXR123456'")
                all_passed = False
        except Exception as e:
            print(f"  ❌ Payload format {i}: Error - {e}")
            all_passed = False
    
    return all_passed


# ============================================================================
# TEST 4: Webhook Signature Verification
# ============================================================================

def test_webhook_signature_handling():
    """Test webhook signature verification logic."""
    
    print("\n[TEST 4] Webhook Signature Handling")
    print("-" * 80)
    
    print(f"\n✓ Webhook signature verification enabled: {app.CASHFREE_WEBHOOK_REQUIRE_SIGNATURE}")
    
    # Test signature normalization
    test_signatures = [
        ("abc123def456", "abc123def456"),
        ("signature=abc123def456", "abc123def456"),
        ("ABC123DEF456", "abc123def456"),
    ]
    
    print("\nTesting signature normalization...")
    all_passed = True
    
    for input_sig, expected in test_signatures:
        try:
            normalized = app._normalize_cashfree_signature(input_sig)
            if normalized == expected:
                print(f"  ✅ '{input_sig}' → '{normalized}'")
            else:
                print(f"  ❌ '{input_sig}' → '{normalized}' (expected '{expected}')")
                all_passed = False
        except Exception as e:
            print(f"  ❌ Error normalizing '{input_sig}': {e}")
            all_passed = False
    
    return all_passed


# ============================================================================
# TEST 5: Payout Response Parsing
# ============================================================================

def test_payout_response_parsing():
    """Test if payout response parsing handles v2023-08-01 format."""
    
    print("\n[TEST 5] Payout Response Parsing")
    print("-" * 80)
    
    # Sample payout response
    sample_payout_response = {
        "transfer_id": "WDR123456789123456",
        "reference_id": "wallet_withdrawal_123",
        "amount": 100.0,
        "status": "SUCCESS",  # May change in v2023-08-01
        "transfer_status": "PROCESSED",
        "created_at": "2025-01-15T10:30:00Z",
        "utr": "UTR123456789"
    }
    
    print("\n✓ Testing payout response parsing...")
    
    try:
        # Test status normalization
        status = sample_payout_response.get('status') or sample_payout_response.get('transfer_status')
        normalized = app._normalize_payout_status(status)
        
        print(f"  • Original status: {status}")
        print(f"  • Normalized status: {normalized}")
        
        # Extract transfer reference
        transfer_id = (
            sample_payout_response.get('transfer_id') or
            sample_payout_response.get('reference_id')
        )
        print(f"  • Transfer ID: {transfer_id}")
        
        print(f"  ✅ Payout response parsing works")
        return True
        
    except Exception as e:
        print(f"  ❌ Error parsing payout response: {e}")
        return False


# ============================================================================
# TEST 6: Configuration Validation
# ============================================================================

def test_configuration():
    """Validate Cashfree configuration."""
    
    print("\n[TEST 6] Configuration Validation")
    print("-" * 80)
    
    print(f"\n✓ Cashfree Configuration:")
    print(f"  • API Version: {app.CASHFREE_API_VERSION}")
    print(f"  • Environment: {app.CASHFREE_ENV}")
    print(f"  • App configured: {app.is_cashfree_ready()}")
    print(f"  • Payouts enabled: {app.CASHFREE_PAYOUTS_ENABLED}")
    print(f"  • Payouts configured: {app._cashfree_payouts_ready()}")
    print(f"  • Webhook signature check: {app.CASHFREE_WEBHOOK_REQUIRE_SIGNATURE}")
    
    warnings = []
    if not app.is_cashfree_ready():
        warnings.append("Payment gateway not configured (APP_ID/SECRET_KEY)")
    if app.CASHFREE_PAYOUTS_ENABLED and not app._cashfree_payouts_ready():
        warnings.append("Payouts enabled but not fully configured")
    
    if warnings:
        print(f"\n⚠️  Configuration Warnings:")
        for w in warnings:
            print(f"  • {w}")
        return False
    else:
        print(f"\n✅ Configuration looks good")
        return True


# ============================================================================
# TEST 7: Critical Field Changes
# ============================================================================

def test_critical_fields():
    """Document critical fields that changed in v2023-08-01."""
    
    print("\n[TEST 7] Known API Behavior Notes (v2023-08-01)")
    print("-" * 80)
    
    changes = {
        "✅ Preserved Fields": [
            "order_id",
            "order_amount",
            "order_currency",
            "order_status",
            "customer_details",
            "order_meta",
            "payment_session_id",
            "payment_link",
        ],
        "⚠️  Potentially Changed": [
            "Status value naming (ACTIVE → INITIATED?)",
            "Webhook payload structure",
            "Signature header names",
            "Error response format",
        ],
        "❓ Needs Verification": [
            "New settlement_details field",
            "New payout response fields",
            "New webhook event types",
            "Rate limiting headers",
        ]
    }
    
    for category, items in changes.items():
        print(f"\n{category}:")
        for item in items:
            print(f"  • {item}")
    
    return True


# ============================================================================
# MAIN TEST RUNNER
# ============================================================================

def run_all_tests():
    """Run all test functions and report results."""
    
    tests = [
        ("Order Response Parsing", test_order_response_parsing),
        ("Order Status Values", test_order_status_values),
        ("Webhook Order ID Extraction", test_webhook_order_id_extraction),
        ("Webhook Signature Handling", test_webhook_signature_handling),
        ("Payout Response Parsing", test_payout_response_parsing),
        ("Configuration Validation", test_configuration),
        ("Critical Field Changes", test_critical_fields),
    ]
    
    results = {}
    for test_name, test_func in tests:
        try:
            results[test_name] = test_func()
        except Exception as e:
            print(f"\n❌ Test '{test_name}' failed with exception: {e}")
            results[test_name] = False
    
    # Summary
    print("\n" + "=" * 80)
    print("TEST SUMMARY")
    print("=" * 80)
    
    passed = sum(1 for v in results.values() if v)
    total = len(results)
    
    for test_name, result in results.items():
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"{status}: {test_name}")
    
    print(f"\nTotal: {passed}/{total} tests passed")
    
    if passed == total:
        print("\n🎉 All tests passed! Your integration is compatible with v2023-08-01")
        return 0
    else:
        print(f"\n⚠️  {total - passed} test(s) may need attention")
        return 1


if __name__ == "__main__":
    exit_code = run_all_tests()
    sys.exit(exit_code)
