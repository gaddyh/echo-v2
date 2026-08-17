"""Tests for the webhook deduplication store."""

from __future__ import annotations

from echo_v2.app.webhooks.dedup import (
    InMemoryWebhookDedupStore,
    WebhookDedupStore,
)


def test_in_memory_store_satisfies_protocol():
    assert isinstance(InMemoryWebhookDedupStore(), WebhookDedupStore)


async def test_claim_returns_true_for_first_claim():
    store = InMemoryWebhookDedupStore()
    assert await store.claim("k1") is True


async def test_claim_returns_false_for_duplicate_claim():
    store = InMemoryWebhookDedupStore()
    assert await store.claim("k1") is True
    assert await store.claim("k1") is False
    assert await store.claim("k1") is False


async def test_claim_distinguishes_different_keys():
    store = InMemoryWebhookDedupStore()
    assert await store.claim("k1") is True
    assert await store.claim("k2") is True
    assert await store.claim("k1") is False
    assert await store.claim("k2") is False


async def test_claim_is_independent_per_store_instance():
    s1 = InMemoryWebhookDedupStore()
    s2 = InMemoryWebhookDedupStore()
    assert await s1.claim("k") is True
    assert await s2.claim("k") is True  # different store, fresh claim
