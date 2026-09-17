"""LangSmith tracing client for sanitized @traceable methods.

The default LangSmith client has ``LANGSMITH_HIDE_INPUTS=true`` and
``LANGSMITH_HIDE_OUTPUTS=true`` (set in ``app/main.py``) to prevent raw LLM
prompts/completions — which may contain message text, phone numbers, or other
PII — from being sent by ``wrap_openai``.

Those global HIDE flags also strip the *sanitized* inputs/outputs produced by
``@traceable(process_inputs=..., process_outputs=...)`` sanitizers, leaving
service-level traces (``wfm.analysis``, ``wfm.llm_analyze``, etc.) with empty
inputs/outputs in the LangSmith UI.

This module provides a separate client with HIDE **disabled**. Pass it to
``@traceable`` decorators that already have ``process_inputs``/``process_outputs``
sanitizers — the sanitizers are the privacy mechanism for these runs, so HIDE
is redundant and harmful.

Usage::

    from echo_v2.observability.tracing import tracing_client

    @traceable(
        name="wfm.analysis",
        client=tracing_client,
        process_inputs=safe_process_chat_inputs,
        process_outputs=safe_process_chat_output,
    )
    async def _process_chat(self, chat): ...

The default client (with HIDE) is still used by ``wrap_openai`` for LLM calls,
so raw prompts/completions are never sent.
"""

from __future__ import annotations

from langsmith import Client

__all__ = ["tracing_client"]

# Created at import time. The Client constructor reads LANGSMITH_API_KEY,
# LANGSMITH_ENDPOINT, LANGSMITH_PROJECT, etc. from the environment — same as
# the default client. The only difference: hide_inputs/hide_outputs disabled.
# If LANGSMITH_TRACING is false, @traceable is a no-op and this client is
# never used to send runs.
tracing_client = Client(hide_inputs=False, hide_outputs=False)
