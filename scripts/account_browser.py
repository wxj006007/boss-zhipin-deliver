"""Compatibility imports; browser identity checks live in browser_session."""
from scripts.browser_session import (
    IDENTITY_JS, request, inspect_identity, verify_identity, guarded_script, bridge_status,
)
