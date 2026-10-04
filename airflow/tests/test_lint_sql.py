"""The SQL lint's exit code is the lint's, not the console's."""

import lint_sql


def test_sqlfluff_writes_utf8_wherever_its_output_goes(monkeypatch):
    """On Windows `isatty()` is true for the null device, so sqlfluff printed its emoji summary
    into the ANSI code page under `make lint >/dev/null`, crashed after a clean lint and exited 1.
    """
    seen = {}

    def call(command, env=None):
        seen["command"], seen["env"] = command, env
        return 0

    monkeypatch.setattr(lint_sql.subprocess, "call", call)
    assert lint_sql.main() == 0
    assert seen["command"][:2] == ["sqlfluff", "lint"]
    # A floor on what it lints, stated: the warehouse schema and the hand-written dbt SQL.
    assert len(seen["command"]) - 2 >= 40
    assert seen["env"]["PYTHONUTF8"] == "1"
    assert seen["env"]["PYTHONIOENCODING"] == "utf-8"
