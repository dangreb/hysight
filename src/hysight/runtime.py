from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Protocol

from .config import Rule, Settings, load_rules
from .store import Store, WriterLock

log = logging.getLogger("hysight")
OWNER = "hysight/v1"


class PromotionError(RuntimeError):
    """Safe diagnostic: never includes a provider response, source text or secret."""


class Backend(Protocol):
    async def model(self, bank: str, model_id: str) -> dict | None: ...
    async def bank(self, bank: str) -> dict | None: ...
    async def document(self, bank: str, document_id: str) -> dict | None: ...
    async def retain(self, bank: str, item: dict) -> Any: ...
    async def delete(self, bank: str, document_id: str) -> Any: ...


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def evidence_refs(model: dict) -> dict[str, list[dict[str, str]]]:
    """Preserve references, not source facts' texts, metadata or tool traces."""
    based_on = (model.get("reflect_response") or {}).get("based_on") or {}
    result = {}
    if not isinstance(based_on, dict):
        return result
    for kind, values in based_on.items():
        if not isinstance(values, list) or kind not in ("memories", "mental_models", "world", "experience", "observation"):
            continue
        refs = []
        for value in values:
            if isinstance(value, dict):
                ref = {key: str(value[key]) for key in ("id", "memory_id", "mental_model_id", "document_id", "bank_id") if value.get(key) is not None}
                if any(key in ref for key in ("id", "memory_id", "mental_model_id")):
                    refs.append(ref)
        if refs:
            result[kind] = sorted(refs, key=canonical)
    return result


class Runtime:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.rules = load_rules(settings.rules_file, settings.max_hops)
        self.store = Store(settings.state_path)
        self.lock = WriterLock(Path(str(settings.state_path) + ".lock"))
        self.backend: Backend | None = None
        self.task: asyncio.Task | None = None
        self.started = False

    async def start(self, backend: Backend) -> None:
        if self.started:
            return
        self.lock.acquire()
        try:
            await asyncio.to_thread(self.store.initialize)
            await asyncio.to_thread(self.store.register, self.rules, self.settings.schema)
            self.backend = backend
            self.started = True
            await self.reconcile()
            self.task = asyncio.create_task(self._loop(), name="hysight-promotion-writer")
        except BaseException:
            self.started = False
            self.lock.release()
            raise

    async def stop(self) -> None:
        if self.task is not None:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
            self.task = None
        self.started = False
        self.lock.release()

    async def hook(self, bank: str, model_id: str, *, success: bool, context: dict | None = None) -> int:
        if not success or not self.started:
            return 0
        selected = [r for r in self.rules if r.enabled and r.source_bank == bank and r.source_model_id == model_id]
        for rule in selected:
            await asyncio.to_thread(self.store.schedule, rule.id, hook=True, context=context)
        return len(selected)

    async def reconcile(self) -> None:
        # Includes disabled/removed rules retained in the DB so their derived documents retract.
        snapshot = await asyncio.to_thread(self.store.snapshot)
        for job in snapshot["jobs"]:
            await asyncio.to_thread(self.store.schedule, job["rule_id"])

    async def _loop(self) -> None:
        next_reconcile = asyncio.get_running_loop().time() + self.settings.reconcile_seconds
        while True:
            try:
                if asyncio.get_running_loop().time() >= next_reconcile:
                    await self.reconcile()
                    next_reconcile = asyncio.get_running_loop().time() + self.settings.reconcile_seconds
                if not await self.tick():
                    await asyncio.sleep(self.settings.poll_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Core operations log their own diagnostics. Do not persist arbitrary
                # backend exceptions: they may carry credentials or memory contents.
                log.error("Hysight loop failed (%s)", type(exc).__name__)
                await asyncio.sleep(self.settings.poll_seconds)

    async def tick(self) -> bool:
        job = await asyncio.to_thread(self.store.claim)
        if job is None:
            return False
        attempts = job["attempts"]
        try:
            async with asyncio.timeout(self.settings.operation_timeout_seconds):
                await self._process(job)
        except asyncio.CancelledError:
            # Running stays durable; exclusive writer startup recovers it.
            raise
        except Exception as exc:
            # _process may have reset the attempt counter for new source content.
            current = await asyncio.to_thread(self.store.snapshot)
            attempts = next(x["attempts"] for x in current["jobs"] if x["rule_id"] == job["rule_id"]) + 1
            message = str(exc) if isinstance(exc, PromotionError) else type(exc).__name__
            delay = min(300.0, self.settings.retry_base_seconds * 2 ** min(attempts - 1, 12))
            await asyncio.to_thread(self.store.fail, job, message, attempts, self.settings.max_attempts, delay)
            log.warning("Promotion %s failed (%s)", job["rule_id"], type(exc).__name__)
        return True

    def _owned(self, doc: dict, rule: Rule) -> None:
        metadata = doc.get("document_metadata") or {}
        expected = {"hysight_owner": OWNER, "hysight_rule": rule.id,
                    "source_bank": rule.source_bank, "source_model_id": rule.source_model_id}
        if any(metadata.get(k) != v for k, v in expected.items()):
            raise PromotionError("Destination document is not owned by this rule; refusing to overwrite or delete")

    async def _process(self, job: dict) -> None:
        assert self.backend is not None
        rule = Rule.parse(json.loads(job["rule_json"]))
        target = await self.backend.bank(rule.target_bank)
        if target is None:
            raise PromotionError("Destination bank does not exist; automatic bank creation is disabled")
        if target.get("bank_id") != rule.target_bank:
            raise PromotionError("Destination aliases are not supported; use a canonical bank id")
        doc = await self.backend.document(rule.target_bank, rule.document_id)
        if doc is not None:
            self._owned(doc, rule)
        model = await self.backend.model(rule.source_bank, rule.source_model_id) if rule.enabled else None
        if model is None:
            fingerprint = "retract"
            attempts = await asyncio.to_thread(self.store.attempt, job, fingerprint)
            if attempts >= self.settings.max_attempts:
                await asyncio.to_thread(self.store.blocked, job, "retry_exhausted")
                return
            if doc is not None:
                await self.backend.delete(rule.target_bank, rule.document_id)
            await asyncio.to_thread(self.store.finish, job, "retracted" if doc is not None else "absent", None)
            return
        if model.get("bank_id", rule.source_bank) != rule.source_bank:
            raise PromotionError("Source aliases are not supported; use a canonical bank id")
        content = model.get("content")
        if not isinstance(content, str) or not content.strip():
            # An empty/failed/unwritten refresh is not a deletion or revocation.
            await asyncio.to_thread(self.store.blocked, job, "source_not_ready")
            return
        if len(content) > self.settings.max_content_chars:
            raise PromotionError("Source model exceeds max_content_chars")
        refs = evidence_refs(model)
        if rule.require_evidence and not refs:
            await asyncio.to_thread(self.store.blocked, job, "source_without_evidence")
            return
        semantic = {"kind": "derived_mental_model", "source_bank": rule.source_bank,
                    "source_model_id": rule.source_model_id, "content": content,
                    "evidence_refs": refs, "target_tags": list(rule.target_tags)}
        fingerprint = hashlib.sha256(canonical(semantic).encode()).hexdigest()
        attempts = await asyncio.to_thread(self.store.attempt, job, fingerprint)
        metadata = doc.get("document_metadata") if doc else {}
        if doc is not None and metadata.get("hysight_revision") == fingerprint and doc.get("memory_unit_count", 0) > 0:
            # Also recovers a crash after destination commit but before local acknowledgement.
            await asyncio.to_thread(self.store.finish, job, "unchanged", fingerprint)
            return
        if attempts >= self.settings.max_attempts:
            await asyncio.to_thread(self.store.blocked, job, "retry_exhausted")
            return
        item = {
            "document_id": rule.document_id,
            "content": canonical({**semantic, "source_refreshed_at": str(model.get("last_refreshed_at") or ""),
                                  "notice": "Derived synthesis, not an independent observation or new evidence."}),
            "context": "Hysight promotion of a selected mental model. Preserve original attribution, scope and uncertainty; do not treat the synthesis as independent evidence.",
            "tags": [*rule.target_tags, "hysight:promoted"],
            "metadata": {"hysight_owner": OWNER, "hysight_rule": rule.id,
                         "hysight_revision": fingerprint, "source_bank": rule.source_bank,
                         "source_model_id": rule.source_model_id, "source_schema": self.settings.schema,
                         "source_refreshed_at": str(model.get("last_refreshed_at") or "")},
            "update_mode": "replace",
        }
        await self.backend.retain(rule.target_bank, item)
        saved = await self.backend.document(rule.target_bank, rule.document_id)
        if saved is None or (saved.get("document_metadata") or {}).get("hysight_revision") != fingerprint or saved.get("memory_unit_count", 0) < 1:
            raise PromotionError("Retain did not confirm the expected revision with memory units")
        await asyncio.to_thread(self.store.finish, job, "published", fingerprint)
