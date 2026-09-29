"""Sélections X11 (PRIMARY, CLIPBOARD) lues et servies directement avec python-xlib.

Pourquoi ne pas passer par Qt : une application résidente reçoit peu
d'événements X11, son horodatage interne devient ancien, et beaucoup
d'applications refusent alors de lui transmettre leur sélection (règle ICCCM).
On procède comme `xclip` : lecture avec CurrentTime, et pour écrire, un petit
propriétaire de sélection qui répond aux demandes dans un thread jusqu'à ce
qu'une autre application copie autre chose."""

from __future__ import annotations

import logging
import select
import threading
import time

log = logging.getLogger(__name__)

_MAX_DIRECT = 200_000  # au-delà, il faudrait le protocole INCR en écriture


def _wait_event(d, predicate, timeout: float):
    deadline = time.monotonic() + timeout
    while True:
        while d.pending_events():
            ev = d.next_event()
            if predicate(ev):
                return ev
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        select.select([d.fileno()], [], [], min(remaining, 0.05))


def read_selection(selection: str = "PRIMARY", timeout: float = 1.0) -> str | None:
    """Texte de la sélection, ou None si personne ne la possède / pas de texte."""
    from Xlib import X, Xatom, display

    d = display.Display()
    try:
        sel = d.intern_atom(selection)
        if d.get_selection_owner(sel) == X.NONE:
            return None
        win = d.screen().root.create_window(0, 0, 1, 1, 0, X.CopyFromParent, event_mask=X.PropertyChangeMask)
        prop = d.intern_atom("_CORRECTEUR_SELECTION")
        incr = d.intern_atom("INCR")
        for target_name in ("UTF8_STRING", "text/plain;charset=utf-8", "STRING"):
            target = d.intern_atom(target_name)
            win.convert_selection(sel, target, prop, X.CurrentTime)
            d.flush()
            ev = _wait_event(d, lambda e: e.type == X.SelectionNotify and e.requestor.id == win.id, timeout)
            if ev is None or ev.property == X.NONE:
                continue
            reply = win.get_full_property(prop, X.AnyPropertyType, sizehint=1 << 20)
            if reply is None:
                continue
            if reply.property_type == incr:
                data = _read_incr(d, win, prop, timeout)
            else:
                data = reply.value
                win.delete_property(prop)
            if isinstance(data, str):
                data = data.encode("latin-1", "replace")
            encoding = "latin-1" if target_name == "STRING" else "utf-8"
            return bytes(data).decode(encoding, "replace")
        return None
    except Exception as exc:
        log.debug("Lecture de la sélection %s impossible : %s", selection, exc)
        return None
    finally:
        d.close()


def _read_incr(d, win, prop, timeout: float) -> bytes:
    from Xlib import X

    chunks = []
    win.delete_property(prop)  # démarre le transfert par morceaux
    d.flush()
    while True:
        ev = _wait_event(d, lambda e: e.type == X.PropertyNotify and e.atom == prop and e.state == X.PropertyNewValue,
                         timeout)
        if ev is None:
            break
        part = win.get_full_property(prop, X.AnyPropertyType, sizehint=1 << 20)
        win.delete_property(prop)
        d.flush()
        value = bytes(part.value) if part is not None else b""
        if not value:
            break
        chunks.append(value)
    return b"".join(chunks)


class SelectionOwner:
    """Possède une sélection et sert son texte aux autres applications."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: _OwnerThread | None = None

    def set_text(self, text: str, selection: str = "CLIPBOARD") -> bool:
        data = text.encode("utf-8")
        if len(data) > _MAX_DIRECT:
            return False
        with self._lock:
            owner = _OwnerThread(data, selection)
            if not owner.acquire():
                return False
            previous, self._current = self._current, owner
            owner.start()
        if previous is not None:
            previous.stop()
        return True

    def owns(self) -> bool:
        return self._current is not None and self._current.is_alive()

    def stop(self) -> None:
        with self._lock:
            current, self._current = self._current, None
        if current is not None:
            current.stop()


class _OwnerThread(threading.Thread):
    def __init__(self, data: bytes, selection: str) -> None:
        super().__init__(name=f"selection-{selection.lower()}", daemon=True)
        from Xlib import X, display

        self.data = data
        self.d = display.Display()
        self.win = self.d.screen().root.create_window(0, 0, 1, 1, 0, X.CopyFromParent, event_mask=X.PropertyChangeMask)
        self.sel = self.d.intern_atom(selection)
        self._stop = threading.Event()

    def _server_time(self) -> int:
        """Horodatage exact du serveur : on modifie une propriété de notre fenêtre."""
        from Xlib import X, Xatom

        atom = self.d.intern_atom("_CORRECTEUR_HORLOGE")
        self.win.change_property(atom, Xatom.INTEGER, 32, [0], X.PropModeAppend)
        self.d.flush()
        ev = _wait_event(self.d, lambda e: e.type == X.PropertyNotify and e.atom == atom, 1.0)
        return ev.time if ev is not None else X.CurrentTime

    def acquire(self) -> bool:
        stamp = self._server_time()
        self.win.set_selection_owner(self.sel, stamp)
        self.d.flush()
        ok = self.d.get_selection_owner(self.sel) == self.win
        if not ok:
            self.d.close()
        return ok

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        from Xlib import X

        try:
            while not self._stop.is_set():
                ev = _wait_event(self.d, lambda e: e.type in (X.SelectionRequest, X.SelectionClear), 0.25)
                if ev is None:
                    continue
                if ev.type == X.SelectionClear:
                    return  # une autre application a pris la main
                self._answer(ev)
        except Exception as exc:
            log.debug("Propriétaire de sélection arrêté : %s", exc)
        finally:
            try:
                self.win.destroy()
                self.d.close()
            except Exception:
                pass

    def _answer(self, ev) -> None:
        from Xlib import X, Xatom
        from Xlib.protocol import event as xevent

        d = self.d
        targets = d.intern_atom("TARGETS")
        utf8 = d.intern_atom("UTF8_STRING")
        text_atom = d.intern_atom("TEXT")
        plain = d.intern_atom("text/plain;charset=utf-8")
        prop = ev.property if ev.property != X.NONE else ev.target
        requestor = ev.requestor
        try:
            if ev.target == targets:
                requestor.change_property(prop, Xatom.ATOM, 32, [targets, utf8, text_atom, plain, Xatom.STRING])
            elif ev.target in (utf8, text_atom, plain):
                requestor.change_property(prop, utf8 if ev.target == text_atom else ev.target, 8, self.data)
            elif ev.target == Xatom.STRING:
                requestor.change_property(prop, Xatom.STRING, 8,
                                          self.data.decode("utf-8").encode("latin-1", "replace"))
            else:
                prop = X.NONE
        except Exception:
            prop = X.NONE
        notify = xevent.SelectionNotify(time=ev.time, requestor=requestor, selection=ev.selection,
                                        target=ev.target, property=prop)
        requestor.send_event(notify, event_mask=0)
        d.flush()
