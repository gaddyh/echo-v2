"""Persistence layer for Echo v2.

Contains:
* The in-memory WhatsApp connection repository used for Step 0 and tests.
* Postgres-backed implementations of the three repository protocols
  (:class:`WhatsAppConnectionRepository`, :class:`WebhookDedupStore`,
  :class:`IdempotencyStore`) plus a ``users`` anchor table — the Foundation
  DB milestone. See ``postgres_whatsapp_connections``,
  ``postgres_webhook_dedup``, ``postgres_idempotency``, ``unit_of_work``.
* A :class:`CredentialCipher` boundary so ``credentials`` BYTEA stores
  encrypted bytes from day one.
* Alembic migrations under ``alembic/``.

A persistent (Postgres) implementation is mandatory before real-user
onboarding (plan guardrail G2: if Green succeeds but persistence fails,
retry persistence, not ``create_connection``).
"""
