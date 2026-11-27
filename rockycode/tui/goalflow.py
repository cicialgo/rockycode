"""/goal from the chat: Docker probe, the goal screen hand-off, the return summary.

Part of the TUI shell (rockycode/tui/app.py), split out so each concern reads on its
own; the app class mixes this in and the methods keep `self` = the app.
"""
from __future__ import annotations

import asyncio
import os

from rich.markup import escape
from textual import work
from textual.widgets import Static


from rockycode.palette import AMBER, VIOLET
from rockycode.tui.chatinput import ChatInput


class GoalMixin:
    def _gather_host_tools(self) -> dict:
        """Return tools that should stay on the host (not enter the sandbox)."""
        host_names = {"web_search", "web_research", "web_fetch", "create_artifact", "inspect_artifact",
                      "lsp_lookup", "lsp_symbol_search", "lsp_file_symbols", "lsp_diagnostics",
                      "loop_start", "loop_stop", "submit_plan"}
        if self._local_registry is None:
            return {}
        return {k: v for k, v in self._local_registry.items() if k in host_names}

    def _restore_host_tools(self) -> None:
        """Re-add host-side tools after a sandbox→local swap (lost in the swap)."""
        host_tools = self._gather_host_tools()
        if host_tools:
            self.engine.registry.update(host_tools)

    @work(group="goal", exclusive=True, exit_on_error=False)
    async def _handle_goal(self, text: str) -> None:
        """/goal [objective] — go autonomous. With an objective AND Docker ready it
        opens the goal SCREEN on top of chat (plan → confirm → work → summary) and
        pops back here when done. Without an objective (or Docker) it just guides.
        Worker: the docker probe must not block the message pump."""
        import shutil

        obj = text[len("/goal"):].strip()
        docker_up = await self._docker_ready()  # the shared probe; False when missing OR stopped
        if docker_up:
            docker_line = self._t('commands.docker_ready')
        elif shutil.which("docker") is None:
            docker_line = self._t('commands.docker_missing')
        else:
            docker_line = self._t('commands.docker_stopped')

        # Run the goal IN A SCREEN on top of chat (plan → confirm → milestones →
        # summary all rendered here), then pop back — no dropping to a bare terminal.
        if obj and docker_up:
            from rockycode.tui.goal_screen import GoalScreen

            backend = self._make_goal_backend(obj)
            await self._add(Static(
                self._t('commands.goal_opening', p1=escape(obj)),
                classes="tool-line"))
            summary = await self.push_screen_wait(GoalScreen(backend, obj, language=self._language))
            await self._on_goal_return(summary)
            return

        # Otherwise: guide (no objective, or Docker not ready).
        lines = [
            self._t('commands.goal_help'),
            docker_line,
        ]
        if not obj:
            lines.append(self._t('commands.goal_example'))
        else:
            lines.append(self._t('commands.goal_retry', p1=escape(obj)))
        await self._add(Static("\n".join(lines), classes="tool-line"))

    async def _docker_ready(self) -> bool:
        """Shared readiness gate for /goal and the plan handoff. A missing executable
        (FileNotFoundError) and a daemon that does not answer both return False, so
        tests can replace this one probe without Docker installed on the host."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "info",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            return (await asyncio.wait_for(proc.wait(), timeout=8)) == 0
        except Exception:  # noqa: BLE001
            return False

    def _make_goal_backend(self, objective: str, *, plan_file=None, plan_text=None):
        """Build the live goal backend from the session's model + config. Separate
        so a test can swap in a fake backend and drive the whole /goal loop.
        plan_file (from a /plan handoff) makes goal skip its planner and execute
        the already-approved plan."""
        from rockycode.config import load as load_config
        from rockycode.goal.budget import recommended
        from rockycode.goal.session import LiveGoalBackend

        currency = load_config(self.engine.workdir)["currency"]
        reviewer = os.getenv("ROCKYCODE_REVIEWER_MODEL") or self.engine.model
        return LiveGoalBackend(
            objective, "" if plan_file else self._chat_digest(),
            model=self.engine.model, reviewer_model=reviewer,
            budget=recommended(currency), workdir=self.engine.workdir,
            currency=currency, plan_file=plan_file, plan_text=plan_text,
            route=getattr(self.engine, "route", None),
        )

    def _chat_digest(self, max_chars: int = 4000) -> str:
        """A compact recap of the chat for the goal — recent user/assistant turns,
        truncated, so the goal continues what we were doing."""
        from rockycode.engine import images as images_mod
        parts = []
        for m in self.engine.history:
            role, content = m.get("role"), images_mod.content_text(m.get("content"))
            if role in ("user", "assistant") and content.strip():
                parts.append(f"{role}: {content.strip()}")
        return "\n".join(parts[-12:])[-max_chars:]

    async def _on_goal_return(self, summary) -> None:
        """Back in chat after the goal screen dismissed. Recap the outcome for the
        user AND seed the model's context so 'review it' / 'keep going' just work."""
        self.query_one(ChatInput).focus()
        if summary is None:
            return
        if summary.status == "cancelled":
            await self._add(Static(self._t('commands.goal_cancelled'), classes="tool-line"))
            return
        colour = VIOLET if summary.status == "done" else AMBER
        recap = self._t('commands.goal_result', p0=colour, p1=escape(summary.status), p2=escape(summary.reason))
        recap += (self._t('commands.goal_milestones', p0=summary.milestones_done, p1=summary.milestones_total, p2=self._t("commands.goal_branch", p0=escape(summary.branch)) if summary.branch else ""))
        if summary.branch:
            recap += (self._t('commands.goal_isolated', p1=escape(summary.branch)))
            recap += self._t('commands.goal_review', p1=escape(summary.review_cmd))
            recap += self._t('commands.goal_merge', p1=escape(summary.origin), p2=escape(summary.branch))
        if summary.log:
            recap += self._t('commands.goal_log', p0=escape(summary.log))
        await self._add(Static(recap, classes="tool-line"))
        # Seed the model with the exact facts so "review it" / "merge it" work and
        # it never assumes the changes are in the current working tree.
        if summary.branch:
            note = (f'I just ran an autonomous goal: {summary.status} — '
                    f'{summary.milestones_done}/{summary.milestones_total} milestones. The changes are '
                    f'COMMITTED ON BRANCH {summary.branch} in a separate git worktree ({summary.workspace}), '
                    f'NOT in the current working tree — the user\'s current files are unchanged. '
                    f'If they ask to REVIEW it, call the review_goal_branch tool with branch '
                    f'"{summary.branch}". If they ask to MERGE it, call the merge_goal_branch tool with '
                    f'branch "{summary.branch}" — it guards the merge (refuses a dirty tree, aborts on '
                    f'conflict) so use it instead of raw git. Always remind them the work is in a '
                    f'separate worktree.')
        else:
            note = (f'I just ran an autonomous goal: {summary.status} — '
                    f'{summary.milestones_done}/{summary.milestones_total} milestones. {summary.reason}')
        self.engine.history.append({"role": "user", "content": f"[system] {note}"})
