"""Hotkey beat logger. Runs next to OBS while you play.

Each hotkey press stores "this beat happened N seconds into the recording".
With the OBS websocket on, N comes straight from OBS, so tags line up exactly
with the recording file, and the file path is saved when you stop recording.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from .config import ROOT

TAG_KEYS = ["problem", "attempt", "obstacle", "retry", "result", "moment"]


def _beep(ok: bool = True) -> None:
    try:
        import winsound
        winsound.Beep(1200 if ok else 400, 80)
    except Exception:
        pass


class ObsClock:
    """Recording time read from OBS over its built-in websocket (OBS 28+)."""

    def __init__(self, cfg: dict, on_stopped):
        import obsws_python as obs
        o = cfg["obs"]
        self.req = obs.ReqClient(host=o["host"], port=o["port"], password=o["password"], timeout=3)
        self.events = obs.EventClient(host=o["host"], port=o["port"], password=o["password"])

        def on_record_state_changed(data):
            if getattr(data, "output_state", "") == "OBS_WEBSOCKET_OUTPUT_STOPPED":
                on_stopped(getattr(data, "output_path", None))

        self.events.callback.register(on_record_state_changed)

    def now(self) -> float | None:
        r = self.req.get_record_status()
        if not r.output_active:
            return None
        return r.output_duration / 1000.0


class WallClock:
    """Fallback: you press the record hotkey together with OBS's own record key."""

    def __init__(self):
        self.started: float | None = None

    def toggle(self) -> bool:
        self.started = None if self.started else time.monotonic()
        return self.started is not None

    def now(self) -> float | None:
        return None if self.started is None else time.monotonic() - self.started


class Logger:
    def __init__(self, cfg: dict, challenge: str, out_dir: Path | None = None):
        self.cfg = cfg
        self.challenge = challenge
        self.out_dir = Path(out_dir or ROOT / "sessions")
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.session: dict | None = None
        self.path: Path | None = None

    def _new_session(self, clock_kind: str) -> None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.path = self.out_dir / f"{stamp}.beats.json"
        self.session = {"challenge": self.challenge, "created": stamp, "clock": clock_kind,
                        "recording": None, "tags": []}
        self.save()

    def save(self) -> None:
        if self.session and self.path:
            self.path.write_text(json.dumps(self.session, indent=2), encoding="utf-8")

    def tag(self, beat: str, t: float | None, clock_kind: str) -> None:
        if t is None:
            print(f"  [{beat}] ignored - not recording")
            _beep(False)
            return
        if self.session is None:
            self._new_session(clock_kind)
        self.session["tags"].append({"t": round(t, 2), "beat": beat})
        self.save()
        print(f"  {_fmt(t)}  {beat.upper()}")
        _beep()

    def undo(self) -> None:
        if self.session and self.session["tags"]:
            gone = self.session["tags"].pop()
            self.save()
            print(f"  removed {gone['beat'].upper()} at {_fmt(gone['t'])}")

    def finish(self, recording_path: str | None) -> None:
        if self.session is None:
            return
        self.session["recording"] = recording_path
        self.save()
        print(f"\nSaved {len(self.session['tags'])} tags -> {self.path}")
        if recording_path:
            print(f"Recording: {recording_path}")
        print(f"Next: python -m clipfarm daily \"{self.path}\"\n")
        self.session = None
        self.path = None


def _fmt(t: float) -> str:
    return f"{int(t // 60):02d}:{t % 60:05.2f}"


def run(cfg: dict, challenge: str) -> None:
    try:
        import keyboard
    except ImportError:
        raise SystemExit("Missing the 'keyboard' package: pip install -r requirements.txt")

    log = Logger(cfg, challenge)
    keys = cfg["hotkeys"]
    clock = None
    kind = "obs"
    if cfg["obs"]["use_websocket"]:
        try:
            clock = ObsClock(cfg, on_stopped=log.finish)
            print("Connected to OBS - tags are timed from the recording itself.")
        except Exception as e:
            print(f"Could not reach the OBS websocket ({e}).")
            print("Falling back to manual mode.")
    if clock is None:
        kind = "clock"
        clock = WallClock()

        def toggle():
            if clock.toggle():
                print("  recording marked as STARTED")
            else:
                print("  recording marked as STOPPED")
                log.finish(None)
        keyboard.add_hotkey(keys["record"], toggle)
        print(f"Bind OBS 'Start/Stop Recording' to {keys['record'].upper()} too, "
              "so one press starts both.")

    for beat in TAG_KEYS:
        keyboard.add_hotkey(keys[beat], lambda b=beat: log.tag(b, clock.now(), kind))
    keyboard.add_hotkey(keys["undo"], log.undo)

    print(f"\nChallenge: {challenge or '(none - set it with --challenge)'}")
    for beat in TAG_KEYS:
        print(f"  {keys[beat].upper():>4}  {beat}")
    print(f"  {keys['undo'].upper():>4}  undo last tag")
    print("Press the key just AFTER the moment happens. Ctrl+C here to quit.\n")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        if log.session:
            log.finish(log.session.get("recording"))
