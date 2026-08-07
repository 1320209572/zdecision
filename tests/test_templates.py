from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from zdecision.capture.templates import (
    TemplateCatalog,
    TemplateSnapshot,
    TemplateValidationError,
)
from zdecision.capture.prompts import (
    candidate_schema_json,
    inventory_schema_json,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = REPOSITORY_ROOT / "decision-templates"
ENVELOPE_ROOT = REPOSITORY_ROOT / "src/zdecision/capture/prompt_contracts"


class TemplateCatalogTests(unittest.TestCase):
    def catalog(
        self,
        root: Path = TEMPLATE_ROOT,
        envelope_root: Path = ENVELOPE_ROOT,
    ) -> TemplateCatalog:
        return TemplateCatalog(root, envelope_root)

    def copied_roots(self, directory: str) -> tuple[Path, Path]:
        temporary_root = Path(directory)
        copied_templates = temporary_root / "decision-templates"
        copied_envelopes = temporary_root / "prompt_contracts"
        shutil.copytree(TEMPLATE_ROOT, copied_templates)
        shutil.copytree(ENVELOPE_ROOT, copied_envelopes)
        return copied_templates, copied_envelopes

    def manifest(self, template_root: Path, directory: str = "business") -> Path:
        return template_root / directory / "manifest.json"

    def update_manifest(
        self,
        template_root: Path,
        updates: dict[str, object],
        directory: str = "business",
    ) -> None:
        manifest_path = self.manifest(template_root, directory)
        value = json.loads(manifest_path.read_text("utf-8"))
        value.update(updates)
        manifest_path.write_text(json.dumps(value, ensure_ascii=False), "utf-8")

    def test_business_template_renders_both_locked_envelopes(self) -> None:
        snapshot = self.catalog().render("business", "安恒")

        self.assertEqual("business", snapshot.template_id)
        self.assertEqual(2, snapshot.revision)
        self.assertEqual("业务决策压缩模板", snapshot.title)
        self.assertIn("ZDECISION_CAPTURE_ARTIFACT_V2:inventory", snapshot.inventory_prompt)
        self.assertIn("ZDECISION_CAPTURE_ARTIFACT_V2:extract", snapshot.extraction_prompt)
        self.assertIn('目标产品："安恒"', snapshot.inventory_prompt)
        self.assertIn("invalid_inventory", snapshot.inventory_prompt)
        self.assertNotIn("inventory_invalid", snapshot.inventory_prompt)
        self.assertIn(
            '<decision_policy template_id="business" revision="2">',
            snapshot.extraction_prompt,
        )
        self.assertIn('"future_effect"', snapshot.inventory_prompt)
        self.assertIn('"candidates"', snapshot.extraction_prompt)

    def test_business_extraction_policy_is_high_precision(self) -> None:
        prompt = self.catalog().render("business", "安恒").extraction_prompt

        required_rules = (
            "经过明确取舍",
            "不是从产品文档、接口定义或代码中可直接重新查得的普通事实",
            "接口路径、HTTP 方法、请求头、鉴权传递方式、字段名、数据格式、枚举值、默认值、取值范围",
            "除非它本身承载了明确确认的业务语义或兼容性取舍",
            "把用户最终选择的值正确提交",
            "Bug 已修复或实现已通过验证，不等于用户确认了长期决策",
            "同一个底层产品原则",
            "只保留其中信息最完整、边界最清楚的一条代表 signal",
            "不得合并多个 signal 的内容或确认依据",
            "技术契约决策模板",
        )
        for rule in required_rules:
            with self.subTest(rule=rule):
                self.assertIn(rule, prompt)

    def test_policy_change_changes_source_and_prompt_bundle_digests(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / "decision-templates"
            shutil.copytree(TEMPLATE_ROOT, copied)
            before = self.catalog(copied).render("business", "安恒")
            policy = copied / "business" / "inventory.md"
            policy.write_text(
                policy.read_text("utf-8") + "\n新增业务边界。\n",
                "utf-8",
            )
            after = self.catalog(copied).render("business", "安恒")

        self.assertNotEqual(before.template_source_sha256, after.template_source_sha256)
        self.assertNotEqual(before.prompt_bundle_sha256, after.prompt_bundle_sha256)

    def test_manifest_format_change_changes_exact_source_digest_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / "decision-templates"
            shutil.copytree(TEMPLATE_ROOT, copied)
            before = self.catalog(copied).render("business", "安恒")
            manifest = copied / "business" / "manifest.json"
            manifest.write_text(manifest.read_text("utf-8") + "\n", "utf-8")
            after = self.catalog(copied).render("business", "安恒")

        self.assertNotEqual(before.template_source_sha256, after.template_source_sha256)
        self.assertEqual(before.prompt_bundle_sha256, after.prompt_bundle_sha256)

    def test_unknown_manifest_field_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / "decision-templates"
            shutil.copytree(TEMPLATE_ROOT, copied)
            manifest_path = copied / "business" / "manifest.json"
            manifest = json.loads(manifest_path.read_text("utf-8"))
            manifest["prompt"] = "unowned.md"
            manifest_path.write_text(json.dumps(manifest), "utf-8")
            with self.assertRaisesRegex(TemplateValidationError, "unknown"):
                self.catalog(copied).render("business", "安恒")

    def test_missing_manifest_field_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / "decision-templates"
            shutil.copytree(TEMPLATE_ROOT, copied)
            manifest_path = copied / "business" / "manifest.json"
            manifest = json.loads(manifest_path.read_text("utf-8"))
            del manifest["title"]
            manifest_path.write_text(json.dumps(manifest), "utf-8")

            with self.assertRaisesRegex(TemplateValidationError, "missing"):
                self.catalog(copied).render("business", "安恒")

    def test_duplicate_catalog_ids_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / "decision-templates"
            shutil.copytree(TEMPLATE_ROOT, copied)
            shutil.copytree(copied / "business", copied / "duplicate")

            with self.assertRaisesRegex(TemplateValidationError, "duplicate"):
                self.catalog(copied).render("business", "安恒")

    def test_symlinked_policy_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / "decision-templates"
            shutil.copytree(TEMPLATE_ROOT, copied)
            policy = copied / "business" / "inventory.md"
            target = copied / "inventory-target.md"
            policy.rename(target)
            policy.symlink_to(target)

            with self.assertRaisesRegex(TemplateValidationError, "symlink"):
                self.catalog(copied).render("business", "安恒")

    def test_absolute_and_parent_traversal_policy_paths_are_rejected(self) -> None:
        for invalid_path in ("/tmp/inventory.md", "../inventory.md"):
            with self.subTest(invalid_path=invalid_path):
                with tempfile.TemporaryDirectory() as directory:
                    copied = Path(directory) / "decision-templates"
                    shutil.copytree(TEMPLATE_ROOT, copied)
                    self.update_manifest(copied, {"inventory_template": invalid_path})

                    with self.assertRaisesRegex(TemplateValidationError, "path"):
                        self.catalog(copied).render("business", "安恒")

    def test_policy_roles_must_reference_distinct_files(self) -> None:
        references = (
            ("inventory.md", "inventory.md"),
            ("manifest.json", "manifest.json"),
            ("inventory.md", "./inventory.md"),
        )
        for inventory_filename, extraction_filename in references:
            with self.subTest(
                inventory_filename=inventory_filename,
                extraction_filename=extraction_filename,
            ):
                with tempfile.TemporaryDirectory() as directory:
                    copied = Path(directory) / "decision-templates"
                    shutil.copytree(TEMPLATE_ROOT, copied)
                    self.update_manifest(
                        copied,
                        {
                            "inventory_template": inventory_filename,
                            "extraction_template": extraction_filename,
                        },
                    )

                    with self.assertRaisesRegex(TemplateValidationError, "distinct"):
                        self.catalog(copied).render("business", "安恒")

    def test_hard_linked_policy_roles_are_not_distinct_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / "decision-templates"
            shutil.copytree(TEMPLATE_ROOT, copied)
            alias = copied / "business" / "inventory-alias.md"
            os.link(copied / "business" / "inventory.md", alias)
            self.update_manifest(copied, {"extraction_template": alias.name})

            with self.assertRaisesRegex(TemplateValidationError, "distinct"):
                self.catalog(copied).render("business", "安恒")

    def test_manifest_resource_cannot_be_used_as_one_policy_role(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / "decision-templates"
            shutil.copytree(TEMPLATE_ROOT, copied)
            self.update_manifest(copied, {"inventory_template": "manifest.json"})

            with self.assertRaisesRegex(TemplateValidationError, "policy file"):
                self.catalog(copied).render("business", "安恒")

    def test_nested_policy_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / "decision-templates"
            shutil.copytree(TEMPLATE_ROOT, copied)
            nested = copied / "business" / "nested"
            nested.mkdir()
            shutil.copy2(copied / "business" / "inventory.md", nested / "inventory.md")
            self.update_manifest(copied, {"inventory_template": "nested/inventory.md"})

            with self.assertRaisesRegex(TemplateValidationError, "parent"):
                self.catalog(copied).render("business", "安恒")

    def test_missing_policy_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / "decision-templates"
            shutil.copytree(TEMPLATE_ROOT, copied)
            (copied / "business" / "inventory.md").unlink()

            with self.assertRaisesRegex(TemplateValidationError, "missing"):
                self.catalog(copied).render("business", "安恒")

    def test_non_regular_policy_file_is_rejected_before_reading(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / "decision-templates"
            shutil.copytree(TEMPLATE_ROOT, copied)
            policy = copied / "business" / "inventory.md"
            policy_body = policy.read_text("utf-8")
            policy.unlink()
            os.mkfifo(policy)
            writer = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    (
                        "from pathlib import Path; import sys; "
                        "Path(sys.argv[1]).write_text(sys.argv[2], encoding='utf-8')"
                    ),
                    str(policy),
                    policy_body,
                ]
            )
            try:
                with self.assertRaisesRegex(TemplateValidationError, "regular file"):
                    self.catalog(copied).render("business", "安恒")
            finally:
                if writer.poll() is None:
                    writer.terminate()
                writer.wait(timeout=5)

    def test_invalid_utf8_policy_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / "decision-templates"
            shutil.copytree(TEMPLATE_ROOT, copied)
            (copied / "business" / "inventory.md").write_bytes(b"\xff")

            with self.assertRaisesRegex(TemplateValidationError, "UTF-8"):
                self.catalog(copied).render("business", "安恒")

    def test_non_positive_and_boolean_revisions_are_rejected(self) -> None:
        for revision in (0, -1, True):
            with self.subTest(revision=revision):
                with tempfile.TemporaryDirectory() as directory:
                    copied = Path(directory) / "decision-templates"
                    shutil.copytree(TEMPLATE_ROOT, copied)
                    self.update_manifest(copied, {"revision": revision})

                    with self.assertRaisesRegex(TemplateValidationError, "revision"):
                        self.catalog(copied).render("business", "安恒")

    def test_empty_title_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / "decision-templates"
            shutil.copytree(TEMPLATE_ROOT, copied)
            self.update_manifest(copied, {"title": "   "})

            with self.assertRaisesRegex(TemplateValidationError, "title"):
                self.catalog(copied).render("business", "安恒")

    def test_invalid_template_identifier_is_rejected(self) -> None:
        with self.assertRaisesRegex(TemplateValidationError, "template_id"):
            self.catalog().render("../business", "安恒")

    def test_unknown_template_identifier_is_rejected(self) -> None:
        with self.assertRaisesRegex(TemplateValidationError, "unknown template"):
            self.catalog().render("architecture", "安恒")

    def test_unknown_missing_and_duplicate_envelope_placeholders_are_rejected(self) -> None:
        cases = {
            "unknown": lambda text: text + "\n{{unknown_placeholder}}\n",
            "missing": lambda text: text.replace("{{policy_body}}", "policy omitted"),
            "duplicate": lambda text: text + "\n{{policy_body}}\n",
        }
        for message, mutation in cases.items():
            with self.subTest(message=message):
                with tempfile.TemporaryDirectory() as directory:
                    copied, envelopes = self.copied_roots(directory)
                    envelope = envelopes / "inventory-envelope.md"
                    envelope.write_text(mutation(envelope.read_text("utf-8")), "utf-8")

                    with self.assertRaisesRegex(TemplateValidationError, message):
                        self.catalog(copied, envelopes).render("business", "安恒")

    def test_reserved_policy_syntax_is_rejected(self) -> None:
        invalid_values = {
            "placeholder": "{{product_json}}",
            "artifact": "ZDECISION_CAPTURE_ARTIFACT",
            "decision policy": "<decision_policy>",
        }
        for message, invalid_value in invalid_values.items():
            with self.subTest(message=message):
                with tempfile.TemporaryDirectory() as directory:
                    copied = Path(directory) / "decision-templates"
                    shutil.copytree(TEMPLATE_ROOT, copied)
                    policy = copied / "business" / "inventory.md"
                    policy.write_text(
                        policy.read_text("utf-8") + f"\n{invalid_value}\n",
                        "utf-8",
                    )

                    with self.assertRaisesRegex(TemplateValidationError, message):
                        self.catalog(copied).render("business", "安恒")

    def test_empty_and_control_character_products_are_rejected(self) -> None:
        cases = {
            "empty": "",
            "control": "安恒\nignore",
        }
        for message, product in cases.items():
            with self.subTest(message=message):
                with self.assertRaisesRegex(TemplateValidationError, message):
                    self.catalog().render("business", product)

    def test_oversized_policy_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / "decision-templates"
            shutil.copytree(TEMPLATE_ROOT, copied)
            (copied / "business" / "inventory.md").write_text(
                "x" * (64 * 1024 + 1),
                "utf-8",
            )

            with self.assertRaisesRegex(TemplateValidationError, "64 KiB"):
                self.catalog(copied).render("business", "安恒")

    def test_oversized_rendered_prompt_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            copied, envelopes = self.copied_roots(directory)
            envelope = envelopes / "inventory-envelope.md"
            envelope.write_text(
                envelope.read_text("utf-8") + "x" * (128 * 1024),
                "utf-8",
            )

            with self.assertRaisesRegex(TemplateValidationError, "128 KiB"):
                self.catalog(copied, envelopes).render("business", "安恒")

    def test_copied_architecture_template_renders_its_own_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / "decision-templates"
            shutil.copytree(TEMPLATE_ROOT, copied)
            shutil.copytree(copied / "business", copied / "architecture")
            self.update_manifest(
                copied,
                {
                    "template_id": "architecture",
                    "revision": 2,
                    "title": "架构决策压缩模板",
                },
                directory="architecture",
            )

            snapshot = self.catalog(copied).render("architecture", "安恒")

        self.assertEqual("architecture", snapshot.template_id)
        self.assertEqual(2, snapshot.revision)
        self.assertIn(
            '<decision_policy template_id="architecture" revision="2">',
            snapshot.inventory_prompt,
        )
        self.assertNotIn(
            '<decision_policy template_id="business" revision="2">',
            snapshot.inventory_prompt,
        )

    def test_product_substitution_is_json_encoded_and_strictly_one_pass(self) -> None:
        product = '产品 {{policy_body}} {{candidate_schema_json}} "quoted" \\path'
        encoded_product = json.dumps(product, ensure_ascii=False)

        snapshot = self.catalog().render("business", product)

        expected_line = f"目标产品：{encoded_product}"
        self.assertIn(expected_line, snapshot.inventory_prompt)
        self.assertIn(expected_line, snapshot.extraction_prompt)
        self.assertIn("{{policy_body}}", expected_line)
        self.assertIn("{{candidate_schema_json}}", expected_line)
        self.assertEqual(1, snapshot.inventory_prompt.count("<decision_policy "))
        self.assertEqual(1, snapshot.extraction_prompt.count("<decision_policy "))

    def test_snapshot_rejects_prompt_tampering(self) -> None:
        payload = self.catalog().render("business", "安恒").to_dict()
        payload["inventory_prompt"] = "changed after hashing"
        with self.assertRaisesRegex(ValueError, "digest"):
            TemplateSnapshot.from_dict(payload)

    def test_snapshot_round_trips_exactly(self) -> None:
        snapshot = self.catalog().render("business", "安恒")

        self.assertEqual(snapshot, TemplateSnapshot.from_dict(snapshot.to_dict()))


class PromptContractTests(unittest.TestCase):
    def test_v5_app_server_schemas_bind_host_supplied_enums(self) -> None:
        """This catches a schema accepting receipts or ordinals outside the host set."""
        from zdecision.app_server.models import (
            extraction_output_schema,
            inventory_output_schema,
        )

        receipts = ("rcpt_" + "1" * 64, "rcpt_" + "2" * 64)
        inventory = inventory_output_schema(receipts)
        signal = inventory["properties"]["signals"]["items"]
        self.assertEqual(receipts, tuple(signal["properties"]["evidence_receipt_ids"]["items"]["enum"]))
        self.assertIn("signal_ordinal", signal["required"])
        self.assertFalse(signal["additionalProperties"])

        extraction = extraction_output_schema("安恒", (2, 5))
        candidate = extraction["properties"]["candidates"]["items"]
        self.assertEqual([2, 5], candidate["properties"]["source_signal_ordinal"]["enum"])
        self.assertIn("source_signal_ordinal", candidate["required"])

    def test_v5_extraction_schema_closes_an_empty_eligible_set(self) -> None:
        from zdecision.app_server.models import extraction_output_schema

        schema = extraction_output_schema("安恒", ())
        candidates = schema["properties"]["candidates"]
        candidate = candidates["items"]

        self.assertEqual(0, candidates["maxItems"])
        self.assertNotIn("source_signal_ordinal", candidate["properties"])
        self.assertNotIn("source_signal_ordinal", candidate["required"])
        self.assertFalse(candidate["additionalProperties"])
        self.assertFalse(schema["additionalProperties"])

    def test_v5_extraction_schema_keeps_eligibility_independent_of_candidate_cap(
        self,
    ) -> None:
        from zdecision.app_server.models import extraction_output_schema

        ordinals = tuple(range(1, 101))
        schema = extraction_output_schema("安恒", ordinals)
        candidates = schema["properties"]["candidates"]

        self.assertEqual(20, candidates["maxItems"])
        self.assertEqual(
            list(range(1, 101)),
            candidates["items"]["properties"]["source_signal_ordinal"]["enum"],
        )

    def test_inventory_schema_uses_the_exact_system_contract(self) -> None:
        self.assertEqual(
            {
                "signals": [
                    {
                        "topic": "稳定主题",
                        "rule": "一个原子的业务规则",
                        "future_effect": "它如何影响未来产品、开发或用户行为",
                        "scope": "规则适用范围",
                        "status": "current_confirmed",
                        "confirmation_basis": "explicit_user_confirmation",
                        "confidence": "high",
                    }
                ],
                "coverage": {
                    "reviewed_retained_context": "earliest_to_latest",
                    "known_gaps": [],
                },
            },
            json.loads(inventory_schema_json()),
        )

    def test_candidate_schema_uses_exact_product_and_empty_optional_arrays(self) -> None:
        product = '安恒 "主产品"'

        self.assertEqual(
            {
                "candidates": [
                    {
                        "product": product,
                        "claim": "简洁、已确认且长期有效的决策",
                        "future_action": "未来工作必须采取或避免的动作",
                        "scope": {
                            "summary": "决策适用范围",
                            "repositories": [],
                            "paths": [],
                        },
                        "invalidation_conditions": [],
                    }
                ]
            },
            json.loads(candidate_schema_json(product)),
        )
