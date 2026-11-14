# superlite bug: generated regression tests depend on a run-scoped, gitignored adapter

**Seen in:** superlite 4.1.0, superlite-cloud build `20261114-145345-superlite-cloud-core`.

## What happens

`probe_run` writes failing probes as `tests/regression/test_probe_<cat>_<n>.py`. The test header says "Fix the code, keep this test." Each test loads:

```python
ADAPTER = ROOT / '.superlite/probes/adapter.py'
```

The project's `.gitignore` excludes `.superlite/`, so the adapter is never committed. On top of that, `superlite-cli finish` moves `.superlite/probes/` into `.superlite/runs/<id>/probes/`.

The result: right after `finish`, every generated regression test fails locally. In a clean clone or in CI they fail from the start. In this build, the affected run went from `186 0 1` before `finish` to `181 5 1` after it, with no code change in between.

## Repro

1. Run a build where `probe_run` generates at least one regression test.
2. Make sure it passes.
3. Run `superlite-cli finish --plan <plan>`.
4. Run `superlite-cli run_tests --affected --base <start sha>`. The generated tests fail with a missing adapter.

## Suggested fix

Any one of these would do:

- **Write the adapter where the tests live.** For example, put it at `tests/regression/probe_adapter.py`, committed with the tests, and have the generated tests point there.
- **Make `finish` copy the adapter there** before archiving `.superlite/probes/`.
- **Inline the adapter entry into each generated test.** The tests then don't depend on any file outside themselves.

## Workaround used in superlite-cloud

The adapter was copied to `tests/regression/probe_adapter.py` and the `ADAPTER` line in the 5 generated tests was changed to point to it.
