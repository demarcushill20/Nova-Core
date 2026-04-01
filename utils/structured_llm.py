"""Structured LLM output using instructor and Pydantic schemas.

Provides type-safe LLM interactions for NovaCore, ensuring reliable parsing
of LLM responses into validated Pydantic models. Part of Enhancement Plan v32 Phase 5.

Example:
    from utils.structured_llm import StructuredLLMClient
    from pydantic import BaseModel

    class AnalysisResult(BaseModel):
        confidence: float
        reasoning: str
        recommendation: str

    client = StructuredLLMClient()
    result = client.complete(
        model="gpt-4-turbo-preview",
        response_model=AnalysisResult,
        messages=[{"role": "user", "content": "Analyze this data..."}]
    )
    # result is guaranteed to be an AnalysisResult instance
"""

import logging
import os
from typing import TypeVar

import instructor
import openai
from pydantic import BaseModel, ValidationError

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class StructuredLLMError(Exception):
    """Base exception for structured LLM operations."""

    pass


class ValidationRetryError(StructuredLLMError):
    """Raised when validation fails after all retry attempts."""

    pass


class StructuredLLMClient:
    """Type-safe LLM client using instructor for validated responses."""

    def __init__(self, api_key: str | None = None, max_retries: int = 3, default_model: str = "gpt-4-turbo-preview"):
        """Initialize the structured LLM client.

        Args:
            api_key: OpenAI API key. If None, uses OPENAI_API_KEY env var.
            max_retries: Number of retry attempts on validation failure.
            default_model: Default model to use if not specified in requests.
        """
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        if not self.api_key:
            raise ValueError("OpenAI API key required (OPENAI_API_KEY env var or api_key param)")

        self.max_retries = max_retries
        self.default_model = default_model

        # Initialize instructor-patched OpenAI client
        self.client = instructor.patch(openai.OpenAI(api_key=self.api_key), mode=instructor.Mode.TOOLS)

        log.info("Initialized StructuredLLMClient with instructor integration")

    def complete(
        self,
        response_model: type[T],
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.1,
        max_tokens: int | None = None,
        **kwargs,
    ) -> T:
        """Get a structured response from the LLM.

        Args:
            response_model: Pydantic model class for the expected response.
            messages: List of message dicts with 'role' and 'content' keys.
            model: Model name to use. Defaults to client's default_model.
            temperature: Sampling temperature (0.0 to 1.0).
            max_tokens: Maximum tokens in response.
            **kwargs: Additional parameters passed to OpenAI API.

        Returns:
            Validated instance of response_model.

        Raises:
            ValidationRetryError: If validation fails after all retries.
            StructuredLLMError: For other API or processing errors.
        """
        if not messages:
            raise ValueError("messages cannot be empty")

        if not issubclass(response_model, BaseModel):
            raise TypeError("response_model must be a Pydantic BaseModel subclass")

        model_name = model or self.default_model

        try:
            log.debug(f"Making structured LLM request to {model_name} with {response_model.__name__}")

            response = self.client.chat.completions.create(
                model=model_name,
                messages=messages,
                response_model=response_model,
                temperature=temperature,
                max_tokens=max_tokens,
                max_retries=self.max_retries,
                **kwargs,
            )

            log.debug(f"Successfully got structured response: {type(response).__name__}")
            return response

        except ValidationError as e:
            log.error(f"Validation failed after {self.max_retries} retries: {e}")
            raise ValidationRetryError(f"Failed to get valid {response_model.__name__} after retries") from e

        except Exception as e:
            log.error(f"Structured LLM request failed: {e}")
            raise StructuredLLMError(f"LLM request failed: {e}") from e

    def complete_partial(
        self, response_model: type[T], messages: list[dict[str, str]], model: str | None = None, **kwargs
    ) -> T:
        """Get a partial/streaming structured response (for large outputs).

        This method allows for partial validation during streaming responses,
        useful for large structured outputs that benefit from incremental parsing.

        Args:
            response_model: Pydantic model class for the expected response.
            messages: List of message dicts with 'role' and 'content' keys.
            model: Model name to use. Defaults to client's default_model.
            **kwargs: Additional parameters passed to OpenAI API.

        Returns:
            Validated instance of response_model (final result after streaming).
        """
        model_name = model or self.default_model

        try:
            log.debug(f"Making partial structured LLM request to {model_name}")

            # Use instructor's partial mode for streaming validation
            response = self.client.chat.completions.create_partial(
                model=model_name, messages=messages, response_model=response_model, stream=True, **kwargs
            )

            # Iterate through partial responses and return final validated result
            final_response = None
            for partial in response:
                final_response = partial

            if final_response is None:
                raise StructuredLLMError("No response received from partial completion")

            log.debug(f"Successfully got partial structured response: {type(final_response).__name__}")
            return final_response

        except Exception as e:
            log.error(f"Partial structured LLM request failed: {e}")
            raise StructuredLLMError(f"Partial LLM request failed: {e}") from e


# Pre-configured client instance for convenient imports
default_client = None


def get_client() -> StructuredLLMClient:
    """Get the default structured LLM client instance."""
    global default_client
    if default_client is None:
        default_client = StructuredLLMClient()
    return default_client


def structured_completion(response_model: type[T], messages: list[dict[str, str]], **kwargs) -> T:
    """Convenience function for structured completions using the default client.

    Args:
        response_model: Pydantic model class for the expected response.
        messages: List of message dicts with 'role' and 'content' keys.
        **kwargs: Additional parameters passed to the completion method.

    Returns:
        Validated instance of response_model.
    """
    client = get_client()
    return client.complete(response_model=response_model, messages=messages, **kwargs)


# Common Pydantic schemas for NovaCore operations
class HealthAssessment(BaseModel):
    """Schema for system health assessment responses."""

    overall_health: float  # 0-100 score
    status: str  # HEALTHY, DEGRADED, CRITICAL
    critical_issues: list[str]
    recommendations: list[str]
    confidence: float  # 0-1 confidence in assessment


class TaskAnalysis(BaseModel):
    """Schema for task analysis and prioritization."""

    priority: int  # 1-10 priority score
    complexity: str  # LOW, MEDIUM, HIGH
    estimated_effort: str  # e.g., "2-4 hours", "1-2 days"
    dependencies: list[str]
    risks: list[str]
    success_criteria: list[str]


class DecisionReasoning(BaseModel):
    """Schema for decision-making with explicit reasoning."""

    decision: str
    reasoning: list[str]  # Step-by-step reasoning
    confidence: float  # 0-1 confidence
    alternatives_considered: list[str]
    risks: list[str]
    expected_outcome: str
