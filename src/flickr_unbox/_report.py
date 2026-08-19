"""
Shared end-of-run summary for every pipeline stage.

The bash/perl reference pipeline streamed one line per file and left it to
the operator to grep the log for COLLISION/UNRESOLVED/errors. That doesn't
scale to a tool meant for people who aren't going to grep a log by hand, so
every flickr-unbox stage builds one of these and prints it once at the end
-- in dry-run mode too, since dry-run's whole job is to be a validation
report the operator reads before deciding to run for real.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RunSummary:
    stage: str
    dry_run: bool
    counts: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)

    def bump(self, key: str, n: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + n

    def note(self, message: str) -> None:
        self.notes.append(message)

    def render(self) -> str:
        mode = "DRY RUN -- nothing on disk was changed" if self.dry_run else "EXECUTED"
        lines = [f"=== {self.stage} summary ({mode}) ==="]
        if self.counts:
            width = max(len(k) for k in self.counts)
            for key, value in self.counts.items():
                lines.append(f"  {key:<{width}} : {value}")
        else:
            lines.append("  (nothing to report)")
        for n in self.notes:
            lines.append(f"  note: {n}")
        return "\n".join(lines)

    def print(self) -> None:
        print(self.render())
