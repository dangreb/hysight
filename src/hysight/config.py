from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path
from typing import Any


class ConfigurationError(ValueError):
    pass


def identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}", value):
        raise ConfigurationError(f"Invalid {name}: use a canonical identifier, not a path or alias")
    return value


@dataclasses.dataclass(frozen=True)
class Rule:
    id: str
    source_bank: str
    source_model_id: str
    target_bank: str
    enabled: bool = True
    target_tags: tuple[str, ...] = ()
    require_evidence: bool = True

    @classmethod
    def parse(cls, data: dict[str, Any]) -> Rule:
        if not isinstance(data, dict):
            raise ConfigurationError("Each rule must be an object")
        allowed = {f.name for f in dataclasses.fields(cls)}
        if set(data) - allowed:
            raise ConfigurationError(f"Unknown rule fields: {sorted(set(data) - allowed)}")
        for key in ("id", "source_bank", "source_model_id", "target_bank"):
            identifier(data.get(key), key)
        for key in ("enabled", "require_evidence"):
            if key in data and not isinstance(data[key], bool):
                raise ConfigurationError(f"{key} must be boolean")
        tags = data.get("target_tags", [])
        if not isinstance(tags, list) or any(not isinstance(t, str) or not t.strip() or len(t) > 200 for t in tags):
            raise ConfigurationError("target_tags must be a list of nonempty strings")
        if any(t.startswith("hysight:") for t in tags):
            raise ConfigurationError("The hysight: tag namespace is reserved")
        return cls(**{**data, "target_tags": tuple(sorted(set(tags)))})

    @property
    def identity(self) -> tuple[str, str, str]:
        return self.source_bank, self.source_model_id, self.target_bank

    @property
    def document_id(self) -> str:
        return f"hysight/v1/{self.id}"

    def as_dict(self) -> dict[str, Any]:
        value = dataclasses.asdict(self)
        value["target_tags"] = list(self.target_tags)
        return value


@dataclasses.dataclass(frozen=True)
class Settings:
    rules_file: Path
    state_path: Path
    schema: str = "public"
    poll_seconds: float = 1.0
    reconcile_seconds: float = 60.0
    operation_timeout_seconds: float = 180.0
    retry_base_seconds: float = 5.0
    max_attempts: int = 5
    max_hops: int = 1
    max_content_chars: int = 100_000

    @classmethod
    def parse(cls, data: dict[str, str]) -> Settings:
        allowed = {f.name for f in dataclasses.fields(cls)}
        if set(data) - allowed:
            raise ConfigurationError(f"Unknown extension settings: {sorted(set(data) - allowed)}")
        if not data.get("rules_file") or not data.get("state_path"):
            raise ConfigurationError("rules_file and state_path are required")
        values: dict[str, Any] = dict(data)
        for key in ("rules_file", "state_path"):
            values[key] = Path(values[key]).expanduser().resolve()
        for key in ("poll_seconds", "reconcile_seconds", "operation_timeout_seconds", "retry_base_seconds"):
            if key in values:
                try:
                    values[key] = float(values[key])
                except (ValueError, TypeError) as exc:
                    raise ConfigurationError(f"Invalid {key}") from exc
        for key in ("max_attempts", "max_hops", "max_content_chars"):
            if key in values:
                try:
                    values[key] = int(values[key])
                except (ValueError, TypeError) as exc:
                    raise ConfigurationError(f"Invalid {key}") from exc
        result = cls(**values)
        import math
        for key in ("poll_seconds", "reconcile_seconds", "operation_timeout_seconds", "retry_base_seconds"):
            val = getattr(result, key)
            if not math.isfinite(val) or val <= 0:
                raise ConfigurationError(f"{key} must be finite and positive")
        if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", result.schema):
            raise ConfigurationError("Invalid schema")
        if not 1 <= result.max_attempts <= 20 or not 1 <= result.max_hops <= 8:
            raise ConfigurationError("max_attempts must be 1..20 and max_hops 1..8")
        if not 1 <= result.max_content_chars <= 1_000_000:
            raise ConfigurationError("max_content_chars must be 1..1000000")
        return result


def load_rules(path: Path, max_hops: int = 1) -> list[Rule]:
    if type(max_hops) is not int or not 1 <= max_hops <= 8:
        raise ConfigurationError("max_hops must be an integer in 1..8")
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ConfigurationError("Could not read the rules JSON") from exc
    if not isinstance(data, dict) or set(data) != {"schema_version", "rules"} or type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise ConfigurationError("Expected schema_version=1 and rules")
    if not isinstance(data["rules"], list):
        raise ConfigurationError("rules must be a list")
    rules = [Rule.parse(r) for r in data["rules"]]
    if len({r.id for r in rules}) != len(rules):
        raise ConfigurationError("Duplicate rule id")
    if len({r.identity for r in rules}) != len(rules):
        raise ConfigurationError("Duplicate source/model/destination route")
    graph: dict[str, set[str]] = {}
    for r in rules:
        if r.enabled:
            graph.setdefault(r.source_bank, set()).add(r.target_bank)
    depths: dict[str, int] = {}

    def depth(node: str, visiting: set[str]) -> int:
        if node in visiting:
            raise ConfigurationError("Promotion graph contains a cycle")
        if node in depths:
            return depths[node]
        result = max((1 + depth(child, visiting | {node}) for child in graph.get(node, ())), default=0)
        if result > max_hops:
            raise ConfigurationError(f"Promotion graph exceeds max_hops={max_hops}")
        depths[node] = result
        return result

    for node in graph:
        depth(node, set())
    return rules

