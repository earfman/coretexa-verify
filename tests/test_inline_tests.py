"""Sub-file test regions: finding them, and reverting around them.

This is the machinery that lets a Rust file be both the source under test and
the test. Getting a region boundary wrong in one direction destroys the PR's
evidence; getting it wrong in the other suppresses a revert that should have
happened. Both directions are tested here.
"""

from __future__ import annotations

import os
import subprocess

import pytest


from coretexa_verify import inline_tests
from coretexa_verify.hunks import Hunk, file_hunks, read_head_text
from coretexa_verify.inline_tests import (
    classify_hunks,
    code_mask,
    find_regions,
    revert_outside_regions,
    rust_test_regions,
)
from coretexa_verify.models import ChangedFile, Kind


# ==========================================================================
# the code scanner
# ==========================================================================


def masked(text: str) -> str:
    """The text with every non-code character blanked, for readable asserts."""
    return "".join(ch if flag else " " for ch, flag in zip(text, code_mask(text)))


def test_line_and_block_comments_are_not_code():
    src = "let a = 1; // } comment\n/* also } here */ let b = 2;\n"
    out = masked(src)
    assert "comment" not in out and "here" not in out
    assert "let a = 1;" in out and "let b = 2;" in out


def test_block_comments_nest_the_way_rust_says_they_do():
    src = "/* outer /* inner */ still comment */ let a = 1;\n"
    out = masked(src)
    assert "still comment" not in out
    assert "let a = 1;" in out


def test_raw_strings_swallow_quotes_and_braces():
    src = 'let s = r#"a } " brace"#; let t = 1;\n'
    out = masked(src)
    assert "brace" not in out
    assert "let t = 1;" in out


def test_a_lifetime_is_not_a_character_literal():
    """`'a` opens nothing; treating it as a literal would eat the rest of the file."""
    src = "fn f<'a>(x: &'a str) -> &'a str { x }\nfn g() { let c = '}'; }\n"
    out = masked(src)
    assert "fn g()" in out
    # Two real closing braces (one for `f`, one for `g`); the one inside the
    # `'}'` character literal is masked out and must not close a region.
    assert out.count("}") == 2
    assert "'}'" not in out


def test_escaped_character_literals_are_handled():
    src = "let n = '\\n'; let b = '}';\nlet real = 1;\n"
    assert "let real = 1;" in masked(src)


# ==========================================================================
# region detection
# ==========================================================================


def test_a_cfg_test_module_is_a_region():
    src = (
        "pub fn add(a: i32) -> i32 { a + 1 }\n"   # 1
        "\n"                                       # 2
        "#[cfg(test)]\n"                          # 3
        "mod tests {\n"                           # 4
        "    use super::*;\n"                     # 5
        "    #[test]\n"                           # 6
        "    fn works() { assert_eq!(add(1), 2); }\n"  # 7
        "}\n"                                     # 8
    )
    assert rust_test_regions(src) == [(3, 8)]


def test_a_brace_in_a_string_does_not_close_the_region_early():
    src = (
        "#[cfg(test)]\n"
        "mod tests {\n"
        '    const S: &str = "}";\n'
        "    #[test]\n"
        "    fn works() {}\n"
        "}\n"
        "pub fn real_source() {}\n"
    )
    regions = rust_test_regions(src)
    assert regions == [(1, 6)]
    # Line 7 is real source and must stay revertable.
    assert not any(lo <= 7 <= hi for lo, hi in regions)


def test_cfg_all_and_cfg_any_still_count_as_test_regions():
    src = "#[cfg(all(test, unix))]\nmod a { }\n#[cfg(any(test, feature = \"x\"))]\nmod b { }\n"
    assert rust_test_regions(src) == [(1, 4)]


def test_cfg_not_test_is_production_code_and_is_never_a_region():
    """`#[cfg(not(test))]` marks code compiled only when *not* testing."""
    src = "#[cfg(not(test))]\nmod real { }\n"
    assert rust_test_regions(src) == []


def test_a_feature_named_test_something_is_not_a_test_region():
    src = '#[cfg(feature = "test-util")]\nmod helpers { }\n'
    assert rust_test_regions(src) == []


def test_a_bare_test_attribute_on_a_function_is_a_region():
    src = "#[test]\nfn alpha() {\n    assert!(true);\n}\npub fn real() {}\n"
    assert rust_test_regions(src) == [(1, 4)]


def test_a_cfg_test_use_statement_ends_at_its_semicolon():
    src = "#[cfg(test)]\nuse std::io::Write;\npub fn real() {}\n"
    assert rust_test_regions(src) == [(1, 2)]


def test_attributes_stacked_above_the_cfg_are_pulled_into_the_region():
    src = "#[allow(dead_code)]\n#[cfg(test)]\nmod tests {\n}\n"
    assert rust_test_regions(src) == [(1, 4)]


def test_a_file_with_no_test_code_has_no_regions():
    assert rust_test_regions("pub fn add(a: i32) -> i32 { a + 1 }\n") == []


def test_an_unterminated_region_is_dropped_rather_than_run_to_end_of_file():
    """A region that is too big would suppress reverts that ought to happen."""
    assert rust_test_regions("#[cfg(test)]\nmod tests {\n    fn x() {\n") == []


def test_only_rust_files_get_regions():
    src = "#[cfg(test)]\nmod tests {\n}\n"
    assert find_regions("a.rs", src) == [(1, 3)]
    assert find_regions("a.go", src) == []
    assert find_regions("a.py", src) == []


# ==========================================================================
# hunk classification
# ==========================================================================


def hunk(index: int, head_start: int, head_len: int) -> Hunk:
    return Hunk(
        path="a.rs", index=index, header="@@", base_start=1, base_len=1,
        head_start=head_start, head_len=head_len, base_lines=[], head_lines=[],
    )


def test_a_hunk_outside_every_region_is_revertable():
    revertable, kept = classify_hunks([hunk(1, 1, 3)], [(10, 20)])
    assert len(revertable) == 1 and kept == []


def test_a_hunk_inside_a_region_is_kept_at_head():
    revertable, kept = classify_hunks([hunk(1, 12, 3)], [(10, 20)])
    assert revertable == [] and len(kept) == 1
    assert "the PR's own test" in kept[0][1]


def test_a_straddling_hunk_is_kept_at_head_and_says_so():
    """We cannot tell which base lines belong to which half, so we never guess."""
    revertable, kept = classify_hunks([hunk(1, 8, 6)], [(10, 20)])
    assert revertable == []
    assert "straddles" in kept[0][1]


def test_a_pure_deletion_is_judged_by_where_it_would_be_reinserted():
    inside = classify_hunks([hunk(1, 15, 0)], [(10, 20)])
    outside = classify_hunks([hunk(1, 30, 0)], [(10, 20)])
    assert inside[0] == [] and len(inside[1]) == 1
    assert len(outside[0]) == 1 and outside[1] == []


# ==========================================================================
# the revert itself, against a real git repository
# ==========================================================================


def git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )


@pytest.fixture()
def rust_repo(tmp_path):
    """A repo whose one file gains both a function and a test for it."""
    repo = str(tmp_path)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")

    src = os.path.join(repo, "src")
    os.makedirs(src)
    base = (
        "pub fn add(a: i32) -> i32 {\n"
        "    a + 1\n"
        "}\n"
        "\n"
        "#[cfg(test)]\n"
        "mod tests {\n"
        "    use super::*;\n"
        "\n"
        "    #[test]\n"
        "    fn add_works() {\n"
        "        assert_eq!(add(1), 2);\n"
        "    }\n"
        "}\n"
    )
    with open(os.path.join(src, "lib.rs"), "w") as fh:
        fh.write(base)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    base_sha = git(repo, "rev-parse", "HEAD").stdout.strip()

    head = (
        "pub fn add(a: i32) -> i32 {\n"
        "    a + 1\n"
        "}\n"
        "\n"
        "pub fn double(a: i32) -> i32 {\n"
        "    a * 2\n"
        "}\n"
        "\n"
        "#[cfg(test)]\n"
        "mod tests {\n"
        "    use super::*;\n"
        "\n"
        "    #[test]\n"
        "    fn add_works() {\n"
        "        assert_eq!(add(1), 2);\n"
        "    }\n"
        "\n"
        "    #[test]\n"
        "    fn double_works() {\n"
        "        assert_eq!(double(2), 4);\n"
        "    }\n"
        "}\n"
    )
    with open(os.path.join(src, "lib.rs"), "w") as fh:
        fh.write(head)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "head")
    head_sha = git(repo, "rev-parse", "HEAD").stdout.strip()
    return repo, base_sha, head_sha


def test_the_revert_removes_the_new_function_and_keeps_the_new_test(rust_repo):
    """The state the whole experiment needs: base source, head tests."""
    repo, base_sha, head_sha = rust_repo
    head_text = read_head_text(repo, head_sha, "src/lib.rs")
    regions = rust_test_regions(head_text)
    text, notes = revert_outside_regions(
        repo, base_sha, head_sha, "src/lib.rs", head_text, regions
    )
    assert text is not None
    # The source the PR added is gone...
    assert "pub fn double" not in text
    # ...but the test that exercises it is still there, which is what will make
    # the compile fail and produce GATE_HOLDS_BUILD.
    assert "fn double_works" in text
    assert "fn add_works" in text
    assert any("non-test hunk(s) reverted to base" in n for n in notes)


def test_annotate_marks_the_file_as_both_source_and_test(rust_repo):
    repo, base_sha, head_sha = rust_repo
    changed = [
        ChangedFile(
            path="src/lib.rs", status="M", kind=Kind.SOURCE, reason="not a test path"
        )
    ]
    notes = inline_tests.annotate(repo, base_sha, head_sha, changed, "rust")
    assert changed[0].has_inline_tests
    assert changed[0].executable_test
    assert "both the source under test and the test itself" in changed[0].reason
    assert notes and "contains the PR's own tests inline" in notes[0]


def test_annotate_ignores_rust_files_when_the_runner_is_not_rust(rust_repo):
    """sqlfluff vendors a Rust parser; its .rs files are not pytest targets."""
    repo, base_sha, head_sha = rust_repo
    changed = [
        ChangedFile(path="src/lib.rs", status="M", kind=Kind.SOURCE, reason="x")
    ]
    assert inline_tests.annotate(repo, base_sha, head_sha, changed, "python") == []
    assert not changed[0].has_inline_tests
    assert not changed[0].executable_test


def test_a_file_whose_inline_tests_the_pr_did_not_touch_stays_plain_source(tmp_path):
    repo = str(tmp_path)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    os.makedirs(os.path.join(repo, "src"))
    body = "#[cfg(test)]\nmod tests {\n    #[test]\n    fn t() {}\n}\n"
    with open(os.path.join(repo, "src/lib.rs"), "w") as fh:
        fh.write("pub fn a() {}\n" + body)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    base_sha = git(repo, "rev-parse", "HEAD").stdout.strip()
    with open(os.path.join(repo, "src/lib.rs"), "w") as fh:
        fh.write("pub fn a() { let _ = 1; }\n" + body)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "head")
    head_sha = git(repo, "rev-parse", "HEAD").stdout.strip()

    changed = [ChangedFile(path="src/lib.rs", status="M", kind=Kind.SOURCE, reason="x")]
    assert inline_tests.annotate(repo, base_sha, head_sha, changed, "rust") == []
    assert not changed[0].has_inline_tests


def test_a_change_confined_to_the_test_region_leaves_nothing_to_revert(tmp_path):
    """No revert is claimed when every hunk is the PR's own test."""
    repo = str(tmp_path)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    os.makedirs(os.path.join(repo, "src"))
    head_of_file = "pub fn a() -> i32 { 1 }\n\n#[cfg(test)]\nmod tests {\n    use super::*;\n"
    with open(os.path.join(repo, "src/lib.rs"), "w") as fh:
        fh.write(head_of_file + "    #[test]\n    fn one() { assert_eq!(a(), 1); }\n}\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    base_sha = git(repo, "rev-parse", "HEAD").stdout.strip()
    with open(os.path.join(repo, "src/lib.rs"), "w") as fh:
        fh.write(
            head_of_file
            + "    #[test]\n    fn one() { assert_eq!(a(), 1); }\n"
            + "    #[test]\n    fn two() { assert_eq!(a(), 1); }\n}\n"
        )
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "head")
    head_sha = git(repo, "rev-parse", "HEAD").stdout.strip()

    head_text = read_head_text(repo, head_sha, "src/lib.rs")
    regions = rust_test_regions(head_text)
    text, notes = revert_outside_regions(
        repo, base_sha, head_sha, "src/lib.rs", head_text, regions
    )
    assert text is None
    assert any("nothing in this file to revert" in n for n in notes)


def test_zero_context_hunks_hug_the_change(rust_repo):
    """Three lines of context would glue a change to the #[cfg(test)] line."""
    repo, base_sha, head_sha = rust_repo
    wide = file_hunks(repo, base_sha, head_sha, "src/lib.rs")
    tight = file_hunks(repo, base_sha, head_sha, "src/lib.rs", context=0)
    assert len(tight) >= len(wide)
    assert all(h.head_len <= 8 for h in tight)
