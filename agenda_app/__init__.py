"""A small, durable local application for town-hall agenda review.

The package deliberately uses only the Python standard library for its core
storage and HTTP layers.  Optional readers/adapters are loaded at the edge so
that opening the review application works when Ollama or a browser is offline.
"""

__version__ = "2.0.0"
