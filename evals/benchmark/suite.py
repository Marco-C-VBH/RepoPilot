"""The shape of the benchmark as a whole (docs/bench-v1-design.md §2).

``suite_report`` counts what the task files say -- per suite and repository,
hidden-only, cross-module, report tiers, fix shapes, categories, mutation size
-- and checks the 36 v1 tasks against the structural targets that were written
down before any of them was built.  A missed target is reported, never hidden:
the numbers go in the README either way.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from typing import Any

from evals.benchmark.schema import Category, ReportLevel, Suite, Task

# Targets for the tasks added in v1 (§2).  ``every_category`` is the minimum per
# category over the v1-new tasks; ``max_changed_lines`` the per-task ceiling.
V1_TARGETS: dict[str, int] = {
    "tasks": 36,
    "hidden_only": 18,
    "cross_module": 18,
    "symptom_only": 12,
    "multi_site": 8,
    "cross_file": 4,
    "every_category": 2,
    "max_changed_lines": 15,
}


def _counts(tasks: Sequence[Task]) -> dict[str, Any]:
    shapes = Counter(str(t.shape.label) for t in tasks)
    return {
        "tasks": len(tasks),
        "hidden_only": sum(t.hidden_only for t in tasks),
        "cross_module": sum(t.cross_module for t in tasks),
        "audited": sum(t.audited for t in tasks),
        "difficulty_derived": sum(t.difficulty == t.derived_difficulty for t in tasks),
        "symptom_only": sum(t.report_level is ReportLevel.SYMPTOM_ONLY for t in tasks),
        "public_api": sum(t.report_level is ReportLevel.PUBLIC_API for t in tasks),
        "internal": sum(t.report_level is ReportLevel.INTERNAL for t in tasks),
        "real": sum(t.source == "real" for t in tasks),
        "multi_site": sum(t.shape.multi_site for t in tasks),
        "cross_file": shapes.get("cross_file", 0),
        "shapes": dict(sorted(shapes.items())),
        "difficulty": dict(sorted(Counter(str(t.difficulty) for t in tasks).items())),
        "category": dict(sorted(Counter(str(t.category) for t in tasks).items())),
        "repo": dict(sorted(Counter(t.repo_name for t in tasks).items())),
        "changed_lines_max": max((t.shape.changed_lines for t in tasks), default=0),
        "oversized": sorted(
            t.id for t in tasks if t.shape.changed_lines > V1_TARGETS["max_changed_lines"]
        ),
    }


def suite_report(tasks: Sequence[Task]) -> dict[str, Any]:
    """Counts for every task, per suite, and the v1-new targets with their verdicts."""
    v1_new = [t for t in tasks if t.suite is Suite.V1]
    counts_new = _counts(v1_new)
    categories_in_use = [
        c for c in Category if c is not Category.OTHER and c is not Category.RETRY_LOGIC
    ]
    thin = [
        str(c)
        for c in categories_in_use
        if counts_new["category"].get(str(c), 0) < V1_TARGETS["every_category"]
    ]
    checks = {
        "tasks": (
            counts_new["tasks"],
            V1_TARGETS["tasks"],
            counts_new["tasks"] >= V1_TARGETS["tasks"],
        ),
        "hidden_only": (
            counts_new["hidden_only"],
            V1_TARGETS["hidden_only"],
            counts_new["hidden_only"] >= V1_TARGETS["hidden_only"],
        ),
        "cross_module": (
            counts_new["cross_module"],
            V1_TARGETS["cross_module"],
            counts_new["cross_module"] >= V1_TARGETS["cross_module"],
        ),
        "symptom_only": (
            counts_new["symptom_only"],
            V1_TARGETS["symptom_only"],
            counts_new["symptom_only"] >= V1_TARGETS["symptom_only"],
        ),
        "multi_site": (
            counts_new["multi_site"],
            V1_TARGETS["multi_site"],
            counts_new["multi_site"] >= V1_TARGETS["multi_site"],
        ),
        "cross_file": (
            counts_new["cross_file"],
            V1_TARGETS["cross_file"],
            counts_new["cross_file"] >= V1_TARGETS["cross_file"],
        ),
        "every_category": (len(thin), 0, not thin),
        "changed_lines": (
            counts_new["changed_lines_max"],
            V1_TARGETS["max_changed_lines"],
            not counts_new["oversized"],
        ),
    }
    return {
        "all": _counts(tasks),
        "v0": _counts([t for t in tasks if t.suite is Suite.V0]),
        "v1_new": counts_new,
        "targets": {k: {"actual": a, "target": b, "met": ok} for k, (a, b, ok) in checks.items()},
        "thin_categories": thin,
        "unaudited": sorted(t.id for t in tasks if not t.audited),
        "difficulty_mismatch": sorted(t.id for t in tasks if t.difficulty != t.derived_difficulty),
    }


def format_suite_report(report: dict[str, Any]) -> str:
    lines: list[str] = []
    for label, key in (("all", "all"), ("v0", "v0"), ("v1-new", "v1_new")):
        c = report[key]
        if not c["tasks"]:
            continue
        lines.append(
            f"{label:<7} {c['tasks']:>3} task(s): hidden-only {c['hidden_only']}, cross-module "
            f"{c['cross_module']}, symptom_only {c['symptom_only']}, public_api {c['public_api']}, "
            f"internal {c['internal']}, real {c['real']}, multi-site {c['multi_site']} "
            f"(cross-file {c['cross_file']}); difficulty "
            + ", ".join(f"{k} {v}" for k, v in c["difficulty"].items())
            + "; repos "
            + ", ".join(f"{k} {v}" for k, v in c["repo"].items())
        )
    if report["v1_new"]["tasks"]:
        lines.append("v1-new targets (docs/bench-v1-design.md §2):")
        for name, t in report["targets"].items():
            mark = "ok " if t["met"] else "MISSED"
            if name == "every_category":
                detail = (
                    "every category >= 2"
                    if t["met"]
                    else f"thin: {', '.join(report['thin_categories'])}"
                )
            elif name == "changed_lines":
                detail = f"max changed lines {t['actual']} (ceiling {t['target']})"
            else:
                detail = f"{t['actual']} (target >= {t['target']})"
            lines.append(f"  {mark:<6} {name:<15} {detail}")
    if report["unaudited"]:
        lines.append(f"unaudited (run make_task --refresh): {', '.join(report['unaudited'])}")
    if report["difficulty_mismatch"]:
        lines.append(
            "difficulty not derived (run make_task --refresh): "
            + ", ".join(report["difficulty_mismatch"])
        )
    return "\n".join(lines)
