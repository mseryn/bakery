#!/usr/bin/env python3
"""Where parbake lives, and the few things the bakery borrows from it.

The bakery finishes what parbake starts, so it needs parbake's markers -- the
conformsTo that says a file is unreviewed, the placeholder that stands in for a
missing description, the names of the directories parbake writes each kind of
output to, and the renderer that turns a Croissant into readable Markdown.
Importing them keeps one definition of each rather than two that drift: if
parbake renames parbaked_txt tomorrow, the importer follows, and the Markdown a
finished file renders to is the same projection parbake already defines.

parbake sits alongside this directory rather than being installed, so its path
has to be added before those imports will resolve. That happens here, once, so
no other module in the bakery has to know about it.
"""

import sys
from pathlib import Path

PARBAKE_DIRECTORY = Path(__file__).resolve().parent.parent / "parbake"

if str(PARBAKE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(PARBAKE_DIRECTORY))

try:
    from parbakery import CROISSANT_SUBDIRECTORY
    from settings import MARKDOWN_SUBDIRECTORY, TEXT_SUBDIRECTORY
    from parbaked_croissant import BANNER, FIELD_PLACEHOLDER, PARBAKED_CONFORMS_TO
    from croissant_to_md import render_markdown
except ImportError as problem:      # pragma: no cover - depends on the layout
    raise SystemExit(
        f"Could not import parbake from {PARBAKE_DIRECTORY}: {problem}\n"
        "The bakery expects to sit alongside the parbake directory."
    )

__all__ = [
    "BANNER",
    "CROISSANT_SUBDIRECTORY",
    "FIELD_PLACEHOLDER",
    "MARKDOWN_SUBDIRECTORY",
    "PARBAKED_CONFORMS_TO",
    "PARBAKE_DIRECTORY",
    "render_markdown",
    "TEXT_SUBDIRECTORY",
]
