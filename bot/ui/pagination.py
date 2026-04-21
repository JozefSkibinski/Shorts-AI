"""Interactive paginator for large embed result sets."""

from __future__ import annotations

import logging
from typing import Optional, Sequence

import discord

log = logging.getLogger(__name__)


class PaginatedEmbedView(discord.ui.View):
    """A prev/next/stop view over a fixed list of :class:`discord.Embed`.

    - Only the invoking user can page (silently ignores others).
    - Buttons disable on the last page / first page to signal bounds.
    - The view disables all buttons when the timeout fires.
    - ``send(interaction)`` is the only entrypoint the cog needs.
    """

    def __init__(
        self,
        pages: Sequence[discord.Embed],
        *,
        author_id: int,
        timeout: float = 120.0,
        files_per_page: Optional[Sequence[Optional[discord.File]]] = None,
    ) -> None:
        super().__init__(timeout=timeout)
        if not pages:
            raise ValueError("PaginatedEmbedView requires at least one page")
        self._pages = list(pages)
        self._files = list(files_per_page) if files_per_page else [None] * len(pages)
        self._author_id = author_id
        self._index = 0
        self._message: Optional[discord.Message] = None
        self._update_button_states()

    # Public ---------------------------------------------------------------

    async def send(self, interaction: discord.Interaction) -> None:
        """Send the first page as a follow-up to an already-deferred interaction."""
        # Single-page results don't need a view at all.
        if len(self._pages) == 1:
            file = self._files[0]
            if file is not None:
                await interaction.followup.send(embed=self._pages[0], file=file)
            else:
                await interaction.followup.send(embed=self._pages[0])
            self.stop()
            return

        file = self._files[0]
        kwargs = dict(embed=self._pages[0], view=self)
        if file is not None:
            kwargs["file"] = file
        self._message = await interaction.followup.send(**kwargs)

    # Buttons --------------------------------------------------------------

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary)
    async def _prev(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        if not await self._guard(interaction):
            return
        self._index = max(0, self._index - 1)
        await self._rerender(interaction)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def _next(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        if not await self._guard(interaction):
            return
        self._index = min(len(self._pages) - 1, self._index + 1)
        await self._rerender(interaction)

    @discord.ui.button(label="✕ Close", style=discord.ButtonStyle.danger)
    async def _close(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        if not await self._guard(interaction):
            return
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        try:
            await interaction.response.edit_message(view=self)
        except discord.HTTPException:
            log.debug("Close failed to edit message", exc_info=True)
        self.stop()

    # Internals ------------------------------------------------------------

    async def _guard(self, interaction: discord.Interaction) -> bool:
        """Only the invoking user may use the buttons."""
        if interaction.user and interaction.user.id == self._author_id:
            return True
        try:
            await interaction.response.send_message(
                "Only the person who ran the command can use these buttons.",
                ephemeral=True,
            )
        except discord.HTTPException:
            pass
        return False

    async def _rerender(self, interaction: discord.Interaction) -> None:
        self._update_button_states()
        try:
            await interaction.response.edit_message(
                embed=self._pages[self._index], view=self
            )
        except discord.HTTPException:
            log.debug("Pagination edit failed", exc_info=True)

    def _update_button_states(self) -> None:
        # Index 0 disables Prev; last index disables Next.
        self._prev.disabled = self._index <= 0
        self._next.disabled = self._index >= len(self._pages) - 1

    async def on_timeout(self) -> None:
        # Freeze the view so users don't keep clicking dead buttons.
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        if self._message is not None:
            try:
                await self._message.edit(view=self)
            except discord.HTTPException:
                log.debug("Timeout edit failed", exc_info=True)


def chunk_lines(lines: Sequence[str], *, per_page: int) -> list[list[str]]:
    """Helper to split a flat list of lines into paged chunks."""
    per_page = max(1, per_page)
    return [list(lines[i : i + per_page]) for i in range(0, len(lines), per_page)]
