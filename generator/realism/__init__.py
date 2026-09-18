"""Distribution and behaviour models.

Nothing in here touches a database or builds a row. Each module answers one question about what
the simulated bank's customers do, takes an explicit `random.Random`, and returns a value. The
entity generators in `generator/entities/` assemble rows from the answers.

Every parameter these models read comes from `generator/profiles.yml`. A number written inline
in one of these files is a defect: `docs/generator_realism.md` justifies the parameters, and it
can only do that for parameters that are in the file it describes.
"""
