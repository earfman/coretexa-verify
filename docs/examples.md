# Worked examples

Every verdict, taken from a real pull request in someone else's repository. These are the runs the tool was validated against, not fixtures.

[← back to the README](../README.md)

---

## Verified against real pull requests

Every verdict below is real output from the CLI, not a mock-up.

### `NO_GATE` — [ioi-isr/cms#174](https://github.com/ioi-isr/cms/pull/174)

```bash
python -m coretexa_verify --repo /path/to/cms --base origin/main --head pr174
```

```
[!!] NO_GATE

3 of 5 behavioural change(s) in this PR can be reverted with all 5 of its tests
still passing: cmscontrib/loaders/italy_yaml.py hunk 2 (head lines 1318-1326);
cmscontrib/loaders/italy_yaml.py hunk 3 (head lines 1339-1345);
cmscontrib/loaders/italy_yaml.py hunk 4 (head lines 1348-1362).
This PR's tests would pass without that change.

per-hunk localisation (each hunk reverted on its own)
  GATED     cms/grading/tasktypes/BatchAndOutput.py hunk 1   ASSERT_FAIL: 3 passed, 2 failed
  GATED     cmscontrib/loaders/italy_yaml.py hunk 1          BUILD_ERROR: 0 passed, 1 errored
  UNGATED   cmscontrib/loaders/italy_yaml.py hunk 2          PASS: 5 passed
  UNGATED   cmscontrib/loaders/italy_yaml.py hunk 3          PASS: 5 passed
  UNGATED   cmscontrib/loaders/italy_yaml.py hunk 4          PASS: 5 passed
```

The PR is named after the loader change. Its five new tests cover the new helper
function and a compatibility shim — but the loader hunk itself can be thrown away
and all five still pass.

### `GATE_HOLDS` — [sqlfluff/sqlfluff#8201](https://github.com/sqlfluff/sqlfluff/pull/8201)

```bash
python -m coretexa_verify --repo /path/to/sqlfluff --base origin/main --head pr8201
```

```
[ok] GATE_HOLDS

Reverting src/sqlfluff/rules/structure/ST05.py makes 6 of the PR's test(s) fail:
the tests really do gate the change.

test selection
  test/fixtures/rules/std_rule_cases/ST05.yml
    -> 10 x test/rules/yaml_test_cases_test.py::test__rule_test_case[ST05_...]
       [fixture-map+added-cases]
       test files containing the literal 'std_rule_cases'; narrowed to the 10
       collected case(s) matching the 10 fixture key(s) this PR added

run at head:              PASS         - 10 passed in 3.36s
run with source reverted: ASSERT_FAIL  - 4 passed, 6 failed in 3.3s
```

The PR's tests are YAML fixtures, not test modules. They were mapped to
`test/rules/yaml_test_cases_test.py` and narrowed to the ten cases the PR added.

### `GATE_HOLDS_BUILD` — [QuantEcon/QuantEcon.py#905](https://github.com/QuantEcon/QuantEcon.py/pull/905), `--localize never`

```bash
python -m coretexa_verify --repo /path/to/quantecon --base origin/main \
    --head pr905 --localize never
```

```
[ok] GATE_HOLDS_BUILD

Reverting quantecon/util/notebooks.py stops 1 test(s) from building/importing
rather than assert-failing. That is still a real gate, but no assertion was
exercised.
```

The test module imports `TIMEOUT`, a constant the PR introduced, so a whole-file
revert cannot even reach an assertion. This is exactly why localisation exists:
with the default `--localize auto` the same PR drills down per hunk and reports
`GATE_HOLDS`, because each behavioural hunk really is detected.

### `NO_NEW_TESTS` — sqlfluff commit `a83c069`

```bash
python -m coretexa_verify --repo /path/to/sqlfluff \
    --base a83c069^ --head a83c069
```

```
[--] NO_NEW_TESTS

1 source file(s) changed and no test file was added or modified.

changed files
  M SOURCE src/sqlfluff/core/helpers/string.py
```

### `INCONCLUSIVE` — QuantEcon#905 with refinement disabled

```bash
python -m coretexa_verify --repo /path/to/quantecon --base origin/main \
    --head pr905 --no-refine
```

```
[??] INCONCLUSIVE

the PR's tests do not pass at head (4 passed, 1 failed, 2 errored), so reverting
the source proves nothing. Failed:
quantecon.util.tests.test_notebooks.TestNotebookUtils::test_fetch_nb_dependencies
```

That test file contains legacy tests that fetch live URLs, which fail in a
sandbox. The tool refuses to produce a verdict rather than blaming the PR for
its neighbours. (With refinement on — the default — only the four tests the PR
added are selected and the verdict is `GATE_HOLDS`.)

---
