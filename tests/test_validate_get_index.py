from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_incremental_scan import write_source
from tests.test_review_drafts import canonical_record, write_records


PROJECT_ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = PROJECT_ROOT / "validate_get_index.py"


class FullIndexValidatorTests(unittest.TestCase):
    def test_cli_validates_a_caller_supplied_temporary_vault(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "vault"
            source = write_source(
                vault,
                "GET NOTES/Notes/2026-09-15/source.md",
                uid="uid-source",
                title="source",
            )
            records = root / "records.jsonl"
            write_records(
                records,
                [canonical_record(source, vault, uid="uid-source", title="source")],
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(VALIDATOR),
                    "--vault-root",
                    str(vault),
                    str(records),
                ],
                text=True,
                capture_output=True,
                check=False,
                timeout=5,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("records=1", result.stdout)
            self.assertIn("PASS full GET source-index validation", result.stdout)

    def test_cli_accepts_query_stripped_titles_without_leaking_queries(self) -> None:
        cases = (
            (
                "uppercase-scheme",
                "reference HTTPS://example.com/page?xsec_token=PRIVATE-UPPER#section",
                "reference https://example.com/page#section",
                "PRIVATE-UPPER",
            ),
            (
                "parenthesized-query",
                "https://example.com/a?token=(PRIVATE-PAREN-QUERY)",
                "https://example.com/a",
                "PRIVATE-PAREN-QUERY",
            ),
            (
                "parenthesized-path",
                "https://example.com/a(b)?token=PRIVATE-PAREN-PATH",
                "https://example.com/a(b)",
                "PRIVATE-PAREN-PATH",
            ),
            (
                "bracketed-parameter",
                "Http://example.com/a?token[]=PRIVATE-BRACKET",
                "http://example.com/a",
                "PRIVATE-BRACKET",
            ),
        )
        for case_name, source_title, canonical_title, private_query in cases:
            with self.subTest(case=case_name), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                vault = root / "vault"
                source = write_source(
                    vault,
                    "GET NOTES/Notes/2026-09-15/source.md",
                    uid="uid-source",
                    title=source_title,
                )
                records = root / "records.jsonl"
                write_records(
                    records,
                    [
                        canonical_record(
                            source,
                            vault,
                            uid="uid-source",
                            title=canonical_title,
                        )
                    ],
                )

                result = subprocess.run(
                    [
                        sys.executable,
                        str(VALIDATOR),
                        "--vault-root",
                        str(vault),
                        str(records),
                    ],
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=5,
                )

                self.assertEqual(result.returncode, 0)
                self.assertFalse(
                    private_query in result.stdout or private_query in result.stderr,
                    "validator output leaked source query data",
                )
                self.assertIn("PASS full GET source-index validation", result.stdout)

    def test_cli_rejects_unsanitized_title_without_leaking_query(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "vault"
            private_query = "PRIVATE-UNSANITIZED"
            source_title = f"HTTPS://example.com/a(b)?token={private_query}"
            source = write_source(
                vault,
                "GET NOTES/Notes/2026-09-15/source.md",
                uid="uid-source",
                title=source_title,
            )
            records = root / "records.jsonl"
            write_records(
                records,
                [canonical_record(source, vault, uid="uid-source", title=source_title)],
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(VALIDATOR),
                    "--vault-root",
                    str(vault),
                    str(records),
                ],
                text=True,
                capture_output=True,
                check=False,
                timeout=5,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(
                private_query in result.stdout or private_query in result.stderr,
                "validator output leaked source query data",
            )


if __name__ == "__main__":
    unittest.main()
