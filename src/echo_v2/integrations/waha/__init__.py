"""WAHA GOWS integration adapter (Phase 3: session management + QR onboarding).

WAHA is a **receive-only** connector: Echo provisions WAHA sessions and
consumes their lifecycle events, but never sends WhatsApp messages through
WAHA. Message delivery stays exclusively with the Phase 1/2 batch agent.
"""
