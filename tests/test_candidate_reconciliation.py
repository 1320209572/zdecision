from __future__ import annotations

import hashlib
import unittest
from dataclasses import replace

from zdecision.capture.models import (
    Candidate,
    CandidateContent,
    SourceCheckpoint,
)
from zdecision.ids import candidate_family_id, candidate_revision_id
from zdecision.jsonio import canonical_json_bytes
from zdecision.capture.provenance import CandidateProvenanceSummary


REPOSITORY_ID = "repo_11111111111111111111111111111111"
DECISION_SPACE_ID = "dsp_22222222222222222222222222222222"
OTHER_DECISION_SPACE_ID = "dsp_33333333333333333333333333333333"


def content(claim: str, future_action: str) -> CandidateContent:
    return CandidateContent(
        product="ZDecision",
        claim=claim,
        future_action=future_action,
        scope_summary="Candidate 自动采集",
        repositories=("zdecision",),
        paths=(),
        invalidation_conditions=("用户重新定义采集边界",),
    )


def observation(
    seed: str,
    ordinal: int,
    candidate_content: CandidateContent,
) -> Candidate:
    capture_id = f"cap_{seed * 32}"
    return Candidate(
        candidate_id=f"cand_{seed * 32}_{ordinal:02d}",
        capture_id=capture_id,
        ordinal=ordinal,
        content=candidate_content,
        source=SourceCheckpoint(
            thread_id=f"thread-{seed}",
            turn_id=f"turn-{seed}",
        ),
    )


OBSERVATION_A = observation(
    "a",
    1,
    content(
        "更新候选决策按钮是采集授权边界",
        "只在用户点击后运行 Candidate 提取",
    ),
)
OBSERVATION_B = observation(
    "b",
    1,
    content(
        "用户点击更新候选决策后才允许采集",
        "未点击时不得启动 Candidate 提取",
    ),
)
REVERSED_OBSERVATION = observation(
    "c",
    1,
    content(
        "候选决策改为持续自动采集",
        "不再等待页面按钮授权",
    ),
)
CURRENT_CONTENT = content(
    "更新候选决策按钮是采集授权边界",
    "只在用户点击后运行 Candidate 提取",
)
CURRENT_FAMILY = candidate_family_id(
    REPOSITORY_ID, DECISION_SPACE_ID, OBSERVATION_A.candidate_id
)
CURRENT_DIGEST = hashlib.sha256(
    canonical_json_bytes(CURRENT_CONTENT.to_dict())
).hexdigest()


def provenance(seed: str) -> CandidateProvenanceSummary:
    return CandidateProvenanceSummary(
        protocol="candidate-provenance-v1",
        kind="host_observed_user_prompt_anchor",
        digest=seed * 64,
    )


class CandidateReconciliationTest(unittest.TestCase):
    def _api(self):
        try:
            from zdecision.capture.reconciliation import (
                CandidateFamilyRevision,
                ReconciliationDecision,
                apply_reconciliation,
                reconciliation_output_schema,
                validate_reconciliation,
            )
        except ModuleNotFoundError as error:
            self.fail(f"Candidate reconciliation API is missing: {error}")
        current = CandidateFamilyRevision(
            family_id=CURRENT_FAMILY,
            revision_id=candidate_revision_id(
                CURRENT_FAMILY, 1, CURRENT_DIGEST
            ),
            revision=1,
            content=CURRENT_CONTENT,
            content_digest=CURRENT_DIGEST,
            evidence_digest="d" * 64,
            supersedes_revision_id=None,
        )
        return (
            current,
            ReconciliationDecision,
            apply_reconciliation,
            reconciliation_output_schema,
            validate_reconciliation,
        )

    def test_two_new_equivalent_observations_form_one_family(self) -> None:
        (
            _,
            ReconciliationDecision,
            apply_reconciliation,
            _,
            _,
        ) = self._api()
        first, second = sorted(
            (OBSERVATION_A, OBSERVATION_B),
            key=lambda item: item.candidate_id,
        )
        first_family = candidate_family_id(
            REPOSITORY_ID, DECISION_SPACE_ID, first.candidate_id
        )
        decisions = (
            ReconciliationDecision(
                first.candidate_id,
                "unrelated",
                first_family,
                None,
            ),
            ReconciliationDecision(
                second.candidate_id,
                "same",
                first_family,
                None,
            ),
        )

        result = apply_reconciliation(
            REPOSITORY_ID,
            DECISION_SPACE_ID,
            (first, second),
            (),
            decisions,
        )

        self.assertEqual(1, len(result.current_revisions))
        self.assertEqual(first_family, result.current_revisions[0].family_id)
        self.assertEqual(1, result.current_revisions[0].revision)
        self.assertEqual(
            (second.candidate_id,), result.same_observation_ids
        )
        self.assertEqual(
            result.current_revisions, result.uploadable_revisions
        )

    def test_same_adds_evidence_without_new_revision(self) -> None:
        (
            current,
            ReconciliationDecision,
            apply_reconciliation,
            _,
            _,
        ) = self._api()

        result = apply_reconciliation(
            REPOSITORY_ID,
            DECISION_SPACE_ID,
            (OBSERVATION_A,),
            (current,),
            (
                ReconciliationDecision(
                    OBSERVATION_A.candidate_id,
                    "same",
                    current.family_id,
                    None,
                ),
            ),
            {OBSERVATION_A.candidate_id: provenance("a")},
        )

        self.assertEqual(1, result.current_revisions[0].revision)
        self.assertEqual((), result.new_revisions)
        self.assertEqual((), result.uploadable_revisions)
        self.assertEqual(
            (OBSERVATION_A.candidate_id,),
            result.same_observation_ids,
        )

    def test_refine_creates_the_next_revision_with_effective_content(
        self,
    ) -> None:
        (
            current,
            ReconciliationDecision,
            apply_reconciliation,
            _,
            _,
        ) = self._api()
        refined = content(
            "只有显式页面操作才授权 Candidate 采集",
            "后台 Agent 必须绑定 Capture Request 后再提取",
        )

        result = apply_reconciliation(
            REPOSITORY_ID,
            DECISION_SPACE_ID,
            (OBSERVATION_B,),
            (current,),
            (
                ReconciliationDecision(
                    OBSERVATION_B.candidate_id,
                    "refine",
                    current.family_id,
                    refined,
                ),
            ),
        )

        revision = result.current_revisions[0]
        self.assertEqual(2, revision.revision)
        self.assertEqual(refined, revision.content)
        self.assertEqual(
            current.revision_id, revision.supersedes_revision_id
        )
        self.assertEqual((revision,), result.new_revisions)
        self.assertEqual((revision,), result.uploadable_revisions)

    def test_new_and_changed_revisions_bind_triggering_provenance(self) -> None:
        (
            current,
            ReconciliationDecision,
            apply_reconciliation,
            _,
            _,
        ) = self._api()
        cases = (
            ("unrelated", (), OBSERVATION_A, OBSERVATION_A.content),
            ("refine", (current,), OBSERVATION_B, OBSERVATION_B.content),
            ("replace", (current,), REVERSED_OBSERVATION, REVERSED_OBSERVATION.content),
        )

        for relation, current_values, item, effective in cases:
            with self.subTest(relation=relation):
                if current_values:
                    current_values = (
                        replace(current, provenance=provenance("e")),
                    )
                family_id = (
                    candidate_family_id(
                        REPOSITORY_ID, DECISION_SPACE_ID, item.candidate_id
                    )
                    if relation == "unrelated"
                    else current.family_id
                )
                summary = provenance(item.candidate_id[5])
                result = apply_reconciliation(
                    REPOSITORY_ID,
                    DECISION_SPACE_ID,
                    (item,),
                    current_values,
                    (
                        ReconciliationDecision(
                            item.candidate_id,
                            relation,
                            family_id,
                            None if relation == "unrelated" else effective,
                        ),
                    ),
                    {item.candidate_id: summary},
                )

                self.assertEqual(summary, result.new_revisions[0].provenance)
                self.assertEqual(
                    summary.to_dict(),
                    result.new_revisions[0].to_dict()["provenance"],
                )

    def test_legacy_family_cannot_be_refined_or_replaced_by_v1_observation(self) -> None:
        current, ReconciliationDecision, apply_reconciliation, _, _ = self._api()

        for relation in ("refine", "replace"):
            with self.subTest(relation=relation):
                result = apply_reconciliation(
                    REPOSITORY_ID,
                    DECISION_SPACE_ID,
                    (OBSERVATION_B,),
                    (current,),
                    (
                        ReconciliationDecision(
                            OBSERVATION_B.candidate_id,
                            relation,
                            current.family_id,
                            OBSERVATION_B.content,
                        ),
                    ),
                    {OBSERVATION_B.candidate_id: provenance("b")},
                )

                self.assertEqual((current,), result.current_revisions)
                self.assertEqual((), result.new_revisions)
                self.assertEqual((), result.uploadable_revisions)
                self.assertEqual(
                    (OBSERVATION_B.candidate_id,),
                    result.ambiguous_observation_ids,
                )

    def test_legacy_revision_round_trips_without_provenance_field(self) -> None:
        current, _, _, _, _ = self._api()
        from zdecision.capture.reconciliation import CandidateFamilyRevision

        payload = current.to_dict()

        self.assertNotIn("provenance", payload)
        self.assertEqual(current, CandidateFamilyRevision.from_dict(payload))

    def test_later_reversal_replaces_with_monotonic_revision(self) -> None:
        (
            current,
            ReconciliationDecision,
            apply_reconciliation,
            _,
            _,
        ) = self._api()

        result = apply_reconciliation(
            REPOSITORY_ID,
            DECISION_SPACE_ID,
            (REVERSED_OBSERVATION,),
            (current,),
            (
                ReconciliationDecision(
                    REVERSED_OBSERVATION.candidate_id,
                    "replace",
                    current.family_id,
                    REVERSED_OBSERVATION.content,
                ),
            ),
        )

        revision = result.current_revisions[0]
        self.assertEqual(2, revision.revision)
        self.assertEqual(
            current.revision_id, revision.supersedes_revision_id
        )
        self.assertEqual(
            candidate_revision_id(
                revision.family_id,
                revision.revision,
                revision.content_digest,
            ),
            revision.revision_id,
        )

    def test_ambiguous_observation_never_enters_upload_outbox(self) -> None:
        (
            current,
            ReconciliationDecision,
            apply_reconciliation,
            _,
            _,
        ) = self._api()

        result = apply_reconciliation(
            REPOSITORY_ID,
            DECISION_SPACE_ID,
            (OBSERVATION_B,),
            (current,),
            (
                ReconciliationDecision(
                    OBSERVATION_B.candidate_id,
                    "ambiguous",
                    None,
                    None,
                ),
            ),
            {OBSERVATION_B.candidate_id: provenance("b")},
        )

        self.assertEqual((), result.uploadable_revisions)
        self.assertEqual(
            (OBSERVATION_B.candidate_id,),
            result.ambiguous_observation_ids,
        )
        self.assertEqual((current,), result.current_revisions)

    def test_multiple_local_revisions_upload_only_the_final_head(self) -> None:
        (
            _,
            ReconciliationDecision,
            apply_reconciliation,
            _,
            _,
        ) = self._api()
        first, second = sorted(
            (OBSERVATION_A, REVERSED_OBSERVATION),
            key=lambda item: item.candidate_id,
        )
        family_id = candidate_family_id(
            REPOSITORY_ID, DECISION_SPACE_ID, first.candidate_id
        )

        result = apply_reconciliation(
            REPOSITORY_ID,
            DECISION_SPACE_ID,
            (first, second),
            (),
            (
                ReconciliationDecision(
                    first.candidate_id,
                    "unrelated",
                    family_id,
                    None,
                ),
                ReconciliationDecision(
                    second.candidate_id,
                    "replace",
                    family_id,
                    second.content,
                ),
            ),
        )

        self.assertEqual((1, 2), tuple(
            item.revision for item in result.new_revisions
        ))
        self.assertEqual(1, len(result.uploadable_revisions))
        self.assertEqual(2, result.uploadable_revisions[0].revision)

    def test_forward_family_reference_and_invented_family_are_rejected(
        self,
    ) -> None:
        (
            _,
            ReconciliationDecision,
            apply_reconciliation,
            _,
            _,
        ) = self._api()
        first, second = sorted(
            (OBSERVATION_A, OBSERVATION_B),
            key=lambda item: item.candidate_id,
        )
        later_family = candidate_family_id(
            REPOSITORY_ID, DECISION_SPACE_ID, second.candidate_id
        )

        with self.assertRaises(ValueError):
            apply_reconciliation(
                REPOSITORY_ID,
                DECISION_SPACE_ID,
                (first, second),
                (),
                (
                    ReconciliationDecision(
                        first.candidate_id,
                        "same",
                        later_family,
                        None,
                    ),
                    ReconciliationDecision(
                        second.candidate_id,
                        "unrelated",
                        later_family,
                        None,
                    ),
                ),
            )

        with self.assertRaises(ValueError):
            apply_reconciliation(
                REPOSITORY_ID,
                DECISION_SPACE_ID,
                (first,),
                (),
                (
                    ReconciliationDecision(
                        first.candidate_id,
                        "same",
                        "cfm_" + "f" * 32,
                        None,
                    ),
                ),
            )

    def test_model_output_is_exact_ordered_and_relation_specific(self) -> None:
        (
            current,
            _,
            _,
            _,
            validate_reconciliation,
        ) = self._api()
        ordered = tuple(sorted(
            (OBSERVATION_A, OBSERVATION_B),
            key=lambda item: item.candidate_id,
        ))
        value = {
            "results": [
                {
                    "observation_id": ordered[0].candidate_id,
                    "relation": "same",
                    "family_id": current.family_id,
                    "effective_content": None,
                },
                {
                    "observation_id": ordered[1].candidate_id,
                    "relation": "ambiguous",
                    "family_id": None,
                    "effective_content": None,
                },
            ]
        }

        decisions = validate_reconciliation(
            value, ordered, (current,)
        )

        self.assertEqual(
            tuple(item.candidate_id for item in ordered),
            tuple(item.observation_id for item in decisions),
        )

        reversed_value = {
            "results": list(reversed(value["results"])),
        }
        with self.assertRaises(ValueError):
            validate_reconciliation(
                reversed_value, ordered, (current,)
            )

        invalid_content = {
            "results": [
                {
                    **value["results"][0],
                    "effective_content": OBSERVATION_A.content.to_dict(),
                },
                value["results"][1],
            ]
        }
        with self.assertRaises(ValueError):
            validate_reconciliation(
                invalid_content, ordered, (current,)
            )

    def test_output_schema_is_closed_and_host_enum_bounded(self) -> None:
        (
            current,
            _,
            _,
            reconciliation_output_schema,
            _,
        ) = self._api()
        schema = reconciliation_output_schema(
            observation_ids=(OBSERVATION_A.candidate_id,),
            family_ids=(
                current.family_id,
                candidate_family_id(
                    REPOSITORY_ID,
                    DECISION_SPACE_ID,
                    OBSERVATION_A.candidate_id,
                ),
            ),
        )

        self.assertFalse(schema["additionalProperties"])
        result_items = schema["properties"]["results"]["items"]
        self.assertEqual(
            [OBSERVATION_A.candidate_id],
            result_items["properties"]["observation_id"]["enum"],
        )
        family_options = result_items["properties"]["family_id"]["anyOf"]
        self.assertEqual(
            sorted(set((
                current.family_id,
                candidate_family_id(
                    REPOSITORY_ID,
                    DECISION_SPACE_ID,
                    OBSERVATION_A.candidate_id,
                ),
            ))),
            family_options[0]["enum"],
        )

    def test_family_continuity_is_repository_and_decision_space_scoped(
        self,
    ) -> None:
        cloud = candidate_family_id(
            REPOSITORY_ID,
            DECISION_SPACE_ID,
            OBSERVATION_A.candidate_id,
        )
        shared = candidate_family_id(
            REPOSITORY_ID,
            OTHER_DECISION_SPACE_ID,
            OBSERVATION_A.candidate_id,
        )

        self.assertNotEqual(cloud, shared)


if __name__ == "__main__":
    unittest.main()
