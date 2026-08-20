from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class Issue:
    severity: str
    code: str
    message: str
    context: str | None = None


class ValidationReport:
    def __init__(self) -> None:
        self.issues: list[Issue] = []

    @property
    def errors(self) -> list[Issue]:
        return [item for item in self.issues if item.severity == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [item for item in self.issues if item.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def error(self, code: str, message: str, context: str | None = None) -> None:
        self.issues.append(Issue("error", code, message, context))

    def warning(self, code: str, message: str, context: str | None = None) -> None:
        self.issues.append(Issue("warning", code, message, context))

    def info(self, code: str, message: str, context: str | None = None) -> None:
        self.issues.append(Issue("info", code, message, context))

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "issues": [asdict(item) for item in self.issues],
        }

    def write_json(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")

    def format_text(self) -> str:
        lines = [
            f"Validation: {'PASS' if self.ok else 'FAIL'} "
            f"({len(self.errors)} errors, {len(self.warnings)} warnings)"
        ]
        for item in self.issues:
            where = f" [{item.context}]" if item.context else ""
            lines.append(f"{item.severity.upper():7} {item.code}{where}: {item.message}")
        return "\n".join(lines)
