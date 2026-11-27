"""Durable two-way mail without model execution, hidden grants or lost text."""
import asyncio
import json
import tempfile
from pathlib import Path

from rockycode.task_links import LIST_LIMIT, MAX_TEXT, TaskError, TaskLinks, build_task_tools


async def main():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        a = TaskLinks(root / "tasks.db")
        a.register("a", root, "Parent", path="first")
        a.register("b", root, "Child", parent="a", path="second", draft="handoff")
        a.register("other", root / "other", "Other project")
        b = TaskLinks(root / "tasks.db")  # independent process/host connection

        async def create(prompt):
            return a.register("c", root, "Child 2", parent="a", draft=prompt)

        ta, tb = build_task_tools(a, "a", create), build_task_tools(b, "b", create)
        receipt = json.loads(await ta["send_task_message"].fn(target="b", message="question"))
        assert receipt["status"] == "stored"
        received = json.loads(await tb["read_task_messages"].fn())["messages"]
        assert received[0]["sender"] == "a" and received[0]["content"] == "question"
        await tb["send_task_message"].fn(target="a", message="answer")
        assert json.loads(await ta["read_task_messages"].fn())["messages"][0]["content"] == "answer"
        assert ta["send_task_message"].risk == ta["create_new_task"].risk == "risky"
        assert ta["read_task_messages"].risk == "safe"
        assert "sender" not in ta["send_task_message"].schema["function"]["parameters"]["properties"]
        for target, content in [("a", "self"), ("other", "cross-workspace"), ("missing", "gone"), ("b", " "), ("b", "x" * 20001)]:
            try:
                a.send("a", target, content)
            except ValueError:
                pass
            else:
                raise AssertionError((target, "should be refused"))
        child = json.loads(await ta["create_new_task"].fn(prompt="new work"))
        assert child["parent"] == "a" and child["draft"] == "new work"
        a.register("b", root, "Child", path="resumed")
        assert a.for_session("second")["id"] == a.for_session("resumed")["id"] == "b"
        assert a.get("b")["parent"] == "a" and a.get("b")["draft"] == "handoff"
        a.close(); b.close()
        restored = TaskLinks(root / "tasks.db")
        assert restored.inbox("a")[0]["content"] == "answer"
        assert restored.inbox("b")[0]["content"] == "question"
        for _ in range(99):
            restored.send("a", "b", "x" * 20000)
        page = json.loads(await build_task_tools(restored, "b", create)["read_task_messages"].fn())
        assert len(page["messages"]) == 2 and page["next_offset"] == 2
        try:
            restored.send("a", "b", "overflow")
        except ValueError:
            pass
        else:
            raise AssertionError("mailbox must refuse overflow")
        assert len(restored.inbox("b")) == 100
        message_id = restored.inbox("b")[0]["id"]
        assert not restored.dismiss("a", message_id)
        assert restored.dismiss("b", message_id)
        assert not restored.dismiss("b", message_id)
        restored.send("a", "b", "space freed")
        restored.delete("b")
        assert [t["id"] for t in restored.list("a")] == ["a", "c"]
        # Refusals carry stable codes; each host renders them from its own catalog.
        codes = {}
        for target, content in [("other", "x"), ("a", "x"), ("missing", "x"), ("c", " ")]:
            try:
                restored.send("a", target, content)
            except TaskError as exc:
                codes[target] = (exc.code, exc.params)
        assert codes == {"other": ("cross_workspace", {}), "a": ("self_target", {}),
                         "missing": ("unknown_task", {}), "c": ("text_size", {"max": MAX_TEXT})}, codes
        # Listings stop at the most recent tasks, the source always included; `total` counts all.
        (root / "a.jsonl").write_text("")
        restored.register("a", root, "Parent", path=str(root / "a.jsonl"))
        for i in range(LIST_LIMIT + 10):
            restored.register(f"t{i}", root, f"Task {i}", parent="a", path=str(root / f"t{i}.jsonl"))
        listed = restored.list("a")
        assert len(listed) == LIST_LIMIT and listed[0]["id"] == "a" and listed[-1]["id"] == f"t{LIST_LIMIT + 9}"
        page = json.loads(await build_task_tools(restored, "a", create)["list_tasks"].fn())
        assert page["total"] == LIST_LIMIT + 12 and len(page["tasks"]) == LIST_LIMIT
        # Tasks whose session file is gone are pruned with their mail; rows without a path stay.
        (root / "t0.jsonl").write_text("")
        assert restored.prune() == LIST_LIMIT + 9
        assert [t["id"] for t in restored.list("a")] == ["a", "c", "t0"]
        restored.close()
    print("TASK LINKS OK: A→B→A, restart, scope, identity, size/queue limits, resume aliases, codes, bounded list, prune")


if __name__ == "__main__":
    asyncio.run(main())
