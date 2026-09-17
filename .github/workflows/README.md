# .github/workflows

GitHub Actions workflows.

`ci.yml` runs on push and pull request with two jobs. `lint` installs the project with uv
and runs ruff check, ruff format in check mode, and sqlfluff when SQL files exist. `docs`
checks that every specification and ADR carries a `Status:` line and that the root README
contains no TODO token.

There is deliberately no `.github/README.md`: GitHub prefers it over the root `README.md` on
the repository homepage.
