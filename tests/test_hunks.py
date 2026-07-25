"""Hunk parsing, single-hunk reversal, and the inertness rule."""

from coretexa_verify.hunks import (
    Hunk,
    apply_reverse,
    is_inert,
    parse_hunks,
    python_semantic_fingerprint,
)

DIFF = """diff --git a/m.py b/m.py
index 1111111..2222222 100644
--- a/m.py
+++ b/m.py
@@ -1,5 +1,6 @@
 import os

+VALUE = 3

 def f(x):
     return x
@@ -10,3 +11,3 @@ def g():
-    return 1
+    return 2
 # trailing
"""


def test_parse_hunks_splits_on_headers():
    hunks = parse_hunks(DIFF, "m.py")
    assert len(hunks) == 2
    assert hunks[0].index == 1
    assert hunks[0].head_start == 1 and hunks[0].head_len == 6
    assert hunks[1].header.startswith("@@ -10,3 +11,3 @@")


def test_parse_hunks_separates_base_and_head_sides():
    first = parse_hunks(DIFF, "m.py")[0]
    assert "VALUE = 3\n" in first.head_lines
    assert "VALUE = 3\n" not in first.base_lines
    assert "import os\n" in first.base_lines  # context appears on both sides


HEAD_FILE = """import os

VALUE = 3


def f(x):
    return x
"""


def test_apply_reverse_removes_only_that_hunk():
    hunk = parse_hunks(DIFF, "m.py")[0]
    out = apply_reverse(HEAD_FILE, hunk)
    assert "VALUE = 3" not in out
    assert "def f(x):" in out
    assert "import os" in out


def test_apply_reverse_leaves_the_rest_of_the_file_untouched():
    head = HEAD_FILE + "\n\ndef later():\n    return 99\n"
    out = apply_reverse(head, parse_hunks(DIFF, "m.py")[0])
    assert out.endswith("def later():\n    return 99\n")


# --- inertness --------------------------------------------------------------


def make_hunk(base_lines, head_lines, head_start=1):
    return Hunk(
        path="m.py", index=1, header="@@", base_start=1, base_len=len(base_lines),
        head_start=head_start, head_len=len(head_lines),
        base_lines=base_lines, head_lines=head_lines,
    )


DOC_HEAD = '''"""Module docstring.

Extra explanation added by the PR.
"""

def f():
    return 1
'''


def test_a_docstring_only_change_is_inert():
    hunk = make_hunk(
        base_lines=['"""Module docstring."""\n', "\n"],
        head_lines=['"""Module docstring.\n', "\n", "Extra explanation added by the PR.\n", '"""\n', "\n"],
    )
    inert, reason = is_inert(hunk, DOC_HEAD)
    assert inert is True
    assert "docstring" in reason


COMMENT_HEAD = """def f():
    # a newly added explanatory comment
    return 1
"""


def test_a_comment_only_change_is_inert():
    hunk = make_hunk(
        base_lines=["def f():\n", "    return 1\n"],
        head_lines=["def f():\n", "    # a newly added explanatory comment\n", "    return 1\n"],
    )
    assert is_inert(hunk, COMMENT_HEAD)[0] is True


CODE_HEAD = """def f():
    return 2
"""


def test_a_real_code_change_is_not_inert():
    hunk = make_hunk(base_lines=["def f():\n", "    return 1\n"],
                     head_lines=["def f():\n", "    return 2\n"])
    inert, reason = is_inert(hunk, CODE_HEAD)
    assert inert is False
    assert "parsed program" in reason


def test_unparseable_python_is_treated_as_behavioural():
    hunk = make_hunk(base_lines=["def f(:\n"], head_lines=["def f(:\n"])
    inert, reason = is_inert(hunk, "def f(:\n")
    assert inert is False
    assert "does not parse" in reason


def test_non_python_falls_back_to_a_lexical_comment_check():
    hunk = Hunk(
        path="m.js", index=1, header="@@", base_start=1, base_len=1, head_start=1, head_len=2,
        base_lines=["const a = 1;\n"],
        head_lines=["// explain the constant\n", "const a = 1;\n"],
    )
    assert is_inert(hunk, "// explain the constant\nconst a = 1;\n")[0] is True

    hunk2 = Hunk(
        path="m.js", index=1, header="@@", base_start=1, base_len=1, head_start=1, head_len=1,
        base_lines=["const a = 1;\n"], head_lines=["const a = 2;\n"],
    )
    assert is_inert(hunk2, "const a = 2;\n")[0] is False


def test_semantic_fingerprint_ignores_docstrings_but_not_code():
    a = python_semantic_fingerprint('"""One."""\nx = 1\n')
    b = python_semantic_fingerprint('"""Two, quite different."""\nx = 1\n')
    c = python_semantic_fingerprint('"""One."""\nx = 2\n')
    assert a == b
    assert a != c


def test_semantic_fingerprint_survives_a_function_losing_its_only_docstring():
    a = python_semantic_fingerprint('def f():\n    """Doc."""\n')
    b = python_semantic_fingerprint("def f():\n    pass\n")
    assert a == b


def test_semantic_fingerprint_returns_none_for_broken_source():
    assert python_semantic_fingerprint("def (:\n") is None
