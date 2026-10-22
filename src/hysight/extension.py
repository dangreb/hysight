"""Native Hindsight adapter. Imported only inside a Hindsight environment."""
from __future__ import annotations

import dataclasses
import logging
import os
from typing import Any

from hindsight_api.config import get_config
from hindsight_api.engine.memory_engine import get_current_schema
from hindsight_api.extensions import HttpExtension, OperationValidatorExtension, ValidationResult
from hindsight_api.models import RequestContext

from .config import ConfigurationError, Settings
from .runtime import PromotionError, Runtime

log = logging.getLogger("hysight")
_runtimes: dict[str, Runtime] = {}


def configured_settings() -> Settings:
    prefix = "HINDSIGHT_API_OPERATION_VALIDATOR_"
    return Settings.parse({k[len(prefix):].lower(): v for k, v in os.environ.items()
                           if k.startswith(prefix) and k != prefix + "EXTENSION"})


def shared_runtime(settings: Settings) -> Runtime:
    key = str(settings.state_path)
    if key not in _runtimes:
        _runtimes[key] = Runtime(settings)
    elif _runtimes[key].settings != settings:
        raise ConfigurationError("Different configurations cannot share the same state path")
    return _runtimes[key]


class EngineBackend:
    """V1 is restricted to the server's default tenant schema.

    Internal authority is intentionally limited by validated operator-owned rules.
    No request API key is persisted, replayed or accepted from model content.
    """
    def __init__(self, engine: Any, schema: str):
        self.engine = engine
        self.schema = schema
        if get_config().database_schema != schema:
            raise ConfigurationError("V1 supports only the server's default tenant schema")

    def context(self) -> RequestContext:
        if get_current_schema() != self.schema:
            raise PromotionError("Refusing operation outside the configured tenant schema")
        return RequestContext(internal=True)

    async def model(self, bank: str, model_id: str) -> dict | None:
        return await self.engine.get_mental_model(bank, model_id, detail="full", request_context=self.context())

    async def bank(self, bank: str) -> dict | None:
        return await self.engine.get_bank_profile(bank, request_context=self.context())

    async def document(self, bank: str, document_id: str) -> dict | None:
        return await self.engine.get_document(document_id, bank, request_context=self.context())

    async def retain(self, bank: str, item: dict) -> Any:
        return await self.engine.retain_batch_async(bank_id=bank, contents=[item], request_context=self.context())

    async def delete(self, bank: str, document_id: str) -> Any:
        return await self.engine.delete_document(document_id, bank, request_context=self.context())


class PromotionExtension(OperationValidatorExtension):
    def __init__(self, config: dict[str, str]):
        super().__init__(config)
        self.settings = Settings.parse(config)
        self.runtime = shared_runtime(self.settings)

    async def validate_retain(self, ctx):
        return ValidationResult.accept()

    async def validate_recall(self, ctx):
        return ValidationResult.accept()

    async def validate_reflect(self, ctx):
        return ValidationResult.accept()

    async def on_mental_model_refresh_complete(self, result) -> None:
        if get_current_schema() != self.settings.schema:
            log.warning("Ignored refresh outside Hysight's configured schema")
            return
        ctx = result.request_context
        # Names and nonsecret execution identifiers only. Never serialize RequestContext:
        # api_key, arbitrary extra_headers and cancellation objects are not provenance.
        trace = {
            "internal": bool(getattr(ctx, "internal", False)),
            "operation_id": getattr(ctx, "operation_id", None),
            "has_tenant_id": bool(getattr(ctx, "tenant_id", None)),
            "has_api_key_id": bool(getattr(ctx, "api_key_id", None)),
            "authenticated_schema": getattr(ctx, "authenticated_schema", None),
            "source_schema": get_current_schema(),
            "request_context_fields": sorted(f.name for f in dataclasses.fields(ctx)) if dataclasses.is_dataclass(ctx) else [],
        }
        await self.runtime.hook(result.bank_id, result.mental_model_id, success=result.success, context=trace)


class PromotionLifecycle(HttpExtension):
    """Lifespan integration only: no UI and no additional HTTP endpoints.

    Hindsight 1.0.0 calls lifecycle hooks for HttpExtension, not for its operation
    validator. An operation-only background task would miss restart reconciliation.
    """
    def __init__(self, config: dict[str, str]):
        super().__init__(config)
        if config:
            raise ConfigurationError("Configure Hysight through OPERATION_VALIDATOR settings only")
        self.runtime = shared_runtime(configured_settings())
        self.engine = None

    def get_router(self, memory):
        from fastapi import APIRouter
        self.engine = memory
        return APIRouter()

    async def on_startup(self) -> None:
        if self.engine is None:
            raise RuntimeError("Hindsight did not supply a memory engine")
        await self.runtime.start(EngineBackend(self.engine, self.runtime.settings.schema))

    async def on_shutdown(self) -> None:
        await self.runtime.stop()

