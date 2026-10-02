"""Self-tests for the stdlib standards checker: each rule fires and passes."""

from __future__ import annotations

import textwrap
import unittest

from tools import model, standards

GOOD = textwrap.dedent('''\
    """Module summary."""

    from __future__ import annotations


    def add(left: int, right: int) -> int:
        """Add two numbers.

        Args:
            left: First addend.
            right: Second addend.

        Returns:
            The sum.
        """
        return left + right


    class Box:
        """A box."""

        def size(self) -> int:
            """Return the size."""
            return 1
    ''')


def rules(source: str, rel: str = "muninn/sample.py") -> set[str]:
    """Return the rule ids that fire for one source text.

    Args:
        source: Python source to check.
        rel: Repository-relative path to pretend the source lives at.

    Returns:
        The set of violated rule ids.
    """
    return {v.rule for v in standards.check_source(rel, source)}


def with_body(body: str) -> str:
    """Wrap a function body in an otherwise compliant module.

    Args:
        body: Indented statements for ``def f() -> None``.

    Returns:
        Module source.
    """
    head = '"""Doc."""\n\nfrom __future__ import annotations\n\n\n'
    return head + 'def f() -> None:\n    """Do it."""\n' + body


class CleanSourceTest(unittest.TestCase):
    """A compliant module raises nothing."""

    def test_good_module_has_no_violations(self) -> None:
        self.assertEqual(rules(GOOD), set())

    def test_the_limits_have_not_been_loosened(self) -> None:
        self.assertLessEqual(model.MAX_FILE_LINES, 400)
        self.assertLessEqual(model.MAX_FUNCTION_LINES, 100)


class SizeRulesTest(unittest.TestCase):
    """S1 and S2."""

    def test_file_over_the_limit_is_s1(self) -> None:
        body = "".join(f"X{n} = {n}\n" for n in range(model.MAX_FILE_LINES))
        self.assertIn("S1", rules(GOOD + body))

    def test_file_at_the_limit_passes(self) -> None:
        used = len([ln for ln in GOOD.splitlines() if ln.strip()])
        body = "".join(f"X{n} = {n}\n" for n in range(400 - used))
        self.assertNotIn("S1", rules(GOOD + body))

    def test_long_function_is_s2(self) -> None:
        lines = "".join(f"    x{n} = {n}\n" for n in range(100))
        self.assertIn("S2", rules(with_body(lines)))

    def test_function_at_the_limit_passes(self) -> None:
        lines = "".join(f"    x{n} = {n}\n" for n in range(97))
        self.assertNotIn("S2", rules(with_body(lines)))


class DocstringRulesTest(unittest.TestCase):
    """S3 and S4."""

    def test_missing_docstrings_are_s3(self) -> None:
        future = "from __future__ import annotations\n\n\n"
        self.assertIn("S3", rules(future + "class A:\n    pass\n"))
        self.assertIn("S3", rules(future + "def f() -> None:\n    pass\n"))
        self.assertIn("S3", rules("X = 1\n"))

    def test_closures_need_no_docstring(self) -> None:
        body = "    def inner() -> None:\n        pass\n\n    inner()\n"
        self.assertNotIn("S3", rules(with_body(body)))

    def test_tests_are_exempt_for_test_methods_and_hooks(self) -> None:
        source = (
            '"""Doc."""\n\nfrom __future__ import annotations\n\n\n'
            'class T:\n    """Doc."""\n\n'
            "    def test_x(self) -> None:\n        pass\n\n"
            "    def setUp(self) -> None:\n        pass\n\n"
            "    def helper(self) -> None:\n        pass\n"
        )
        found = standards.check_source("tests/test_x.py", source)
        self.assertEqual([v.message for v in found], ["helper: no docstring"])

    def test_summary_must_end_with_punctuation(self) -> None:
        self.assertIn("S4", rules(GOOD.replace("Add two numbers.", "Add")))

    def test_blank_line_after_the_summary_is_required(self) -> None:
        bad = GOOD.replace("Add two numbers.\n\n", "Add two numbers.\n")
        self.assertIn("S4", rules(bad))

    def test_non_google_section_names_are_s4(self) -> None:
        bad = GOOD.replace("Args:", "Parameters:")
        self.assertIn("S4", rules(bad))


class CommentRulesTest(unittest.TestCase):
    """S5: labels that only make sense inside the conversation."""

    def test_internal_labels_are_s5(self) -> None:
        labels = ("design " + "3.5", "spec " + "O7", "WU" + "4", "(A" + "3)")
        for label in labels:
            with self.subTest(label=label):
                source = GOOD + f"# the rule, per {label}\n"
                self.assertIn("S5", rules(source))

    def test_plain_comments_and_pr_numbers_pass(self) -> None:
        source = GOOD + "# Keep the lock until the commit (see PR #12).\n"
        self.assertNotIn("S5", rules(source))


class ImportRulesTest(unittest.TestCase):
    """S6."""

    def test_lazy_import_is_s6(self) -> None:
        self.assertIn("S6", rules(with_body("    import os\n")))

    def test_star_import_is_s6(self) -> None:
        self.assertIn("S6", rules(GOOD + "from os import *\n"))

    def test_type_checking_is_s6(self) -> None:
        source = GOOD + "TYPE_CHECKING = False\nif TYPE_CHECKING:\n    pass\n"
        self.assertIn("S6", rules(source))

    def test_missing_future_import_is_s6(self) -> None:
        self.assertIn("S6", rules('"""Doc."""\n\nX = 1\n'))


class SignatureRulesTest(unittest.TestCase):
    """S7 and S8."""

    def test_missing_annotations_are_s7(self) -> None:
        bad = GOOD.replace("left: int", "left")
        self.assertIn("S7", rules(bad))
        self.assertIn("S7", rules(GOOD.replace(") -> int:", "):")))

    def test_mutable_default_is_s8(self) -> None:
        bad = GOOD.replace("right: int", "right: list[int] = []")
        self.assertIn("S8", rules(bad))


if __name__ == "__main__":
    unittest.main()
