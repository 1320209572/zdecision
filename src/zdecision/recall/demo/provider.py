"""Fail-closed local Recall provider for the bounded third-party-services demo."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from zdecision.jsonio import canonical_json_bytes
from zdecision.recall.demo.bundle import (
    VerifiedDemoBundle,
    VerifiedDemoBundleMetadata,
    load_verified_bundle,
    load_verified_bundle_metadata,
)
from zdecision.recall.demo.config import DemoProviderConfig, load_demo_recall_config
from zdecision.recall.demo.index import DemoIndex
from zdecision.recall.demo.model_store import (
    InstalledModelMetadata,
    InstalledModels,
    load_installed_model_metadata,
    load_installed_models,
)
from zdecision.recall.demo.publication import DemoBundlePointer
from zdecision.recall.demo.retrieval import HybridDemoRetriever
from zdecision.recall.demo.runtime import ModelRuntimeBundle, load_transformers_runtime
from zdecision.recall.handoff import (
    RecallPreflightClarification,
    RecallPreflightReady,
    RecallPreflightResult,
    RecallPreflightUnavailable,
    RecallShortlist,
    RecalledDecision,
)
from zdecision.recall.provider import (
    RecallProvider,
    RecallProviderUnavailable,
    UnavailableRecallProvider,
)
from zdecision.recall.session import RecallIntent


_REPOSITORY_DISPLAY_NAME = "zstack-ui-next"
_PRODUCT_DISPLAY_NAME = "third-party-services"
_DECISION_SPACE_ID = "prod_3e6e73b8defbfee89ce7bf26e739b1dc"
_READY_FOR = timedelta(minutes=15)


@dataclass(frozen=True)
class _RuntimeIndex:
    runtime: ModelRuntimeBundle
    index: DemoIndex


def build_demo_index(bundle: VerifiedDemoBundle, runtime: ModelRuntimeBundle) -> DemoIndex:
    return DemoIndex.build(bundle, runtime)


class DemoRecallProvider:
    """Local-only provider whose expensive retrieval begins only after consent."""

    def __init__(self, config: DemoProviderConfig) -> None:
        self._config = config
        self._runtime_indexes: dict[tuple[str, str, str, str], _RuntimeIndex] = {}

    def preflight(
        self,
        *,
        repository_id: str,
        repository_display_name: str,
        intent: RecallIntent,
        now: datetime,
    ) -> RecallPreflightResult:
        if repository_display_name != _REPOSITORY_DISPLAY_NAME:
            return RecallPreflightUnavailable(code="recall_not_ready")
        try:
            targets = intent.target_decision_space_ids
            if not targets or (intent.explicit_multi_space and _DECISION_SPACE_ID in targets):
                return RecallPreflightClarification(
                    code="recall_product_selection_required",
                    candidate_display_names=(_PRODUCT_DISPLAY_NAME,),
                )
            if targets != (_DECISION_SPACE_ID,):
                return RecallPreflightUnavailable(code="recall_not_ready")
            pointer, bundle_metadata, model_metadata = self._current_metadata()
            self._validate_metadata(pointer, bundle_metadata, model_metadata)
            return RecallPreflightReady(
                repository_id=repository_id,
                repository_display_name=repository_display_name,
                intent=intent,
                target_decision_space_ids=(_DECISION_SPACE_ID,),
                target_display_names=(_PRODUCT_DISPLAY_NAME,),
                catalog_digest=pointer.manifest_digest,
                generation=pointer.generation,
                generation_digest=pointer.generation_digest,
                retrieval_profile_digest=pointer.profile_digest,
                index_generation=pointer.generation,
                freshness="ready",
                expires_at=_canonical_expiry(now),
            )
        except Exception:
            return RecallPreflightUnavailable(code="recall_not_ready")

    def retrieve(self, preflight: RecallPreflightReady) -> RecallShortlist:
        try:
            pointer, bundle_metadata, model_metadata = self._current_metadata()
            self._validate_metadata(pointer, bundle_metadata, model_metadata)
            self._validate_preflight(preflight, pointer)

            bundle = load_verified_bundle(
                bundle_root=_selected_bundle_root(self._config, pointer),
                trust_root_path=self._config.trust_root_path,
            )
            installed = load_installed_models(
                bundle.profile, self._config.model_state_root
            )
            self._validate_full_bindings(
                pointer, bundle_metadata, model_metadata, bundle, installed
            )

            key = (
                pointer.generation_digest,
                pointer.profile_digest,
                pointer.model_install_digest,
                pointer.manifest_digest,
            )
            cached = self._runtime_indexes.get(key)
            if cached is None:
                runtime = load_transformers_runtime(bundle.profile, installed)
                index = build_demo_index(bundle, runtime)
                cached = _RuntimeIndex(runtime=runtime, index=index)
                self._runtime_indexes[key] = cached
            result = HybridDemoRetriever().retrieve(
                preflight.intent, bundle, cached.index, cached.runtime
            )
            if (
                result.intent_digest != preflight.intent.digest
                or result.profile_digest != pointer.profile_digest
                or result.manifest_digest != pointer.manifest_digest
            ):
                raise RecallProviderUnavailable("Recall provider is unavailable")
            items = tuple(
                RecalledDecision.create(
                    decision_space_id=preflight.target_decision_space_ids[0],
                    revision=item.revision,
                    match_reason=item.match_reason,
                )
                for item in result.items
            )
            if any(recalled.digest != ranked.digest for recalled, ranked in zip(items, result.items, strict=True)):
                raise RecallProviderUnavailable("Recall provider is unavailable")
            shortlist = RecallShortlist.create(preflight=preflight, items=items)

            current_pointer, current_bundle, current_model = self._current_metadata()
            self._validate_metadata(current_pointer, current_bundle, current_model)
            if current_pointer != pointer or current_bundle != bundle_metadata or current_model != model_metadata:
                raise RecallProviderUnavailable("Recall provider is unavailable")
            return shortlist
        except RecallProviderUnavailable:
            raise
        except Exception:
            raise RecallProviderUnavailable("Recall provider is unavailable") from None

    def _current_metadata(
        self,
    ) -> tuple[DemoBundlePointer, VerifiedDemoBundleMetadata, InstalledModelMetadata]:
        pointer = _load_current_pointer(self._config)
        bundle_root = _selected_bundle_root(self._config, pointer)
        bundle_metadata = load_verified_bundle_metadata(
            bundle_root=bundle_root, trust_root_path=self._config.trust_root_path
        )
        model_metadata = load_installed_model_metadata(
            bundle_metadata.profile, self._config.model_state_root
        )
        return pointer, bundle_metadata, model_metadata

    def _validate_metadata(
        self,
        pointer: DemoBundlePointer,
        bundle: VerifiedDemoBundleMetadata,
        model: InstalledModelMetadata,
    ) -> None:
        if (
            pointer.profile_digest != bundle.profile.digest
            or pointer.manifest_digest != bundle.manifest_digest
            or pointer.model_install_digest != model.install_manifest_digest
            or model.profile_digest != bundle.profile.digest
            or bundle.decision_space_id != self._config.decision_space_id
            or bundle.product_name != self._config.product_name
            or bundle.repository != self._config.repository_name
            or self._config.repository_name != _REPOSITORY_DISPLAY_NAME
            or self._config.product_name != _PRODUCT_DISPLAY_NAME
            or self._config.decision_space_id != _DECISION_SPACE_ID
            or bundle.decision_count != len(bundle.decision_leaves)
            or bundle.decision_count < 1
        ):
            raise ValueError("recall metadata binding is invalid")

    def _validate_preflight(
        self, preflight: RecallPreflightReady, pointer: DemoBundlePointer
    ) -> None:
        if (
            not isinstance(preflight, RecallPreflightReady)
            or preflight.repository_display_name != _REPOSITORY_DISPLAY_NAME
            or preflight.target_decision_space_ids != (_DECISION_SPACE_ID,)
            or preflight.target_display_names != (_PRODUCT_DISPLAY_NAME,)
            or preflight.catalog_digest != pointer.manifest_digest
            or preflight.generation != pointer.generation
            or preflight.generation_digest != pointer.generation_digest
            or preflight.retrieval_profile_digest != pointer.profile_digest
            or preflight.index_generation != pointer.generation
        ):
            raise RecallProviderUnavailable("Recall provider is unavailable")

    def _validate_full_bindings(
        self,
        pointer: DemoBundlePointer,
        bundle_metadata: VerifiedDemoBundleMetadata,
        model_metadata: InstalledModelMetadata,
        bundle: VerifiedDemoBundle,
        installed: InstalledModels,
    ) -> None:
        install_digest = hashlib.sha256(
            installed.install_manifest_path.read_bytes()
        ).hexdigest()
        if (
            bundle.manifest_digest != pointer.manifest_digest
            or bundle.profile.digest != pointer.profile_digest
            or bundle.decision_space_id != bundle_metadata.decision_space_id
            or bundle.product_name != bundle_metadata.product_name
            or bundle.repository != bundle_metadata.repository
            or installed.profile_digest != pointer.profile_digest
            or install_digest != pointer.model_install_digest
            or model_metadata.install_manifest_digest != install_digest
        ):
            raise RecallProviderUnavailable("Recall provider is unavailable")


def configured_recall_provider(path: Path) -> RecallProvider:
    """Return the local demo provider only when owner-only configuration parses."""
    try:
        return DemoRecallProvider(load_demo_recall_config(Path(path)).provider)
    except Exception:
        return UnavailableRecallProvider()


def _load_current_pointer(config: DemoProviderConfig) -> DemoBundlePointer:
    root = Path(config.bundle_state_root)
    root_state = root.lstat()
    if (
        not root.is_absolute()
        or stat.S_ISLNK(root_state.st_mode)
        or not stat.S_ISDIR(root_state.st_mode)
        or root_state.st_uid != os.geteuid()
        or stat.S_IMODE(root_state.st_mode) != 0o700
    ):
        raise ValueError("pointer invalid")
    path = root / "current.json"
    path_state = path.lstat()
    if (
        stat.S_ISLNK(path_state.st_mode)
        or not stat.S_ISREG(path_state.st_mode)
        or path_state.st_uid != os.geteuid()
        or stat.S_IMODE(path_state.st_mode) != 0o600
    ):
        raise ValueError("pointer invalid")
    content = path.read_bytes()
    value = json.loads(content)
    if canonical_json_bytes(value) != content:
        raise ValueError("pointer invalid")
    return DemoBundlePointer.from_dict(value)


def _selected_bundle_root(config: DemoProviderConfig, pointer: DemoBundlePointer) -> Path:
    root = Path(config.bundle_state_root)
    bundles = root / "bundles"
    generation = bundles / pointer.publication_commit
    bundle = generation / "bundle"
    states = (root.lstat(), bundles.lstat(), generation.lstat(), bundle.lstat())
    if any(
        stat.S_ISLNK(item.st_mode)
        or not stat.S_ISDIR(item.st_mode)
        or item.st_uid != os.geteuid()
        or stat.S_IMODE(item.st_mode) != 0o700
        for item in states
    ):
        raise ValueError("bundle invalid")
    resolved_root = root.resolve(strict=True)
    resolved_bundles = bundles.resolve(strict=True)
    resolved_generation = generation.resolve(strict=True)
    resolved_bundle = bundle.resolve(strict=True)
    if (
        resolved_bundles.parent != resolved_root
        or resolved_generation.parent != resolved_bundles
        or resolved_generation.name != pointer.publication_commit
        or resolved_bundle.parent != resolved_generation
    ):
        raise ValueError("bundle invalid")
    return resolved_bundle


def _canonical_expiry(now: datetime) -> str:
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("now is invalid")
    return (now.astimezone(UTC) + _READY_FOR).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )
