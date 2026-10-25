"""PricingService unit tests — cost estimation and cost-source classification."""

from __future__ import annotations

from decimal import Decimal

from echo_v2.domain.ai_usage_event import AiCostSource
from echo_v2.services.reporting.pricing_service import PricingService


def test_version_is_loaded() -> None:
    service = PricingService()
    assert service.version() != ""


def test_token_cost_measured_usage_priced() -> None:
    """OpenAI tokens with real usage → measured_usage_priced."""
    service = PricingService()
    cost, source = service.estimate_token_cost(
        provider="openai",
        model="gpt-5.4-mini",
        prompt_tokens=1000,
        completion_tokens=200,
    )
    assert source == AiCostSource.MEASURED_USAGE_PRICED
    assert cost is not None
    # 1000 * 0.75 / 1M + 200 * 4.50 / 1M
    expected = Decimal("0.00075") + Decimal("0.0009")
    assert cost == expected


def test_token_cost_fallback_when_no_usage() -> None:
    """No token counts → fallback per-call assumptions → fallback_estimate."""
    service = PricingService()
    cost, source = service.estimate_token_cost(
        provider="openai",
        model="gpt-5.4-mini",
        prompt_tokens=None,
        completion_tokens=None,
    )
    assert source == AiCostSource.FALLBACK_ESTIMATE
    assert cost is not None
    # 4000 * 0.75 / 1M + 150 * 4.50 / 1M
    expected = Decimal("0.003") + Decimal("0.000675")
    assert cost == expected


def test_token_cost_unknown_for_unlisted_provider() -> None:
    """Unknown provider → unknown."""
    service = PricingService()
    cost, source = service.estimate_token_cost(
        provider="anthropic",
        model="claude-3",
        prompt_tokens=1000,
        completion_tokens=200,
    )
    assert source == AiCostSource.UNKNOWN
    assert cost is None


def test_token_cost_uses_default_rates_for_unknown_model() -> None:
    """Unknown model falls back to provider's 'default' rates."""
    service = PricingService()
    cost, source = service.estimate_token_cost(
        provider="openai",
        model="gpt-99-future",
        prompt_tokens=1000,
        completion_tokens=200,
    )
    assert source == AiCostSource.MEASURED_USAGE_PRICED
    assert cost is not None


def test_token_cost_cached_input_discount() -> None:
    """Cached tokens are priced at the cached rate, not the full input rate."""
    service = PricingService()
    cost, source = service.estimate_token_cost(
        provider="openai",
        model="gpt-5.4-mini",
        prompt_tokens=1000,
        completion_tokens=0,
        cached_tokens=500,
    )
    assert source == AiCostSource.MEASURED_USAGE_PRICED
    # 500 * 0.75/1M (non-cached) + 500 * 0.075/1M (cached)
    expected = Decimal("0.000375") + Decimal("0.0000375")
    assert cost == expected


def test_gemini_token_cost() -> None:
    """Gemini tokens are priced from the google section."""
    service = PricingService()
    cost, source = service.estimate_token_cost(
        provider="google",
        model="gemini-3.5-flash-lite",
        prompt_tokens=2000,
        completion_tokens=150,
    )
    assert source == AiCostSource.MEASURED_USAGE_PRICED
    assert cost is not None
    # 2000 * 0.10/1M + 150 * 0.40/1M
    expected = Decimal("0.0002") + Decimal("0.00006")
    assert cost == expected


def test_audio_cost_fallback_estimate() -> None:
    """Modal audio → fallback_estimate with per-minute rate."""
    service = PricingService()
    cost, source = service.estimate_audio_cost(audio_duration_seconds=120)
    assert source == AiCostSource.FALLBACK_ESTIMATE
    # 120s / 60 = 2 min * $0.006 = $0.012
    assert cost == Decimal("0.012")


def test_audio_cost_unknown_when_no_duration() -> None:
    """No audio duration → unknown."""
    service = PricingService()
    cost, source = service.estimate_audio_cost(audio_duration_seconds=None)
    assert source == AiCostSource.UNKNOWN
    assert cost is None


def test_audio_cost_unknown_when_zero_duration() -> None:
    """Zero audio duration → unknown."""
    service = PricingService()
    cost, source = service.estimate_audio_cost(audio_duration_seconds=0)
    assert source == AiCostSource.UNKNOWN
    assert cost is None


def test_image_cost_fallback_estimate() -> None:
    """Image cost → fallback_estimate with per-image rate."""
    service = PricingService()
    cost, source = service.estimate_image_cost(image_count=3)
    assert source == AiCostSource.FALLBACK_ESTIMATE
    # 3 * $0.002 = $0.006
    assert cost == Decimal("0.006")


def test_image_cost_unknown_when_no_count() -> None:
    """No image count → unknown."""
    service = PricingService()
    cost, source = service.estimate_image_cost(image_count=None)
    assert source == AiCostSource.UNKNOWN
    assert cost is None


def test_modal_config_parsed_correctly() -> None:
    """The new modal.audio_cost structure is parsed correctly."""
    service = PricingService()
    config = service._config
    assert config.modal_gpu_type == "T4"
    assert config.modal_audio_usd_per_minute == Decimal("0.006")
    assert config.modal_audio_include_overhead is True
    assert "T4" in config.modal_gpu_second
    assert config.modal_gpu_second["T4"] == Decimal("0.000164")
