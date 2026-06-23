"""The interactive feedback workspace (the human-in-the-loop generator).

`smoketree workspace` serves a small local web app: a gallery of the rendered outputs of
every rule that declares a ``feedback.append`` channel, each with a note box. A human
reviews an output and writes feedback; it is saved to that output's feedback file
(``feedback/.../X_feedback.md``), which the pipeline's compile rule folds back in on the
next run. The human is the feedback generator; this UI brings them into the loop.
"""

from __future__ import annotations

from .index import FeedbackCard, build_index

__all__ = ["FeedbackCard", "build_index"]
