"""Typing is always the user's message: the one keyboard rule of every card in the chat.

A card sits just above the input: a tool call to approve, a plan to review, an
image to route, a loop, a proposal, a routine. Some open by themselves, while the
user may be in the middle of a sentence. A card that took the keyboard then read
those keys as its answer: it ran commands nobody had looked at, and dropped the
rest of the sentence. So:

  - A card the user did not open never takes the keyboard. It waits, dimmed, and
    its last line says how to come to it. A card the user opened (a command, a
    click) has the keyboard at once.
  - From an empty message box, Enter comes to the newest card that waits, and Tab
    does from any message. Enter only brings the keyboard.
  - ↑ and ↓ in an empty message box do nothing while a card waits. They are not a
    way to a card: an arrow that came to a card and moved it landed one Enter from
    "allow for session", and ↑↑ for an older message walked a card that starts on
    Deny towards "run". They do not bring back the last message either: the Enter
    behind it would send that again and drop the turn.
  - But every card that waits remembers that arrows were pressed in the message box
    (`poke_all`: an empty box or a draft, it makes no difference to what the user
    meant). When the user then comes to a card, no row is highlighted and Enter
    picks nothing until an arrow is pressed ON the card. That first arrow only shows
    the row the card stands on; the next ones move. Enter never picks a row after
    arrows the card did not see: ↓ ↓ Enter Enter, meant as Deny, used to pick the
    row the card started on.
  - With more than one card, Tab goes round: the message box, the newest card, the
    one above, …, the box again. So each can be reached, and a card's waiting line
    names the key that really leads to it.
  - A card is answered by clicking a row, or with ↑↓ and Enter. No letter answers it.
  - What is typed or pasted while a card holds the keyboard goes into the message
    box, and the keyboard goes with it.
  - Enter is heard only after the card has held the keyboard, untouched, for half
    a second: not right after the user came to it, not right after the highlight
    moved. An early Enter says so and starts the wait again, so the Enter that
    brought the user to the card, an Enter held down or pressed twice, never picks
    a row. The arrows are never held back. The wait lives in the action, not in the
    key handler: a key that another widget let through reaches whichever card has
    the keyboard by then, and a card without the keyboard hears nothing.
  - ↑ and ↓ stop at the first and last row. From a card that starts on Deny one
    stray arrow does not wrap around to "run". ↓ past the last row goes back to a
    message that is being written.
  - Esc in the message box is the Esc of the card the user was last on, when that
    Esc lets nothing go ahead (ESC_FROM_BOX). A card they never came to is not it.
  - When a card that has the keyboard closes, the keyboard goes back to the message
    box first (card_closing, before the card is removed), never on to another card:
    left to itself, Textual hands it to the card above.

A card shows who has the keyboard the way the message box does: its border and rows
are bright only while it holds it (each card's `:focus` rules), and its last line
then lists its keys. The card keeps its height through all of that.
"""
from __future__ import annotations

import time
import weakref

from textual import events
from textual.widgets import Static

from rockycode.i18n import tr
from rockycode.palette import LAVENDER

ENTER_GUARD = 0.5  # seconds a card must hold the keyboard, untouched, before it hears Enter
_NEVER = float("inf")


def _box(app):
    """The message box, or None (a modal screen is up, the app is closing)."""
    from rockycode.tui.chatinput import ChatInput

    try:
        return app.query_one(ChatInput)
    except Exception:  # noqa: BLE001 — no box, nothing to hand keys to
        return None


def waiting_cards(app, closed=None) -> "list[CardKeys]":
    """The cards on screen that still want an answer, oldest first (top to bottom)."""
    try:
        cards = [w for w in app.screen.walk_children() if isinstance(w, CardKeys)]
    except Exception:  # noqa: BLE001 — app may be tearing down
        return []
    return [c for c in cards if c is not closed and not c._future.done()]


def waiting(app, closed=None) -> "CardKeys | None":
    """The newest card that waits: the one right above the message box."""
    cards = waiting_cards(app, closed)
    return cards[-1] if cards else None


def last_on(app) -> "CardKeys | None":
    """The waiting card the user was last on, if any: the latest on their trail that still waits."""
    cards = waiting_cards(app)
    for ref in reversed(getattr(app, "_card_trail", ())):
        card = ref()
        if card is not None and card in cards:
            return card
    return None


def poke_all(app) -> None:
    """An arrow was pressed in the message box while cards wait: none of them takes
    the row it stands on for granted when the user comes to it."""
    for card in waiting_cards(app):
        card._poked = True


def refresh_lines(app, skip=None) -> None:
    """Which key leads to a waiting card depends on the other cards and on where the
    keyboard is. `skip`: the card that is going away, or draws its own line just now."""
    focused = app.focused  # not has_focus: inside a Focus or Blur handler that flag is not turned yet
    for card in waiting_cards(app, skip):
        if card is not focused:
            card._show_keys(False)


def card_closing(app, card) -> None:
    """Call right BEFORE a card is removed. If it has the keyboard, the keyboard goes
    back to the message box now: left to itself, Textual hands it to a neighbour, and
    that can be a card that waits. If the user already took it elsewhere (another
    card, the box), it stays where they put it."""
    try:
        focused = app.focused
        box = _box(app)
        if box is not None and (focused is card or focused is None):
            app.screen.set_focus(box)  # at once: no neighbour gets it in between
        refresh_lines(app, card)
    except Exception:  # noqa: BLE001 — app may be tearing down; the card must still be removed
        pass


class CardKeys:
    """Mixin for an inline card; list it before the Textual base class. The card
    names the Static that shows its keys (KEYS_ID) and what that line says while
    the card holds the keyboard (`_keys_markup`), answers Enter in `_confirm`,
    moves its highlight in `action_move`, keeps the highlighted row in `self._idx`
    and resolves `self._future`."""

    KEYS_ID = ""
    ESC_FROM_BOX = True  # Esc pressed in the message box is this card's Esc (False: its Esc lets something go ahead)
    _hears_enter_at = _NEVER  # deaf until it has the keyboard
    _early_timer = None
    _laid_out = False  # its keys have been on screen, so the waiting line can take their place
    _poked = False     # arrows were pressed at this card from the message box, before the user came to it
    _unpicked = False  # so no row is highlighted, and Enter picks nothing, until an arrow is pressed here

    def _invited(self) -> bool:
        """The user opened this card (a command, a click), so it has the keyboard at
        once. False: the card opened by itself, and waits for the user to come to it."""
        return True

    def _keys_markup(self) -> str:
        raise NotImplementedError

    def _confirm(self) -> None:
        raise NotImplementedError

    def on_mount(self) -> None:  # after the card's own on_mount, which draws it
        if self._invited():
            self.take_keyboard()
        self._waits_from_the_start()

    def _waits_from_the_start(self, tries: int = 40) -> None:
        """Show the waiting lines, but only once this card's keys are laid out: the line
        keeps the height they have at this width (see _show_keys)."""
        try:
            laid_out = bool(self.query_one(self.KEYS_ID, Static).size.height)
        except Exception:  # noqa: BLE001 — not composed yet, or going away
            laid_out = False
        if laid_out or tries <= 0:
            self._laid_out = True
            self._show_keys(self.app.focused is self)
            refresh_lines(self.app, self)  # a new card: the others may be reached by another key now
        else:
            self.set_timer(0.02, lambda: self._waits_from_the_start(tries - 1))

    def take_keyboard(self) -> None:
        self._touch()
        self.focus()

    def _lit(self) -> int:
        """The row a card draws as highlighted: none while the choice is not the user's yet."""
        return -1 if self._unpicked else self._idx

    def _redraw(self) -> None:
        (getattr(self, "_render_choices", None) or self._render_rows)()

    def _touch(self) -> None:
        """The card just got the keyboard, the highlight moved, or a key went through
        the card: Enter is not heard for the next half second."""
        self._hears_enter_at = time.monotonic() + ENTER_GUARD

    def on_focus(self) -> None:
        self._touch()
        if self._poked:
            self._poked, self._unpicked = False, True
            self._redraw()
        trail = [ref for ref in getattr(self.app, "_card_trail", ()) if ref() is not None and ref() is not self]
        self.app._card_trail = trail + [weakref.ref(self)]
        self._show_keys(True)
        refresh_lines(self.app, self)

    def on_blur(self) -> None:
        self._hears_enter_at = _NEVER
        self._show_keys(False)
        refresh_lines(self.app, self)

    def _waits_markup(self, language: str) -> str:
        """Enter and ↓ lead from the message box to the newest card. To any other card,
        and from anywhere else, it is Tab."""
        focused = self.app.focused
        elsewhere = focused is not None and focused is not self and focused is not _box(self.app)
        direct = waiting(self.app) is self and not elsewhere  # a picker or a line of the transcript counts too
        return tr("cards.waits" if direct else "cards.waits_tab", language, colour=LAVENDER)

    def _show_keys(self, mine: bool, early: bool = False) -> None:
        """The last line says who has the keyboard: the card's keys, or how to get to it."""
        if not self._laid_out and not mine:
            return  # _waits_from_the_start shows it, once the keys' height is known
        try:
            line = self.query_one(self.KEYS_ID, Static)
        except Exception:  # noqa: BLE001 — not composed yet, or going away
            return
        language = getattr(self, "_language", None) or getattr(self, "language", "en")
        # The line never gets shorter than its keys: a card that grew when the keyboard
        # arrived moved its rows out from under the click that brought it. The keys' own
        # line count is the floor; what they measure at this width (wrapped) counts when known.
        line.styles.min_height = max(line.size.height, self._keys_markup().strip("\n").count("\n") + 1)
        if early:
            line.update(tr("cards.early", language, colour=LAVENDER))
        elif mine and self._unpicked:
            line.update(tr("cards.pick", language, colour=LAVENDER))
        elif mine:
            line.update(self._keys_markup())
        else:
            line.update(self._waits_markup(language))

    def action_confirm(self) -> None:
        """Enter. The half second is checked here, where the answer is given: every
        path an Enter can take to this card ends in this action."""
        if time.monotonic() < self._hears_enter_at:
            if self._hears_enter_at != _NEVER:  # it has the keyboard: say why nothing happened
                self._touch()
                self._show_keys(True, early=True)
                if self._early_timer is not None:
                    self._early_timer.stop()
                self._early_timer = self.set_timer(max(ENTER_GUARD, 0.1), lambda: self._show_keys(self.has_focus))
            return
        if self._unpicked:
            self._show_keys(True)  # "no row is picked yet": the arrows pressed before were not seen here
            return
        self._confirm()

    def _move(self, delta: int, rows: int) -> bool:
        """↑↓: move the highlight at once, without wrapping around. True when the card
        must draw its rows again."""
        self._touch()
        if self._unpicked:
            # The first arrow only shows the row the card stands on: the user sees where a
            # move starts from. From here on the choice is theirs, and Enter is heard again.
            self._unpicked = False
            self._show_keys(True)
            return True
        idx = self._idx + delta
        if 0 <= idx < rows:
            self._idx = idx
            return True
        box = _box(self.app)
        if idx >= rows and box is not None and box.text:
            box.focus()  # ↓ past the last row: back to the message being written
        return False

    async def escape(self) -> None:
        """The card's own Esc, for an Esc pressed in the message box."""
        for binding in self.BINDINGS:
            key, action = (binding[0], binding[1]) if isinstance(binding, tuple) else (binding.key, binding.action)
            if key == "escape":
                await self.run_action(action)
                return

    def _next_card(self) -> "CardKeys | None":
        """Tab on a card: the card above that waits. None: this is the top one, and Tab
        goes back to the message box — the round is box, newest card, …, oldest, box."""
        cards = waiting_cards(self.app)
        at = cards.index(self) if self in cards else 0
        return cards[at - 1] if at > 0 else None

    async def _on_key(self, event: events.Key) -> None:
        if event.key in ("enter", "up", "down", "escape"):
            return  # the card's bindings: confirm (where the wait lives), move, its own Esc
        self._touch()  # an Enter right behind any other key is not an answer
        box = _box(self.app)
        if event.key == "tab":
            other = self._next_card()
            if other is not None:
                event.prevent_default()
                event.stop()
                other.take_keyboard()
            elif box is not None:
                self._to_box(event, box)
            return
        if box is None:
            return
        if event.key == "ctrl+j":  # the keys that write a message without a character
            self._to_box(event, box)
            box.insert("\n")
        elif event.key == "ctrl+v":
            self._to_box(event, box)
            box.post_message(box.PasteRequested())
        elif event.key in ("backspace", "delete") and box.text:
            self._to_box(event, box)
            if event.key == "backspace":
                box.action_delete_left()
            else:
                box.action_delete_right()
        elif event.is_printable and event.character:
            event.prevent_default()
            event.stop()
            if event.character.isspace() and not box.text:
                return  # a message does not start with a space; it would only hide in the box
            box.focus()
            box.insert(event.character)

    @staticmethod
    def _to_box(event: events.Key, box) -> None:
        event.prevent_default()
        event.stop()
        box.focus()

    async def _on_paste(self, event: events.Paste) -> None:
        self._touch()
        box = _box(self.app)
        if box is not None and event.text:
            event.prevent_default()
            event.stop()
            box.focus()
            box.insert(event.text)
