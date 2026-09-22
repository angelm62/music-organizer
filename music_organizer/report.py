from __future__ import annotations

import json
import os
from dataclasses import asdict

from rich.table import Table

from .console import console
from .planner import PlanEntry


def print_dry_run(entries: list[PlanEntry], library_root: str) -> None:
    matched = [e for e in entries if e.status != "unmatched"]
    unmatched = [e for e in entries if e.status == "unmatched"]

    table = Table(title=f"Planned changes ({len(matched)} tracks)", show_lines=False)
    table.add_column("Status", style="bold")
    table.add_column("Confidence", justify="right")
    table.add_column("Current path")
    table.add_column("New path")

    for e in matched:
        style = "green" if e.status == "matched" else "yellow"
        table.add_row(
            f"[{style}]{e.status}[/{style}]",
            f"{e.confidence}%",
            os.path.relpath(e.old_path, library_root),
            os.path.relpath(e.new_path, library_root),
        )
    console.print(table)

    if unmatched:
        console.print(
            f"\n[bold red]{len(unmatched)} tracks could not be confidently matched[/bold red] "
            "and will be left in place:"
        )
        for e in unmatched[:25]:
            reason = "; ".join(e.notes) if e.notes else "unknown"
            console.print(f"  - {os.path.relpath(e.old_path, library_root)} ({reason})")
        if len(unmatched) > 25:
            console.print(f"  ... and {len(unmatched) - 25} more (see report file)")

    console.print(
        f"\n[bold]{len(matched)}[/bold] would be moved/tagged, "
        f"[bold]{len(unmatched)}[/bold] need manual review."
    )
    console.print("Run again with [bold]--apply[/bold] to make these changes.")


def write_report_file(entries: list[PlanEntry], path: str) -> None:
    payload = []
    for e in entries:
        d = asdict(e)
        payload.append(d)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    console.print(f"Full report written to [bold]{path}[/bold]")
