"""/sandbox on | off | status: swap the registry into and out of a container.

Part of the TUI shell (rockycode/tui/app.py), split out so each concern reads on its
own; the app class mixes this in and the methods keep `self` = the app.
"""
from __future__ import annotations

from textual import work
from textual.widgets import Static




class SandboxMixin:
    @work(group="sandbox", exclusive=True, exit_on_error=False)
    async def _handle_sandbox(self, text: str) -> None:
        """Handle /sandbox on|off|status.

        A worker (not the message pump): the first `/sandbox on` may pull a
        Docker image, which is unbounded — awaiting it inline froze the UI with
        no way to cancel or quit. Own group so a turn won't cancel a start."""
        parts = text.split()
        sub = parts[1].lower() if len(parts) > 1 else "status"

        if sub == "status":
            if self.sandbox is not None and self.sandbox.is_running:
                await self._add(
                    Static(
                        self._t('commands.sandbox_on', p2=self.sandbox.container_id[:12]),
                        classes="tool-line",
                    )
                )
            else:
                await self._add(Static(self._t('commands.sandbox_off'), classes="tool-line"))
            return

        if sub == "off":
            if self.sandbox is None or not self.sandbox.is_running:
                await self._add(Static(self._t('commands.sandbox_not_running'), classes="tool-line"))
                return
            await self.sandbox.stop()
            self.sandbox = None
            if self._local_registry is not None:
                self.engine.swap_registry(self._local_registry, file_access=self._local_file_access)
            # Re-add host-side tools that were stripped during sandbox swap.
            self._restore_host_tools()
            self._render_status()
            await self._add(
                Static(
                    self._t('commands.sandbox_stopped'),
                    classes="tool-line",
                )
            )
            await self._maybe_nudge_yolo_host()  # if yolo, the host is exposed again
            return

        if sub == "on":
            if self.sandbox is not None and self.sandbox.is_running:
                await self._add(Static(self._t('commands.sandbox_running'), classes="tool-line"))
                return
            if self._sandbox_starting:
                await self._add(Static(self._t('commands.sandbox_starting_already'), classes="tool-line"))
                return
            self._sandbox_starting = True
            try:
                from rockycode.engine.sandbox import ChatSandbox, build_sandbox_registry
                want_net = len(parts) > 2 and parts[2].lower() in ("network", "net", "online")
                await self._add(Static(self._t('commands.sandbox_starting'), classes="tool-line"))
                sb = await ChatSandbox.start(self.engine.workdir, network=want_net)
                self.sandbox = sb
                host_tools = self._gather_host_tools()
                self.engine.swap_registry(build_sandbox_registry(sb, extras=host_tools))
                self._render_status()
                net_note = (self._t('commands.network_on') if want_net
                            else self._t('commands.network_off'))
                await self._add(
                    Static(
                        self._t('commands.sandbox_started', p2=sb.container_id[:12], p3=net_note),
                        classes="tool-line",
                    )
                )
            except Exception as exc:
                await self._add(
                    Static(
                        self._t('commands.sandbox_error', p1=exc),
                        classes="tool-line error-msg",
                    )
                )
            finally:
                self._sandbox_starting = False
            return

        await self._add(Static(self._t('commands.sandbox_usage'), classes="tool-line"))

    # ---- image attachments ---------------------------------------------------
