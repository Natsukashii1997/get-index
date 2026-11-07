from __future__ import annotations

import unittest

from get_index_render import (
    IndexRenderError,
    render_index_pages,
    source_target,
    validate_index_pages,
)


def record(relative_path: str, uid: str, *, domain: str) -> dict:
    return {
        "relative_path": relative_path,
        "uid": uid,
        "title": uid,
        "created_date": "2026-09-15",
        "note_type": "recorder_audio",
        "domain": domain,
        "sensitivity": "普通",
        "tags": ["项目与流程"],
        "review_state": "规则初分",
        "basis": "metadata",
        "flag": "none",
    }


class IndexRenderTests(unittest.TestCase):
    def test_source_targets_reject_obsidian_link_syntax_and_control_characters(
        self,
    ) -> None:
        unsafe_segments = {
            "closing_brackets": "unsafe]]name",
            "alias_separator": "unsafe|name",
            "heading_fragment": "unsafe#name",
            "block_fragment": "unsafe^name",
            "backslash": r"unsafe\name",
            "tab_control": "unsafe\tname",
            "c1_control": "unsafe\x85name",
        }
        for kind, segment in unsafe_segments.items():
            with self.subTest(kind=kind):
                row = record(
                    f"GET NOTES/Notes/2026-09-15/{segment}.md",
                    f"uid-{kind}",
                    domain="生活",
                )

                with self.assertRaises(IndexRenderError):
                    source_target(row)

    def test_render_index_pages_rejects_an_unsafe_source_target(self) -> None:
        rows = [
            record(
                "GET NOTES/Notes/2026-09-15/unsafe#heading.md",
                "uid-unsafe",
                domain="生活",
            )
        ]

        with self.assertRaises(IndexRenderError):
            render_index_pages(rows)

    def test_complete_wikilinks_allow_one_source_target_to_prefix_another(self) -> None:
        rows = [
            record(
                "GET NOTES/Notes/2026-09-15/source.md",
                "uid-source",
                domain="生活",
            ),
            record(
                "GET NOTES/Notes/2026-09-15/source-extra.md",
                "uid-source-extra",
                domain="工作/个人",
            ),
        ]

        pages = render_index_pages(rows)

        validate_index_pages(pages, rows)


if __name__ == "__main__":
    unittest.main()
