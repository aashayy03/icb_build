"""Live status table using the optional 'rich' library."""

from __future__ import annotations

from typing import Dict, List

from rich.console import Console
from rich.live import Live
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from ..runner import fmt_duration
from ..stages import StageState, StageStatus
from .base import STATE_STYLE, Display, overall_result, result_label, stage_detail


class RichDisplay(Display):
    def start(self, stages: List[StageStatus]) -> None:
        self._stages = stages
        # Spinners are kept per stage: a new Spinner each frame would never animate.
        self._spinners = {}  # type: Dict[str, Spinner]
        self.console = Console(file=self.stream)
        self._live = Live(
            get_renderable=self._render,
            console=self.console,
            refresh_per_second=4,
            transient=True,  # the final summary replaces the live table
        )
        self._live.start()

    def update(self, stages: List[StageStatus]) -> None:
        self._stages = stages

    def message(self, text: str) -> None:
        self.console.print(text, markup=False, highlight=False)

    def pause(self, stages: List[StageStatus]) -> None:
        self._stages = stages
        self._live.stop()  # transient: clears the live table...
        self.console.print(self._render())  # ...so leave a static copy above the question

    def stop(self, stages: List[StageStatus], interrupted: bool = False) -> None:
        self._stages = stages
        self._live.stop()  # no-op if already paused
        self.print_summary(stages, interrupted)

    def _state_cell(self, s: StageStatus):
        label = Text(s.state.value, style=STATE_STYLE[s.state])
        if s.state is StageState.RUNNING:
            spinner = self._spinners.get(s.name)
            if spinner is None:
                spinner = self._spinners[s.name] = Spinner("dots", text=label, style="yellow")
            return spinner
        return label

    def _render(self) -> Table:
        table = Table(
            title="ICB build  run %s  elapsed %s" % (self.run_id, self.run_elapsed()),
            caption="logs: %s" % self.run_dir,
            expand=True,
            title_justify="left",
            caption_justify="left",
        )
        table.add_column("Stage", no_wrap=True)
        table.add_column("State", no_wrap=True, min_width=12)
        table.add_column("Elapsed", no_wrap=True)
        table.add_column("Last output", no_wrap=True, overflow="ellipsis", ratio=1)
        for s in list(self._stages):
            detail = s.last_line
            if s.state in (StageState.SKIPPED, StageState.BLOCKED) or not detail:
                detail = s.note
            table.add_row(
                s.name,
                self._state_cell(s),
                fmt_duration(s.elapsed()),
                Text(detail, style="dim" if s.state is not StageState.RUNNING else ""),
            )
        return table

    def print_summary(self, stages: List[StageStatus], interrupted: bool = False) -> None:
        table = Table(
            title="Summary (run %s, total %s)" % (self.run_id, self.run_elapsed()),
            title_justify="left",
        )
        table.add_column("Stage", no_wrap=True)
        table.add_column("Result", no_wrap=True)
        table.add_column("Duration", no_wrap=True)
        table.add_column("Details", overflow="fold")
        for s in stages:
            table.add_row(
                s.name,
                Text(result_label(s), style=STATE_STYLE[s.state]),
                fmt_duration(s.elapsed()),
                Text(stage_detail(s)),
            )
        self.console.print(table)
        result = overall_result(stages, interrupted)
        style = "bold green" if result == "SUCCESS" else "bold red"
        self.console.print(Text("Result: ") + Text(result, style=style))
        self.console.print(Text("Logs:   %s" % self.run_dir))
