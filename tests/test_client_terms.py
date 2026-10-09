#!/usr/bin/env python3
"""合成した辞書で、顧客語の照合規則を木・履歴・公開テキストの入口から検査する。

実在の顧客名はテストにも置かない。標準ライブラリだけで run_all.py から実行でき、
pytest からも同じテストケースを収集できる。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "engine"))
# engine/ を sys.path に挿した後でしか解決できないため、import の順序だけを抑制する。
import abstraction_gate  # noqa: E402
import release_gate  # noqa: E402


TERMS = '''[clients]
names = ["架空商会", "fixture.example", "syntheticbrand"]
words = ["acme", "UV Advisory", "A+B"]
words_cs = ["GLOW", "Q-MAKE"]

[products]
names = ["fixture-product"]
words = ["widget"]
words_cs = ["GADGET"]
'''


class ClientTermsTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.registry = self.root / "registry"
        self.registry.mkdir()
        self.terms = self.registry / "private_terms.toml"
        self.terms.write_text(TERMS, encoding="utf-8")
        self.target = self.root / "text.md"
        self.addCleanup(patch.stopall)
        patch.object(release_gate, "REGISTRY", self.registry).start()
        patch.object(abstraction_gate, "TERMS", self.terms).start()
        self.specs, self.is_rx = release_gate.load_pattern_file(
            "private_terms.toml", ["clients"])

    def scan(self, body: str) -> list[tuple[str, int, list[release_gate.Match]]]:
        self.target.write_text(body, encoding="utf-8")
        return release_gate._scan_specs(
            [(self.target.name, self.target)], self.specs, not self.is_rx, {},
            collect_all=True)

    def assert_hit(self, body: str, label: str) -> None:
        hits = self.scan(body)
        self.assertEqual([(shown, count) for shown, count, _ in hits], [(label, 1)])
        self.assertEqual(hits[0][2][0].body, body)

    def text_gate(self, body: str) -> subprocess.CompletedProcess[str]:
        self.target.write_text(body, encoding="utf-8")
        return subprocess.run(
            [sys.executable, str(ROOT / "engine" / "deliverable_gate.py"),
             "--text", str(self.target)],
            env={**os.environ, "WORKOS_REGISTRY": str(self.registry)},
            capture_output=True, text=True, timeout=30)

    def test_cjk_substring(self) -> None:
        self.assert_hit("前置き架空商会向け", "clients.names:架空商会")

    def test_domain_is_literal_and_case_insensitive(self) -> None:
        self.assert_hit("https://sub.FIXTURE.EXAMPLE/path", "clients.names:fixture.example")
        self.assertEqual(self.scan("fixtureXexample"), [])

    def test_names_keep_substring_matching(self) -> None:
        self.assert_hit("preSYNTHETICBRANDpost", "clients.names:syntheticbrand")

    def test_words_reject_adjacent_ascii_letters_and_digits(self) -> None:
        for body in ("acmeified", "preacme", "ACME9", "9ACME", "xacmex"):
            with self.subTest(body=body):
                self.assertEqual(self.scan(body), [])

    def test_digit_prefix_is_not_a_word_boundary(self) -> None:
        # 数字も ASCII 英数字に含む。番号と直結する顧客識別子は names に明示する。
        self.assertEqual(self.scan("12acme"), [])
        self.assertEqual(self.scan("第12acmeビル"), [])
        self.assert_hit("12-acme", "clients.words:acme")

    def test_words_match_domains_and_uppercase(self) -> None:
        for body in ("acme.co.jp", "ACME", "acme"):
            with self.subTest(body=body):
                self.assert_hit(body, "clients.words:acme")

    def test_ascii_boundaries_include_punctuation_and_non_ascii(self) -> None:
        for body in ("_acme_", "-acme-", "前acme後", "éacmeé", "Kacmeſ", "１acme２"):
            with self.subTest(body=body):
                self.assert_hit(body, "clients.words:acme")

    def test_word_phrases_and_symbols_are_literal(self) -> None:
        self.assert_hit("uv advisory.co.jp", "clients.words:UV Advisory")
        self.assert_hit("a+b", "clients.words:A+B")
        for body in ("UV  Advisory", "UV Advisory9", "AAAB", "AAB"):
            with self.subTest(body=body):
                self.assertEqual(self.scan(body), [])

    def test_words_cs_require_exact_case(self) -> None:
        for body in ("glow", "Glow", "gLoW", "q-make", "Q-make"):
            with self.subTest(body=body):
                self.assertEqual(self.scan(body), [])
        self.assert_hit("GLOW", "clients.words_cs:GLOW")
        self.assert_hit("Q-MAKE", "clients.words_cs:Q-MAKE")

    def test_words_cs_require_ascii_boundaries(self) -> None:
        for body in ("GLOWING", "AGLOW", "9GLOW", "GLOW9", "xQ-MAKE"):
            with self.subTest(body=body):
                self.assertEqual(self.scan(body), [])
        self.assert_hit("前_GLOW-後", "clients.words_cs:GLOW")

    def test_reporting_keeps_table_keys_and_terms(self) -> None:
        hits = self.scan("架空商会\nACME ACME\nGLOW")
        self.assertEqual({label: n for label, n, _ in hits}, {
            "clients.names:架空商会": 1, "clients.words:acme": 2,
            "clients.words_cs:GLOW": 1,
        })
        self.assertEqual([m.line for _, _, ms in hits for m in ms], [2, 2, 1, 3])
        status, detail, _ = release_gate.obs_pattern_absent(self.root, {
            "patterns_from": "private_terms.toml", "categories": ["clients"],
        }, {"files": [(self.target.name, self.target)], "lanes_cfg": {}})
        self.assertEqual(status, "fail")
        for label in ("clients.names: 架空商会", "clients.words: acme", "clients.words_cs: GLOW"):
            self.assertIn(label, detail)

    def test_legacy_consumer_loads_all_values_as_literals(self) -> None:
        self.assertFalse(self.is_rx)
        expected = [
            "架空商会", "fixture.example", "syntheticbrand", "acme", "UV Advisory", "A+B",
            "GLOW", "Q-MAKE", "fixture-product", "widget", "GADGET",
        ]
        self.assertEqual(abstraction_gate.load_terms(), expected)
        with patch("workos.tomllib", None):
            self.assertEqual(abstraction_gate.load_terms(), expected)
        self.assertIn(("clients.words", "acme"), self.specs)
        self.assertIn(("clients.words_cs", "GLOW"), self.specs)
        self.assertFalse(any(label.startswith("products.") for label, _ in self.specs))

    def test_other_tables_keep_literal_substring_semantics(self) -> None:
        self.specs, self.is_rx = release_gate.load_pattern_file("private_terms.toml", ["products"])
        self.assert_hit("preWIDGETpost", "products.words:widget")
        self.assert_hit("pregadgetpost", "products.words_cs:GADGET")

    def test_regex_table_keeps_regex_semantics(self) -> None:
        (self.registry / "patterns.toml").write_text(
            "[patterns]\n'clients.words_cs' = 'a.c'\n", encoding="utf-8")
        self.specs, self.is_rx = release_gate.load_pattern_file("patterns.toml")
        self.assertTrue(self.is_rx)
        self.assert_hit("xabcx", "clients.words_cs")
        self.assertEqual(self.scan("ABC"), [])

    def test_text_gate_rejects_each_client_mode(self) -> None:
        for body, term in (("前架空商会後", "架空商会"),
                           ("fixture.example", "fixture.example"),
                           ("ACME.co.jp", "acme"), ("GLOW", "GLOW")):
            with self.subTest(body=body):
                result = self.text_gate(body)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(term, result.stdout)

    def test_text_gate_accepts_nonmatches_and_products(self) -> None:
        result = self.text_gate("acmeified glow fixture-product widget GADGET")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("顧客名なし", result.stdout)

    def test_history_uses_the_same_boundaries_and_case(self) -> None:
        repo = self.root / "repo"
        repo.mkdir()
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
               "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}

        def git(*args: str) -> None:
            subprocess.run(["git", *args], cwd=repo, env=env, check=True,
                           capture_output=True, timeout=30)

        def commit(body: str, message: str) -> None:
            (repo / "note.md").write_text(body, encoding="utf-8")
            git("add", "note.md")
            git("commit", "-qm", message)

        git("init", "-q")
        commit("acmeified glow", "test: initial")
        chk = {"patterns_from": "private_terms.toml", "categories": ["clients"]}
        self.assertEqual(release_gate.obs_history_pattern_absent(repo, chk, {})[0], "pass")
        commit("ACME.co.jp", "test: GLOW")
        status, _, examples = release_gate.obs_history_pattern_absent(repo, chk, {})
        self.assertEqual(status, "fail")
        self.assertTrue(any("[clients.words]" in example for example in examples))
        self.assertTrue(any("[clients.words_cs]" in example for example in examples))


if __name__ == "__main__":
    unittest.main()
