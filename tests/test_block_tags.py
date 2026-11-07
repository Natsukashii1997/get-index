import tempfile
import unittest
from pathlib import Path
import get_index_incremental as inc

class BlockTagsTests(unittest.TestCase):
    def read(self, block):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"source.md"
            p.write_text("---\nuid: x\n"+block+"\n---\nunchanged body")
            return inc.read_frontmatter(p,"source.md")
    def test_inline_and_block_have_same_hash(self):
        a=self.read('tags: ["录音卡笔记", "产品设计"]')
        b=self.read('tags:\n  - 录音卡笔记\n  - "产品设计"\nhas_transcript: true')
        self.assertEqual(inc.source_tags_sha256(a,"a"),inc.source_tags_sha256(b,"b"))
    def test_quoted_comma_and_apostrophe(self):
        b=self.read("tags:\n  - 'it''s okay'\n  - \"a,b\"")
        self.assertEqual(inc.parse_inline_tags(b["tags"],"b"),["a,b","it's okay"])
    def test_unsafe_structures_rejected(self):
        for block in ['tags:', 'tags:\n  - ', 'tags:\n  - [nested]', 'tags:\n  - key: value','tags:\n  - a\n    - b','tags:\n  - a\ntags: [b]', 'title:\n  - a', 'tags:\n  - &alias x', 'tags:\n  - # comment', 'tags:\n  - -', 'tags:\n  - @reserved', 'tags:\n  - `reserved', 'tags:\n  - ]', 'tags:\n  - ,x', 'tags:\n  - :']:
            with self.subTest(block=block),self.assertRaises(inc.ScanError):self.read(block)

    def test_quoted_forms_have_same_semantics(self):
        for quoted, expected in [("'it''s okay'", "it's okay"), ('"\\u4e2d"', "中"), ('"a,b"', "a,b"), ('"a\\\\b"', "a\\b")]:
            with self.subTest(quoted=quoted):
                inline=self.read('tags: ['+quoted+']')
                block=self.read('tags:\n  - '+quoted)
                self.assertEqual(inc.source_tags_sha256(inline,"a"),inc.source_tags_sha256(block,"b"))
                self.assertEqual(inc.parse_inline_tags(inline["tags"],"a"),[expected])
