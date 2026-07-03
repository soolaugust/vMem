# Project Structure

vMem is organized around a small public Python package plus Claude Code hook entrypoints. The intended boundary is:

```text
memory_os/      Core Python package: storage, runtime, VFS, CLI, observability.
hooks/          Claude Code hook entrypoints and wrappers. Keep these as thin adapters where possible.
tests/          Product tests collected by pytest. Hook-specific tests live in tests/hooks/.
tools/          Operator and developer utilities: imports, maintenance, reports, migrations.
benchmarks/     Reproducible benchmarks and production smoke gates.
docs/           Architecture, setup, design notes, and public documentation.
assets/         Public assets used by docs or package metadata.
marketing/      Launch and community-facing drafts.
paper/          Research paper source and benchmark artifacts.
scripts/        Local development scripts; public release scripts should be documented or ignored.
```

## Layering Rules

1. `hooks/` should behave like syscall adapters: parse hook input, call `memory_os.*`, emit hook output.
2. Long-lived logic belongs under `memory_os/`, not in hook entrypoints.
3. Tests belong under `tests/`; hook tests belong under `tests/hooks/` and reference hook scripts explicitly.
4. Generated caches and build artifacts must stay out of the repository.
5. Large historical regression suites should be grouped by purpose when touched, instead of adding more flat files.

## Current Known Debt

- `hooks/retriever.py`, `hooks/retriever_daemon.py`, and `hooks/extractor.py` still contain substantial runtime logic.
- `memory_os/store/vfs.py` and `memory_os/store/mm.py` are large modules that should be split by storage responsibility over time.
- `tests/` contains many iteration-history regressions in a flat layout; future cleanup should move them under `tests/regression/iter/`.

## Preferred Future Shape

```text
memory_os/
  cli/
  core/
  store/
  retrieval/
  extraction/
  runtime/
    context/
    hooks/
    net/
    sched/
    workspace/
  vfs/
  observability/
  config/

hooks/
  *.py                 # Thin entrypoints only
  wrappers/
  hooks.json

tests/
  unit/
  integration/
  e2e/
  regression/iter/
  hooks/

tools/
  importers/
  maintenance/
  migrations/
  reports/
  dev/
```
