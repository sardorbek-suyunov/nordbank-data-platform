"""Row builders, one module per entity or closely coupled group.

No module here opens a database connection. Each takes the run configuration, the reference
vocabularies as plain data, a sub-stream factory, and a spool to write rows to. What comes back
is the state later generators need — the account book, the card book — held as compact tuples
rather than as objects, because the full profile holds about a million of them at once.
"""
