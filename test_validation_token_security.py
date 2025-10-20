#!/usr/bin/env python
"""
Quick test script to verify the validation token security fix.
"""

import re
import sys


def test_validation_token_patterns():
    """Test that our validation token pattern works correctly."""

    # Valid Microsoft validation tokens (UUIDs)
    valid_tokens = [
        "123e4567-e89b-12d3-a456-426614174000",
        "550e8400-e29b-41d4-a716-446655440000",
        "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
        "6ba7b811-9dad-11d1-80b4-00c04fd430c8",
        "01234567-89ab-cdef-0123-456789abcdef",
    ]

    # Invalid/malicious tokens (potential XSS payloads)
    invalid_tokens = [
        "<script>alert('xss')</script>",
        "javascript:alert(1)",
        "'><script>alert(document.cookie)</script>",
        "onmouseover=alert(1)",
        "123e4567-e89b-12d3-a456-42661417400g",  # Invalid character
        "123e4567-e89b-12d3-a456",  # Too short
        "123e4567-e89b-12d3-a456-426614174000-extra",  # Too long
        "",  # Empty
        "notauuid",  # Clearly not a UUID
        "123e4567e89b12d3a456426614174000",  # Missing hyphens
    ]

    uuid_pattern = r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$"

    print("Testing valid tokens:")
    for token in valid_tokens:
        if re.match(uuid_pattern, token, re.IGNORECASE):
            print(f"✅ {token} - VALID")
        else:
            print(f"❌ {token} - INVALID (should be valid)")
            return False

    print("\nTesting invalid/malicious tokens:")
    for token in invalid_tokens:
        if re.match(uuid_pattern, token, re.IGNORECASE):
            print(f"❌ {token} - VALID (should be invalid)")
            return False
        else:
            print(f"✅ {token} - INVALID")

    return True


if __name__ == "__main__":
    if test_validation_token_patterns():
        print("\n🎉 All validation token security tests passed!")
        sys.exit(0)
    else:
        print("\n💥 Some validation token security tests failed!")
        sys.exit(1)
