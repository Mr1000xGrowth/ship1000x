# Contributing to Ship1000x

Thanks for considering a contribution! Ship1000x is early-stage — PRs,
issues, and discussion are all welcome.

## Quick start

```bash
git clone https://github.com/Mr1000xGrowth/ship1000x.git
cd ship1000x
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Run the test suite
pytest tests/ -v

# Lint
ruff check .
```

## Workflow

1. **Open an issue first** for anything larger than a one-line fix or a
   typo. It lets us discuss scope before you spend time on code.
2. **Branch from `main`** : `git checkout -b my-feature`.
3. **Keep commits focused** — one logical change per commit, clean
   history matters more than velocity here.
4. **Run `pytest` + `ruff check .`** before opening the PR. CI will run
   them anyway across Ubuntu/macOS × Python 3.10/3.11/3.12.
5. **Open a PR** against `main`. Describe the *why*, not just the *what*.

## What we're looking for

- **New collectors** for AI dev tools not yet supported (Aider, Zed,
  Replit Agent, etc.). See `ship1000x/collectors/` for the contract.
- **Bug reports** from real installs (Linux especially — we're mostly
  tested on macOS).
- **Documentation** : unclear sections in README or `docs/ARCHITECTURE.md`,
  missing examples, typos.
- **Pricing updates** : when Anthropic/OpenAI change prices, update
  `ship1000x/core/pricing.py`.
- **Glob rules** : additions to `config/line_classification.yaml` for
  languages / frameworks that slip through.

## What we're NOT looking for (yet)

- **Refactors for the sake of refactoring** — wait until we have more
  real users and their feedback.
- **Web dashboard / SaaS features** — those are on the roadmap but need
  careful design. Open an issue first.
- **Team / multi-user aggregation** in the core — ship1000x is
  mono-user by design. Aggregation is the job of downstream consumers.

## Code style

- Python 3.10+ (use modern syntax : `X | None`, walrus, match/case when
  it helps).
- Ruff config in `pyproject.toml` is the source of truth.
- Type hints on public functions. Private helpers optional.
- Docstrings in French or English, both fine. Stay consistent within
  a file.
- No emoji in code, in docstrings, or in commit messages unless the
  user explicitly uses them in an issue.

## License

By contributing, you agree that your contributions will be licensed
under the [MIT License](LICENSE).
