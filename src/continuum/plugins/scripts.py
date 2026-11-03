"""Built-in scripts."""

from __future__ import annotations


async def echo(job) -> None:
    """Acknowledge follow-ups, write what was asked into the workspace, and report the ticket ready."""
    notes = []
    for item in await job.inputs():
        if item.kind == "trigger" and item.human and not item.context:
            job.ack(item.trigger_id, f"收到补充：{item.text}")
            notes.append(item.text)
        elif item.kind == "text":
            notes.append(f"[host] {item.text}")
    job.workdir.mkdir(parents=True, exist_ok=True)
    result = job.workdir / "result.md"
    with result.open("a", encoding="utf-8") as out:
        out.write(f"# {job.title}\n\n{job.assignment}\n\n" + "".join(f"- {n}\n" for n in notes) + "\n")
    done = f"补充「{notes[-1]}」" if notes else f"「{job.title}」"
    job.report("ready", f"已处理{done}，结果在 {result}")
