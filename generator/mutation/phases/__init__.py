"""One module per change class, in the order `generator/mutation/changes.py` runs them.

A phase takes the tick context and records what it did on the report. It does not open a
connection, commit, or decide whether the tick succeeded: it changes rows inside a transaction
somebody else owns.
"""
