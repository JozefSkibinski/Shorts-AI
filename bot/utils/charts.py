"""Render comparison charts with matplotlib.

We use matplotlib's non-interactive ``Agg`` backend so the code runs happily
on a headless server (no display required). The function returns raw PNG
bytes that the cog wraps in a :class:`discord.File`.
"""

from __future__ import annotations

import io
from typing import Sequence

import matplotlib

matplotlib.use("Agg")  # must be set before importing pyplot

import matplotlib.pyplot as plt  # noqa: E402  (import after backend switch)
import pandas as pd  # noqa: E402


def render_interest_over_time(
    frame: pd.DataFrame,
    keywords: Sequence[str],
    *,
    title: str,
) -> bytes:
    """Render a line chart of interest-over-time and return PNG bytes.

    ``frame`` is expected to be the dataframe returned by
    ``pytrends.interest_over_time()`` (index = datetime, one column per keyword,
    plus an ``isPartial`` column which we drop).
    """
    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=120)
    try:
        for kw in keywords:
            if kw in frame.columns:
                ax.plot(frame.index, frame[kw], label=kw, linewidth=1.8)

        ax.set_title(title)
        ax.set_xlabel("Date")
        ax.set_ylabel("Relative interest (0–100)")
        ax.grid(True, alpha=0.3)
        ax.legend(loc="best", fontsize=9)
        fig.autofmt_xdate()
        fig.tight_layout()

        buf = io.BytesIO()
        fig.savefig(buf, format="png")
        buf.seek(0)
        return buf.getvalue()
    finally:
        # Always close the figure to avoid memory leaks inside the long-running
        # bot process.
        plt.close(fig)
