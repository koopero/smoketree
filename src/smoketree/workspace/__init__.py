"""The interactive feedback workspace (the human-in-the-loop generator).

`smoketree workspace` serves a small local web app: a gallery of the rendered outputs of
every rule that declares one or more ``feedback`` channels, each rendered as a notes box
or a select control. A human reviews an output and records feedback; it is saved to that
channel's file, which the pipeline folds back in on the next run. The human is the
feedback generator; this UI brings them into the loop.
"""

from __future__ import annotations

from .index import FeedbackCard, build_index

__all__ = ["FeedbackCard", "build_index"]
