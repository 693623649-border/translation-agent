from __future__ import annotations

import unittest
from pathlib import Path

from product_contracts import (
    CONTRACT_SCHEMA_VERSION,
    ArtifactRecord,
    ContractError,
    RunEvent,
    RunSpec,
)


class ProductContractTests(unittest.TestCase):
    def test_run_spec_round_trips_as_strict_json_data(self) -> None:
        spec = RunSpec(
            source="book.pdf",
            source_mode="text-pdf",
            output_dir="outputs/book",
            recipe="recipes/text-pdf-full-publication.toml",
            targets=("publication.report",),
            options={"text_pdf_reflow": True},
        )

        restored = RunSpec.from_dict(spec.to_dict())

        self.assertEqual(restored, spec)
        self.assertEqual(restored.schema_version, CONTRACT_SCHEMA_VERSION)

    def test_contracts_reject_unknown_fields_and_raw_source_modes(self) -> None:
        payload = RunSpec().to_dict()
        payload["api_key"] = "must-never-be-serialized"
        with self.assertRaisesRegex(ContractError, "unknown fields.*api_key"):
            RunSpec.from_dict(payload)
        with self.assertRaisesRegex(ContractError, "unsupported source_mode"):
            RunSpec(source_mode="auto")

    def test_options_are_detached_json_and_reject_credentials(self) -> None:
        raw = {"nested": [1, {"enabled": True}]}
        spec = RunSpec(options=raw)
        raw["nested"][1]["enabled"] = False
        self.assertTrue(spec.to_dict()["options"]["nested"][1]["enabled"])
        with self.assertRaisesRegex(ContractError, "credential"):
            RunSpec(options={"provider": {"api_key": "must-not-persist"}})
        with self.assertRaisesRegex(ContractError, "must contain JSON"):
            RunSpec(options={"glossary": Path("glossary.json")})
        with self.assertRaisesRegex(ContractError, "NaN"):
            RunSpec(options={"temperature": float("nan")})

    def test_artifact_and_event_are_json_safe(self) -> None:
        artifact = ArtifactRecord(
            schema_version=1,
            name="book.docx",
            kind="publication.docx",
            path="outputs/book/book.docx",
            status="released",
            sha256="a" * 64,
        )
        event = RunEvent(
            schema_version=1,
            run_id="run-1",
            event="node_succeeded",
            timestamp="2026-08-14T00:00:00Z",
            data={"artifact": artifact.to_dict()},
        )

        self.assertEqual(
            ArtifactRecord.from_dict(artifact.to_dict()),
            artifact,
        )
        self.assertEqual(event.to_dict()["data"]["artifact"]["status"], "released")


if __name__ == "__main__":
    unittest.main()
