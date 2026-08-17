"""Persistence layer for Echo v2.

Currently contains the in-memory WhatsApp connection repository used for Step 0
and tests. A persistent (Postgres/Redis) implementation is mandatory before
real-user onboarding (plan guardrail G2: if Green succeeds but persistence
fails, retry persistence, not ``create_connection``).
"""
