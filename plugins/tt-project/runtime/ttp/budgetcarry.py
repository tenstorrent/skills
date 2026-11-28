# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A task budget never stops a tested landing.

When a code task's run ends on its own budget (turnsize.over_budget) without a hand-off, and `ttp checks`
recorded a candidate pass on exactly its branch's current head (prguard.task_record: the project's push
checks, plus any command the run checked after `--`, such as the device test its spec names), the daemon
hands that head off itself as done, with the checks as its evidence (carry): the usual review and push
queue follow, model-free. The overrun is recorded as one KIND event and a follow-up line naming the
budget and the spend, for the coordinator. A run without such a pass still stops at its budget."""
from __future__ import annotations

import json
import time

KIND = "task_budget_carry"


def carry(p, task: dict, head: str) -> dict | None:
    """The done hand-off that carries `head` on its recorded pass, or None (no head, no pass on it)."""
    from . import prguard
    if not head or task.get("kind") != "code":
        return None
    rec = prguard.task_record(p, task["id"], head)
    if not rec or rec.get("passed") is not True:
        return None
    budget, spent = float(task.get("budget_usd") or 0), float(task.get("spent_usd") or 0)
    cmds = [str(c) for c in rec.get("commands") or [] if str(c).strip()]
    line = (f"budget overrun: #{task['id']} spent ${spent:.2f} of its ${budget:.2f} budget; its head "
            f"{head[:12]} passed its checks, so the harness carried it to review and landing")
    p.db.x("INSERT INTO events(ts,source,kind,severity,text,data,status,task) VALUES(?,?,?,?,?,?,?,?)",
           (time.time(), "daemon", KIND, "low", line,
            json.dumps({"budget": budget, "spent": spent, "head": head, "run_dir": rec.get("run_dir") or ""}),
            "handled", task["id"]))
    return {"status": "done", "summary": f"The run stopped on its budget after `ttp checks` passed on head "
                                         f"{head[:12]} ({'; '.join(cmds)[:400]}): handed off by the harness.",
            "budget_carry": {"budget": budget, "spent": spent, "head": head, "commands": cmds,
                             "checks_run_dir": rec.get("run_dir") or ""},
            "followups": [line]}
