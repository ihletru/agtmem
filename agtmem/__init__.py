"""agtmem — a memory store you own.

Four rules the whole design follows:

  * the store is the contract: plain Markdown + frontmatter, on disk, yours
  * the index is a cache: delete it and ``agtmem reindex`` rebuilds it
  * the server never calls an LLM: writes are file I/O, retrieval is a query
  * zero runtime dependencies: Python standard library only

See ``docs/ARCHITECTURE.md`` for why each of those was chosen.
"""

__version__ = "0.1.0"
