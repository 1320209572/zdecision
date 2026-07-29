"""Repository-owned decision-compression template loading and rendering."""

from __future__ import annotations

import hashlib
import json
import re
import stat
import unicodedata
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from zdecision.capture.prompts import (
    CANDIDATE_CONTRACT_VERSION,
    INVENTORY_CONTRACT_VERSION,
    candidate_schema_json,
    inventory_schema_json,
)
from zdecision.jsonio import canonical_json_bytes


RENDERER_VERSION = "renderer-v1"

_TEMPLATE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_PLACEHOLDER_PATTERN = re.compile(r"{{([^{}]*)}}")
_MANIFEST_FIELDS = frozenset(
    (
        "template_id",
        "revision",
        "title",
        "inventory_template",
        "extraction_template",
    )
)
_INVENTORY_PLACEHOLDERS = frozenset(
    (
        "template_id",
        "template_revision",
        "policy_body",
        "product_json",
        "inventory_schema_json",
    )
)
_EXTRACTION_PLACEHOLDERS = frozenset(
    (
        "template_id",
        "template_revision",
        "policy_body",
        "product_json",
        "candidate_schema_json",
    )
)
_MAX_POLICY_BYTES = 64 * 1024
_MAX_PROMPT_BYTES = 128 * 1024


@dataclass(frozen=True)
class TemplateSnapshot:
    template_id: str
    revision: int
    title: str
    template_source_sha256: str
    renderer_version: str
    inventory_contract_version: str
    candidate_contract_version: str
    inventory_prompt_sha256: str
    extraction_prompt_sha256: str
    prompt_bundle_sha256: str
    inventory_prompt: str
    extraction_prompt: str

    def to_dict(self) -> dict[str, object]:
        return {
            "template_id": self.template_id,
            "revision": self.revision,
            "title": self.title,
            "template_source_sha256": self.template_source_sha256,
            "renderer_version": self.renderer_version,
            "inventory_contract_version": self.inventory_contract_version,
            "candidate_contract_version": self.candidate_contract_version,
            "inventory_prompt_sha256": self.inventory_prompt_sha256,
            "extraction_prompt_sha256": self.extraction_prompt_sha256,
            "prompt_bundle_sha256": self.prompt_bundle_sha256,
            "inventory_prompt": self.inventory_prompt,
            "extraction_prompt": self.extraction_prompt,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> TemplateSnapshot:
        fields = frozenset(
            (
                "template_id",
                "revision",
                "title",
                "template_source_sha256",
                "renderer_version",
                "inventory_contract_version",
                "candidate_contract_version",
                "inventory_prompt_sha256",
                "extraction_prompt_sha256",
                "prompt_bundle_sha256",
                "inventory_prompt",
                "extraction_prompt",
            )
        )
        if frozenset(value) != fields:
            raise ValueError("Invalid TemplateSnapshot fields")
        if not isinstance(value["revision"], int) or isinstance(
            value["revision"], bool
        ):
            raise ValueError("TemplateSnapshot revision must be an integer")
        strings = {field: value[field] for field in fields - {"revision"}}
        if any(not isinstance(item, str) for item in strings.values()):
            raise ValueError("TemplateSnapshot text fields must be strings")
        snapshot = cls(revision=value["revision"], **strings)
        snapshot.verify_integrity()
        return snapshot

    def verify_integrity(self) -> None:
        inventory_digest = _prompt_digest(
            stage="inventory",
            contract_version=self.inventory_contract_version,
            renderer_version=self.renderer_version,
            prompt=self.inventory_prompt,
        )
        extraction_digest = _prompt_digest(
            stage="extraction",
            contract_version=self.candidate_contract_version,
            renderer_version=self.renderer_version,
            prompt=self.extraction_prompt,
        )
        bundle_digest = _prompt_bundle_digest(
            candidate_contract_version=self.candidate_contract_version,
            extraction_prompt=self.extraction_prompt,
            inventory_contract_version=self.inventory_contract_version,
            inventory_prompt=self.inventory_prompt,
            renderer_version=self.renderer_version,
        )
        if (
            inventory_digest != self.inventory_prompt_sha256
            or extraction_digest != self.extraction_prompt_sha256
            or bundle_digest != self.prompt_bundle_sha256
        ):
            raise ValueError("TemplateSnapshot prompt digest mismatch")


class TemplateValidationError(ValueError):
    pass


def _prompt_digest(
    *, stage: str, contract_version: str, renderer_version: str, prompt: str
) -> str:
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "contract_version": contract_version,
                "prompt": prompt,
                "renderer_version": renderer_version,
                "stage": stage,
            }
        )
    ).hexdigest()


def _prompt_bundle_digest(
    *,
    candidate_contract_version: str,
    extraction_prompt: str,
    inventory_contract_version: str,
    inventory_prompt: str,
    renderer_version: str,
) -> str:
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "candidate_contract_version": candidate_contract_version,
                "extraction_prompt": extraction_prompt,
                "inventory_contract_version": inventory_contract_version,
                "inventory_prompt": inventory_prompt,
                "renderer_version": renderer_version,
            }
        )
    ).hexdigest()


@dataclass(frozen=True)
class _Manifest:
    template_id: str
    revision: int
    title: str
    inventory_template: str
    extraction_template: str
    directory: Path
    raw_text: str


class TemplateCatalog:
    def __init__(self, template_root: Path, envelope_root: Path) -> None:
        self.template_root = Path(template_root)
        self.envelope_root = Path(envelope_root)

    def render(self, template_id: str, product: str) -> TemplateSnapshot:
        self._validate_requested_template_id(template_id)
        self._validate_product(product)
        manifest, inventory_policy, extraction_policy = self._load(template_id)
        return self._render(manifest, inventory_policy, extraction_policy, product)

    def _load(self, template_id: str) -> tuple[_Manifest, str, str]:
        manifests = self._load_catalog()
        manifest = manifests.get(template_id)
        if manifest is None:
            raise TemplateValidationError(f"unknown template: {template_id}")

        inventory_policy = self._load_policy(
            manifest.directory,
            manifest.inventory_template,
            "inventory",
        )
        extraction_policy = self._load_policy(
            manifest.directory,
            manifest.extraction_template,
            "extraction",
        )
        manifest_path = manifest.directory / "manifest.json"
        inventory_path = manifest.directory / manifest.inventory_template
        extraction_path = manifest.directory / manifest.extraction_template
        if inventory_path.samefile(manifest_path) or extraction_path.samefile(
            manifest_path
        ):
            raise TemplateValidationError(
                "manifest policy paths must reference policy files"
            )
        if inventory_path.samefile(extraction_path):
            raise TemplateValidationError(
                "manifest policy paths must reference distinct files"
            )
        return manifest, inventory_policy, extraction_policy

    def _load_catalog(self) -> dict[str, _Manifest]:
        if self.template_root.is_symlink():
            raise TemplateValidationError("template root must not be a symlink")
        if not self.template_root.is_dir():
            raise TemplateValidationError("template catalog is missing")

        manifests: dict[str, _Manifest] = {}
        for directory in sorted(self.template_root.iterdir(), key=lambda item: item.name):
            if directory.is_symlink():
                raise TemplateValidationError("template directory must not be a symlink")
            if not directory.is_dir():
                continue
            manifest = self._load_manifest(directory)
            if manifest.template_id in manifests:
                raise TemplateValidationError(
                    f"duplicate template catalog id: {manifest.template_id}"
                )
            manifests[manifest.template_id] = manifest
        return manifests

    def _load_manifest(self, directory: Path) -> _Manifest:
        manifest_path = directory / "manifest.json"
        raw_text = self._read_utf8_file(manifest_path, "manifest")
        try:
            value = json.loads(raw_text, object_pairs_hook=_unique_json_object)
        except (json.JSONDecodeError, TemplateValidationError) as exc:
            raise TemplateValidationError(f"invalid manifest JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise TemplateValidationError("manifest must be an object")

        fields = frozenset(value)
        unknown = fields - _MANIFEST_FIELDS
        missing = _MANIFEST_FIELDS - fields
        if unknown:
            raise TemplateValidationError("manifest contains unknown fields")
        if missing:
            raise TemplateValidationError("manifest is missing required fields")

        template_id = value["template_id"]
        revision = value["revision"]
        title = value["title"]
        inventory_template = value["inventory_template"]
        extraction_template = value["extraction_template"]
        if not isinstance(template_id, str) or not _TEMPLATE_ID_PATTERN.fullmatch(
            template_id
        ):
            raise TemplateValidationError("manifest template_id is invalid")
        if (
            not isinstance(revision, int)
            or isinstance(revision, bool)
            or revision <= 0
        ):
            raise TemplateValidationError("manifest revision must be a positive integer")
        if not isinstance(title, str) or not title.strip():
            raise TemplateValidationError("manifest title must not be empty")
        if not isinstance(inventory_template, str) or not isinstance(
            extraction_template, str
        ):
            raise TemplateValidationError("manifest policy paths must be strings")
        if Path(inventory_template) == Path(extraction_template):
            raise TemplateValidationError(
                "manifest policy paths must reference distinct files"
            )

        return _Manifest(
            template_id=template_id,
            revision=revision,
            title=title,
            inventory_template=inventory_template,
            extraction_template=extraction_template,
            directory=directory,
            raw_text=raw_text,
        )

    def _load_policy(self, directory: Path, filename: str, role: str) -> str:
        relative = Path(filename)
        if not filename or relative.is_absolute() or ".." in relative.parts:
            raise TemplateValidationError(f"{role} policy path is invalid")
        if relative.parent != Path("."):
            raise TemplateValidationError(
                f"{role} policy parent must be the selected template directory"
            )
        policy = self._read_utf8_file(
            directory / relative,
            f"{role} policy",
            max_bytes=_MAX_POLICY_BYTES,
        )
        if "{{" in policy:
            raise TemplateValidationError(f"{role} policy contains a placeholder")
        if "ZDECISION_CAPTURE_ARTIFACT" in policy:
            raise TemplateValidationError(
                f"{role} policy contains a reserved artifact marker"
            )
        if "<decision_policy" in policy or "</decision_policy" in policy:
            raise TemplateValidationError(
                f"{role} policy contains a decision policy tag"
            )
        return policy

    def _render(
        self,
        manifest: _Manifest,
        inventory_policy: str,
        extraction_policy: str,
        product: str,
    ) -> TemplateSnapshot:
        inventory_envelope = self._load_envelope(
            "inventory-envelope.md",
            _INVENTORY_PLACEHOLDERS,
        )
        extraction_envelope = self._load_envelope(
            "extraction-envelope.md",
            _EXTRACTION_PLACEHOLDERS,
        )
        common = {
            "template_id": manifest.template_id,
            "template_revision": str(manifest.revision),
            "product_json": canonical_json_bytes(product)
            .decode("utf-8")
            .removesuffix("\n"),
        }
        inventory_prompt = self._substitute_once(
            inventory_envelope,
            {
                **common,
                "policy_body": inventory_policy,
                "inventory_schema_json": inventory_schema_json(),
            },
        )
        extraction_prompt = self._substitute_once(
            extraction_envelope,
            {
                **common,
                "policy_body": extraction_policy,
                "candidate_schema_json": candidate_schema_json(product),
            },
        )
        self._validate_rendered_prompt(inventory_prompt, "inventory")
        self._validate_rendered_prompt(extraction_prompt, "extraction")

        template_source_sha256 = hashlib.sha256(
            canonical_json_bytes(
                {
                    "extraction_policy": {
                        "filename": manifest.extraction_template,
                        "text": extraction_policy,
                    },
                    "inventory_policy": {
                        "filename": manifest.inventory_template,
                        "text": inventory_policy,
                    },
                    "manifest": {
                        "filename": "manifest.json",
                        "text": manifest.raw_text,
                    },
                }
            )
        ).hexdigest()
        inventory_digest = _prompt_digest(
            stage="inventory",
            contract_version=INVENTORY_CONTRACT_VERSION,
            renderer_version=RENDERER_VERSION,
            prompt=inventory_prompt,
        )
        extraction_digest = _prompt_digest(
            stage="extraction",
            contract_version=CANDIDATE_CONTRACT_VERSION,
            renderer_version=RENDERER_VERSION,
            prompt=extraction_prompt,
        )
        bundle_digest = _prompt_bundle_digest(
            candidate_contract_version=CANDIDATE_CONTRACT_VERSION,
            extraction_prompt=extraction_prompt,
            inventory_contract_version=INVENTORY_CONTRACT_VERSION,
            inventory_prompt=inventory_prompt,
            renderer_version=RENDERER_VERSION,
        )
        return TemplateSnapshot(
            template_id=manifest.template_id,
            revision=manifest.revision,
            title=manifest.title,
            template_source_sha256=template_source_sha256,
            renderer_version=RENDERER_VERSION,
            inventory_contract_version=INVENTORY_CONTRACT_VERSION,
            candidate_contract_version=CANDIDATE_CONTRACT_VERSION,
            inventory_prompt_sha256=inventory_digest,
            extraction_prompt_sha256=extraction_digest,
            prompt_bundle_sha256=bundle_digest,
            inventory_prompt=inventory_prompt,
            extraction_prompt=extraction_prompt,
        )

    def _load_envelope(self, filename: str, required: frozenset[str]) -> str:
        if self.envelope_root.is_symlink():
            raise TemplateValidationError("envelope root must not be a symlink")
        if not self.envelope_root.is_dir():
            raise TemplateValidationError("prompt envelope root is missing")
        envelope = self._read_utf8_file(
            self.envelope_root / filename,
            f"prompt envelope {filename}",
        )
        placeholders = _PLACEHOLDER_PATTERN.findall(envelope)
        if envelope.count("{{") != len(placeholders) or envelope.count("}}") != len(
            placeholders
        ):
            raise TemplateValidationError("prompt envelope has unknown placeholder syntax")
        counts = Counter(placeholders)
        unknown = frozenset(counts) - required
        missing = required - frozenset(counts)
        duplicate = frozenset(
            name for name, count in counts.items() if count != 1
        )
        if unknown:
            raise TemplateValidationError("prompt envelope contains unknown placeholders")
        if missing:
            raise TemplateValidationError("prompt envelope is missing required placeholders")
        if duplicate:
            raise TemplateValidationError("prompt envelope contains duplicate placeholders")
        return envelope

    @staticmethod
    def _substitute_once(envelope: str, values: Mapping[str, str]) -> str:
        return _PLACEHOLDER_PATTERN.sub(lambda match: values[match.group(1)], envelope)

    @staticmethod
    def _validate_requested_template_id(template_id: str) -> None:
        if not isinstance(template_id, str) or not _TEMPLATE_ID_PATTERN.fullmatch(
            template_id
        ):
            raise TemplateValidationError("template_id is invalid")

    @staticmethod
    def _validate_product(product: str) -> None:
        if not isinstance(product, str) or not product.strip():
            raise TemplateValidationError("product must not be empty")
        if any(unicodedata.category(character) == "Cc" for character in product):
            raise TemplateValidationError("product must not contain control characters")

    @staticmethod
    def _read_utf8_file(
        path: Path,
        label: str,
        *,
        max_bytes: int | None = None,
    ) -> str:
        if path.is_symlink():
            raise TemplateValidationError(f"{label} must not be a symlink")
        try:
            file_status = path.stat(follow_symlinks=False)
        except FileNotFoundError as exc:
            raise TemplateValidationError(f"{label} is missing") from exc
        except OSError as exc:
            raise TemplateValidationError(f"unable to inspect {label}") from exc
        if not stat.S_ISREG(file_status.st_mode):
            raise TemplateValidationError(f"{label} must be a regular file")
        try:
            raw = path.read_bytes()
        except (FileNotFoundError, IsADirectoryError) as exc:
            raise TemplateValidationError(f"{label} is missing") from exc
        except OSError as exc:
            raise TemplateValidationError(f"unable to read {label}") from exc
        if max_bytes is not None and len(raw) > max_bytes:
            raise TemplateValidationError(f"{label} exceeds {max_bytes // 1024} KiB")
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise TemplateValidationError(f"{label} must be valid UTF-8") from exc

    @staticmethod
    def _validate_rendered_prompt(prompt: str, stage: str) -> None:
        encoded = prompt.encode("utf-8")
        if not prompt or not prompt.strip():
            raise TemplateValidationError(f"{stage} rendered prompt must not be empty")
        if len(encoded) > _MAX_PROMPT_BYTES:
            raise TemplateValidationError(f"{stage} rendered prompt exceeds 128 KiB")


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise TemplateValidationError("duplicate manifest field")
        value[key] = item
    return value
