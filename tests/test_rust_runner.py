"""Rust runner: cargo text parsing, crate mapping, narrowing, inline tests.

All parsing tests run against canned cargo output; no toolchain is needed.
"""

from __future__ import annotations

import os

from coretexa_verify.classify import classify, is_rust_integration_test
from coretexa_verify.models import Kind, Outcome, UNEVALUABLE_OUTCOMES
from coretexa_verify.runners.rust import (
    CargoTestRunner,
    crate_name,
    is_workspace_root,
    module_prefix,
    owning_manifest,
    parse_cargo_test_text,
    region_generates_tests_by_macro,
    rust_test_paths,
    find_test_spans,
)

PASS_OUTPUT = """
   Compiling ignore v0.4.31 (/repo/crates/ignore)
    Finished `test` profile [unoptimized + debuginfo] target(s) in 1.20s
     Running unittests src/lib.rs (target/debug/deps/ignore-cef53e5ff488cc02)

running 186 tests
test gitignore::tests::ig_escaped_trailing_space ... ok
test gitignore::tests::trim_trailing_unescaped_spaces ... ok

test result: ok. 186 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.27s

     Running tests/gitignore_skip_bom.rs (target/debug/deps/gitignore_skip_bom-1fae)

running 1 test
test bom_skipped ... ok

test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s
"""

FAIL_OUTPUT = """
running 3 tests
test gitignore::tests::ig_escaped_trailing_space ... FAILED
test gitignore::tests::other ... ok
test gitignore::tests::ignot_escaped_trailing_space_nomatch ... FAILED

failures:
    gitignore::tests::ig_escaped_trailing_space

test result: FAILED. 1 passed; 2 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.01s
"""

# The gtk4-rs shape: revert the source, keep the test, the crate stops compiling.
COMPILE_ERROR_OUTPUT = """
   Compiling gtk4 v0.12.0-alpha (/repo/gtk4)
error[E0599]: no method named `accessible_role` found for mutable reference `&mut CustomClassWidgetClass` in the current scope
    --> gtk4/src/subclass/widget.rs:1600:24
error[E0599]: no method named `activate_signal` found for mutable reference `&mut CustomClassWidgetClass` in the current scope
    --> gtk4/src/subclass/widget.rs:1601:24
error[E0599]: no method named `add_shortcut` found for mutable reference `&mut CustomClassWidgetClass` in the current scope
    --> gtk4/src/subclass/widget.rs:1602:24
error: aborting due to 3 previous errors

For more information about this error, try `rustc --explain E0599`.
error: could not compile `gtk4` (lib test) due to 3 previous errors
"""

IGNORED_OUTPUT = """
running 4 tests
test a::needs_network ... ignored
test a::needs_display ... ignored
test a::slow ... ignored
test a::other ... ignored

test result: ok. 0 passed; 0 failed; 4 ignored; 0 measured; 0 filtered out; finished in 0.00s
"""


# ==========================================================================
# parsing
# ==========================================================================


def test_a_clean_run_sums_every_test_binary():
    res = parse_cargo_test_text(PASS_OUTPUT, 0)
    assert res.outcome is Outcome.PASS
    assert res.passed == 187  # 186 lib tests + 1 integration test
    assert res.failed == res.skipped == 0
    assert res.executed == 187


def test_failures_are_assert_fail_and_are_named():
    res = parse_cargo_test_text(FAIL_OUTPUT, 101)
    assert res.outcome is Outcome.ASSERT_FAIL
    assert res.passed == 1 and res.failed == 2
    assert "gitignore::tests::ig_escaped_trailing_space" in res.failing_ids
    assert "gitignore::tests::ignot_escaped_trailing_space_nomatch" in res.failing_ids


def test_a_compile_error_is_a_build_error_carrying_its_e_codes():
    """This is what makes GATE_HOLDS_BUILD mean something for Rust."""
    res = parse_cargo_test_text("", 101, stderr=COMPILE_ERROR_OUTPUT)
    assert res.outcome is Outcome.BUILD_ERROR
    assert res.erroring_ids == ["E0599"]
    assert "did not compile" in res.note
    assert res.passed == 0 and res.failed == 0


def test_the_error_count_excludes_cargos_own_summary_lines():
    """`error: could not compile ... due to 3 previous errors` is not a 4th error.

    Hand-counting gtk4-rs gives exactly 8 E0599 diagnostics; counting cargo's
    restatement of them alongside made the tool report 9.
    """
    res = parse_cargo_test_text("", 101, stderr=COMPILE_ERROR_OUTPUT)
    assert res.errored == 3
    assert "3 rustc error(s)" in res.note


def test_an_uncoded_error_is_counted_only_when_nothing_carries_a_code():
    blob = "error: expected one of `,` or `}`\nerror: could not compile `x`\n"
    res = parse_cargo_test_text("", 101, stderr=blob)
    assert res.outcome is Outcome.BUILD_ERROR
    assert res.errored == 1
    assert res.erroring_ids == ["<compile error>"]


def test_ignored_tests_are_skips_and_never_back_a_verdict():
    """`#[ignore]` is Rust's skip: excluded from the executed count."""
    res = parse_cargo_test_text(IGNORED_OUTPUT, 0)
    assert res.outcome is Outcome.PASS
    assert res.skipped == 4 and res.passed == 0
    assert res.executed == 0  # the 1.2.1 skip guard fires on exactly this


def test_a_filter_that_matches_nothing_is_no_tests_run():
    out = "running 0 tests\n\ntest result: ok. 0 passed; 0 failed; 0 ignored; 0 measured; 5 filtered out\n"
    res = parse_cargo_test_text(out, 0)
    assert res.outcome is Outcome.NO_TESTS_RUN


def test_a_nonzero_exit_with_no_summary_and_no_error_is_a_runner_error():
    res = parse_cargo_test_text("", 127, stderr="cargo: command not found\n")
    assert res.outcome is Outcome.RUNNER_ERROR
    assert res.outcome in UNEVALUABLE_OUTCOMES


def test_should_panic_annotations_do_not_break_the_line_parser():
    out = (
        "test a::boom - should panic ... ok\n"
        "test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out\n"
    )
    assert parse_cargo_test_text(out, 0).passed == 1


# ==========================================================================
# manifests and crate mapping
# ==========================================================================


def write(root, rel, text=""):
    path = os.path.join(str(root), rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def test_crate_name_reads_only_the_package_table():
    manifest = '[workspace]\nmembers = ["a"]\n\n[package]\nname = "ignore"\nversion = "0.4.31"\n'
    assert crate_name(manifest) == "ignore"
    assert is_workspace_root(manifest)


def test_a_virtual_manifest_has_no_package_name():
    assert crate_name('[workspace]\nmembers = ["a", "b"]\n') == ""


def test_owning_manifest_finds_the_nearest_real_crate(tmp_path):
    write(tmp_path, "Cargo.toml", '[workspace]\nmembers = ["crates/*"]\n')
    write(tmp_path, "crates/ignore/Cargo.toml", '[package]\nname = "ignore"\n')
    write(tmp_path, "crates/ignore/src/gitignore.rs", "")
    assert owning_manifest(str(tmp_path), "crates/ignore/src/gitignore.rs") == "crates/ignore"


def test_focus_maps_paths_to_p_crate_specs(tmp_path):
    write(tmp_path, "Cargo.toml", '[workspace]\nmembers = ["crates/*"]\n')
    write(tmp_path, "crates/ignore/Cargo.toml", '[package]\nname = "ignore"\n')
    runner = CargoTestRunner(str(tmp_path), "x")
    targets, why = runner.focus(["crates/ignore/src/gitignore.rs"])
    assert targets == ["ignore"]
    assert "-p ignore" in why


def test_an_integration_test_file_becomes_a_test_binary_target(tmp_path):
    write(tmp_path, "Cargo.toml", '[workspace]\nmembers = ["crates/*"]\n')
    write(tmp_path, "crates/ignore/Cargo.toml", '[package]\nname = "ignore"\n')
    runner = CargoTestRunner(str(tmp_path), "x")
    targets, _ = runner.focus(["crates/ignore/tests/gitignore_skip_bom.rs"])
    assert targets == ["ignore#gitignore_skip_bom"]
    argv = runner.build_command(targets, "/tmp/r.xml")
    assert argv[:3] == ["cargo", "test", "--no-fail-fast"]
    assert "--test" in argv and "gitignore_skip_bom" in argv


def test_a_narrowed_target_keeps_its_filter_through_focus(tmp_path):
    write(tmp_path, "Cargo.toml", '[package]\nname = "solo"\n')
    runner = CargoTestRunner(str(tmp_path), "x")
    targets, _ = runner.focus(["src/lib.rs::tests::alpha"])
    assert targets == ["solo::tests::alpha"]
    argv = runner.build_command(targets, "/tmp/r.xml")
    assert argv[-3:] == ["--", "--exact", "tests::alpha"]


def test_several_filters_are_all_passed_and_anchored(tmp_path):
    runner = CargoTestRunner(str(tmp_path), "x")
    argv = runner.build_command(["ignore::a::one", "ignore::a::two"], "/tmp/r.xml")
    # --exact stops `one` also selecting `one_regression`.
    assert argv[-4:] == ["--", "--exact", "a::one", "a::two"]
    assert argv.count("-p") == 1


def test_collect_declines_while_targets_are_still_file_paths(tmp_path):
    """Enumerating costs a full compile; do not burn one on a command cargo rejects."""
    runner = CargoTestRunner(str(tmp_path), "x")
    assert runner.collect(["crates/ignore/src/gitignore.rs"], 300) is None


# ==========================================================================
# libtest module paths
# ==========================================================================


def test_module_prefix_follows_the_file_into_the_crate():
    assert module_prefix("src/lib.rs") == ""
    assert module_prefix("src/main.rs") == ""
    assert module_prefix("src/gitignore.rs") == "gitignore"
    assert module_prefix("src/a/mod.rs") == "a"
    assert module_prefix("src/a/b.rs") == "a::b"
    # An integration test file is its own crate root, so it has no prefix.
    assert module_prefix("tests/cli.rs") == ""


def test_in_file_module_nesting_builds_the_reported_test_path():
    src = (
        "#[cfg(test)]\n"
        "mod tests {\n"
        "    use super::*;\n"
        "    #[test]\n"
        "    fn alpha() { assert!(true); }\n"
        "    mod inner {\n"
        "        #[test]\n"
        "        fn beta() {}\n"
        "    }\n"
        "}\n"
    )
    found = {name: path for name, path, _ in rust_test_paths(src)}
    assert found == {"alpha": "tests::alpha", "beta": "tests::inner::beta"}


def test_braces_inside_strings_and_comments_do_not_shift_the_module_path():
    src = (
        "mod tests {\n"
        '    const A: &str = "} not a real brace {";\n'
        "    // } neither is this\n"
        "    /* nor { this } */\n"
        '    const B: &str = r#"raw } string"#;\n'
        "    #[test]\n"
        "    fn alpha() {}\n"
        "}\n"
        "#[test]\n"
        "fn top_level() {}\n"
    )
    found = {name: path for name, path, _ in rust_test_paths(src)}
    assert found == {"alpha": "tests::alpha", "top_level": "top_level"}


def test_test_spans_end_at_the_matching_brace_not_at_the_next_test():
    src = (
        "#[test]\n"          # 1
        "fn alpha() {\n"     # 2
        "    let x = 1;\n"   # 3
        "}\n"                # 4
        "\n"                 # 5
        "fn helper() {}\n"   # 6
        "\n"                 # 7
        "#[test]\n"          # 8
        "fn beta() {}\n"     # 9
    )
    spans = {name: (start, end) for name, _, start, end in find_test_spans(src)}
    assert spans["alpha"] == (1, 4)  # not (1, 7)
    assert spans["beta"][0] == 8


def test_framework_attributes_count_as_tests():
    src = "#[tokio::test]\nasync fn alpha() {}\n#[rstest]\nfn beta() {}\n"
    assert {n for n, _, _ in rust_test_paths(src)} == {"alpha", "beta"}


# ==========================================================================
# the macro guard
# ==========================================================================


def test_a_test_generating_macro_blocks_narrowing():
    """ripgrep's gitignore tests are `ignored!(...)` macros.

    Their names appear nowhere in the source, so keeping only the `#[test] fn`s
    the diff touched would silently drop the cases the PR actually added.
    """
    src = (
        "#[cfg(test)]\n"
        "mod tests {\n"
        "    ignored!(ig1, \"a\", \"b\");\n"
        "    #[test]\n"
        "    fn alpha() {}\n"
        "}\n"
    )
    assert region_generates_tests_by_macro(src, [(1, 6)]).startswith("ignored!")


def test_a_macro_in_a_comment_or_string_does_not_block_narrowing():
    src = (
        "mod tests {\n"
        "    // ignored!(not really);\n"
        '    const S: &str = "ignored!(nope);";\n'
        "    #[test]\n"
        "    fn alpha() {}\n"
        "}\n"
    )
    assert region_generates_tests_by_macro(src, [(1, 6)]) == ""


def test_a_plain_test_module_permits_narrowing():
    src = "mod tests {\n    #[test]\n    fn alpha() {}\n}\n"
    assert region_generates_tests_by_macro(src, [(1, 4)]) == ""


# ==========================================================================
# classification
# ==========================================================================


def test_a_top_level_tests_rs_file_is_an_executable_integration_test():
    assert is_rust_integration_test("crates/ignore/tests/gitignore.rs")
    c = classify("crates/ignore/tests/gitignore.rs")
    assert c.kind is Kind.TEST and c.executable_test


def test_a_shared_helper_under_tests_is_not_a_cargo_target():
    """`tests/common/mod.rs` is a module, not a test binary; --test would reject it."""
    assert not is_rust_integration_test("crates/ignore/tests/common/mod.rs")
    c = classify("crates/ignore/tests/common/mod.rs")
    assert c.kind is Kind.TEST and not c.executable_test


def test_a_plain_source_file_is_source():
    assert classify("crates/ignore/src/gitignore.rs").kind is Kind.SOURCE


# ==========================================================================
# no separate build step
# ==========================================================================


def test_cargo_declares_no_build_step_and_no_artefact_risk():
    """`cargo test` recompiles from source, so a revert always reaches the tests."""
    runner = CargoTestRunner("/repo", "x")
    assert runner.detect_build_step(900) is None
    assert runner.artifact_risk(["ignore"], ["crates/ignore/src/gitignore.rs"]) == ""


def test_colour_is_disabled_so_the_line_parsers_see_plain_text():
    runner = CargoTestRunner("/repo", "x")
    assert runner.subprocess_env()["CARGO_TERM_COLOR"] == "never"
