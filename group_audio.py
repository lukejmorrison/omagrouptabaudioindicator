#!/usr/bin/env python3
"""Mark the Hyprland group pill whose window is playing audio.

PipeWire says a process is audible. Hyprland says which toplevels that
process owns. A browser with several windows is marked only when one
title or one idle inhibitor picks a single toplevel. The painter reads
the JSON this writes; it does not decide who is audible.
"""

from __future__ import annotations

import atexit
import json
import os
import re
import select
import signal
import socket
import subprocess
import sys
import threading
import time
from enum import Enum
from pathlib import Path

LEADING_COUNT = re.compile(r"^\(\d+\)\s*")
MIN_TITLE = 12
OPTION_KEYS = (
    "height",
    "gaps_in",
    "gaps_out",
    "indicator_height",
    "indicator_gap",
    "keep_upper_gap",
    "text_padding",
    "stacked",
    "enabled",
    "disable_when_only",
)
ICON_PX = 14
ICON_EDGE = 6
ICON_GAP = 8
DEFAULT_OPTS = {
    "height": 26,
    "gaps_in": 8,
    "gaps_out": 0,
    "indicator_height": 12,
    "indicator_gap": -12,
    "keep_upper_gap": True,
    "text_padding": 10,
    "stacked": False,
    "enabled": True,
    "disable_when_only": False,
}
COALESCE_SEC = 0.08
PEAK_FLOOR = 32
PEAK_HOLD_SEC = 1.5
PEAK_RATE = 48000
PEAK_CHANNELS = 2
PEAK_SAMPLE_BYTES = 2
PEAK_WINDOW_FRAMES = 2048
METER_FIRST_SEC = 1.0
TIP_LIMIT = 96


def log(message: str) -> None:
    print(f"groupbar-audio: {message}", file=sys.stderr, flush=True)


def round_px(value: float) -> float:
    return round(float(value), 2)


def normalize_title(title: str) -> str:
    return LEADING_COUNT.sub("", title or "").strip().casefold()


def titles_match(window_title: str, media_title: str) -> bool:
    window = normalize_title(window_title)
    media = normalize_title(media_title)
    if len(window) < MIN_TITLE or len(media) < MIN_TITLE:
        return False
    return window in media or media in window


def parse_getoption(text: str):
    line = ""
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped and not stripped.startswith("set:"):
            line = stripped
            break
    if ":" not in line:
        raise ValueError(f"unparsed hyprctl option: {text!r}")
    kind, value = line.split(":", 1)
    value = value.strip()
    if kind == "bool":
        return value == "true"
    if kind == "int":
        return int(value)
    if kind == "float":
        return float(value)
    return value


def proc_parent(pid: int) -> int | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    end = stat.rfind(")")
    if end < 0:
        return None
    fields = stat[end + 2 :].split()
    if len(fields) < 2:
        return None
    try:
        return int(fields[1])
    except ValueError:
        return None


def walk_owner(pid: int, windows_by_pid: dict, parent_of) -> int | None:
    seen: set[int] = set()
    while pid and pid > 1 and pid not in seen:
        seen.add(pid)
        if pid in windows_by_pid:
            return pid
        parent = parent_of(pid)
        if parent is None or parent == pid:
            return None
        pid = parent
    return None


def player_matches_owner(player_pid, owner: int, parent_of) -> bool:
    if not isinstance(player_pid, int) or player_pid <= 0:
        return False
    if player_pid == owner:
        return True
    seen: set[int] = set()
    pid = player_pid
    while pid and pid > 1 and pid not in seen:
        seen.add(pid)
        parent = parent_of(pid)
        if parent == owner:
            return True
        if parent is None or parent == pid:
            return False
        pid = parent
    return False


def match_windows(group: list[dict], players: list[dict], parent_of) -> list[dict]:
    if not group:
        return []
    if len(group) == 1:
        return [group[0]]
    owner = group[0]["pid"]
    chosen: list[dict] = []
    seen: set[str] = set()

    def add(window: dict) -> None:
        if window["address"] in seen:
            return
        seen.add(window["address"])
        chosen.append(window)

    # One MPRIS title can match several windows, and a second window can be
    # audible without being the title MPRIS currently reports. Keep every hit.
    for player in players:
        if player.get("status") != "Playing":
            continue
        if not player_matches_owner(player.get("pid"), owner, parent_of):
            continue
        for window in group:
            if titles_match(window.get("title") or "", player.get("title") or ""):
                add(window)
    for window in group:
        if window.get("inhibiting"):
            add(window)
    if chosen:
        return chosen
    # x.com sets the window title to "X" and leaves the livestream name on MPRIS.
    playing = any(
        player.get("status") == "Playing" and player_matches_owner(player.get("pid"), owner, parent_of)
        for player in players
    )
    if not playing:
        return chosen
    shorts = [window for window in group if len(normalize_title(window.get("title") or "")) < MIN_TITLE]
    if not shorts:
        return chosen
    shorts.sort(key=lambda window: (window.get("focus_history", 99), window["address"]))
    add(shorts[0])
    return chosen


def audible_node(node: dict) -> bool:
    if node.get("media_class") != "Stream/Output/Audio" or node.get("state") != "running":
        return False
    if node.get("mute") or node.get("corked"):
        return False
    if not node.get("has_signal", True):
        return False
    pid = node.get("pid")
    return isinstance(pid, int) and pid > 0


def clip_tip(value) -> str:
    text = str(value or "").strip()
    if len(text) <= TIP_LIMIT:
        return text
    return text[:TIP_LIMIT]


def node_tips(node: dict) -> dict:
    app = clip_tip(node.get("name")) or clip_tip(node.get("binary"))
    media = clip_tip(node.get("media_name"))
    sink = clip_tip(node.get("target"))
    tips = {}
    if app:
        tips["tipApp"] = app
    if media:
        tips["tipMedia"] = media
    if sink:
        tips["tipSink"] = sink
    return tips


def audible_windows(windows: list[dict], nodes: list[dict], players: list[dict], parent_of) -> list[dict]:
    by_pid: dict[int, list[dict]] = {}
    for window in windows:
        by_pid.setdefault(window["pid"], []).append(window)
    owners: list[int] = []
    seen_owners: set[int] = set()
    for node in nodes:
        if not audible_node(node):
            continue
        owner = walk_owner(node["pid"], by_pid, parent_of)
        if owner is None or owner in seen_owners:
            continue
        seen_owners.add(owner)
        owners.append(owner)
    marked: list[dict] = []
    seen: set[str] = set()
    for owner in owners:
        for window in match_windows(by_pid.get(owner, []), players, parent_of):
            if window["address"] in seen:
                continue
            seen.add(window["address"])
            marked.append(window)
    return marked


def streams_by_owner(windows: list[dict], nodes: list[dict], parent_of) -> dict[int, int]:
    by_pid: dict[int, list[dict]] = {}
    for window in windows:
        by_pid.setdefault(window["pid"], []).append(window)
    counts: dict[int, int] = {}
    for node in nodes:
        if not audible_node(node):
            continue
        owner = walk_owner(node["pid"], by_pid, parent_of)
        if owner is None:
            continue
        counts[owner] = counts.get(owner, 0) + 1
    return counts


def tips_by_owner(windows: list[dict], nodes: list[dict], parent_of) -> dict:
    by_pid: dict[int, list[dict]] = {}
    for window in windows:
        by_pid.setdefault(window["pid"], []).append(window)
    found: dict[int, dict] = {}
    for node in nodes:
        if not audible_node(node):
            continue
        owner = walk_owner(node["pid"], by_pid, parent_of)
        if owner is None or owner in found:
            continue
        tips = node_tips(node)
        if tips:
            found[owner] = tips
    return found


def hold_marks(owner: int, fresh: list[dict], stream_count: int, memory: dict, counts: dict) -> list[str]:
    fresh_addrs = [window["address"] for window in fresh if window.get("pid") == owner]
    previous = counts.get(owner, 0)
    if stream_count <= 0:
        memory.pop(owner, None)
        counts[owner] = 0
        return []
    if stream_count < previous:
        kept = fresh_addrs
    else:
        kept = fresh_addrs + [addr for addr in memory.get(owner, []) if addr not in fresh_addrs]
    memory[owner] = kept
    counts[owner] = stream_count
    return kept


def resolve_audible(windows: list[dict], nodes: list[dict], players: list[dict], parent_of, memory: dict, counts: dict, prior: list[str] | None = None) -> list[dict]:
    if prior:
        by_seed = {window["address"]: window for window in windows}
        for addr in prior:
            window = by_seed.get(addr)
            if window is None:
                continue
            bucket = memory.setdefault(window["pid"], [])
            if addr not in bucket:
                bucket.append(addr)
    fresh = audible_windows(windows, nodes, players, parent_of)
    current = streams_by_owner(windows, nodes, parent_of)
    tips = tips_by_owner(windows, nodes, parent_of)
    by_addr = {window["address"]: window for window in windows}
    owners = set(current) | set(memory)
    marked: list[dict] = []
    seen: set[str] = set()
    for owner in owners:
        held = hold_marks(owner, [window for window in fresh if window.get("pid") == owner], current.get(owner, 0), memory, counts)
        living = []
        for addr in held:
            window = by_addr.get(addr)
            if window is None or window.get("pid") != owner or addr in seen:
                continue
            seen.add(addr)
            living.append(addr)
            stamped = dict(window)
            stamped.update(tips.get(owner, {}))
            marked.append(stamped)
        if living:
            memory[owner] = living
        else:
            memory.pop(owner, None)
    return marked


def reserved_height(opts: dict) -> float:
    # CHyprGroupBarDecoration::getPositioningInfo, v0.56.2.
    keep = 1 if opts.get("keep_upper_gap") else 0
    return (
        opts["gaps_out"] * (1 + keep)
        + opts["indicator_height"]
        + opts["indicator_gap"]
        + opts["height"]
    )


def pill_geometry(window: dict, index: int, count: int, opts: dict) -> tuple[float, float, float, float]:
    # Horizontal group bar. The gradient pill is `height` tall and sits on
    # the window. gaps_out 0 makes the reserved strip and the pill the same box.
    gaps_out = opts["gaps_out"]
    keep = 1 if opts.get("keep_upper_gap") else 0
    bar_w = (window["size"][0] - opts["gaps_in"] * (count - 1)) / count
    x = window["at"][0] + index * (bar_w + opts["gaps_in"])
    y = window["at"][1] - reserved_height(opts) + gaps_out * keep
    return x, y, bar_w, float(opts["height"])


def placements(marked: list[dict], monitors_by_id: dict, opts: dict) -> list[dict]:
    if not opts.get("enabled") or opts.get("stacked"):
        return []
    found: list[dict] = []
    for window in marked:
        grouped = window.get("grouped") or []
        count = len(grouped)
        if count < 1:
            continue
        if opts.get("disable_when_only") and count < 2:
            continue
        if window.get("fullscreen"):
            continue
        if window["address"] not in grouped:
            continue
        monitor = monitors_by_id.get(window.get("monitor_id"))
        if not monitor:
            continue
        if window.get("workspace_id") != monitor.get("active_workspace"):
            continue
        index = grouped.index(window["address"])
        x, y, width, height = pill_geometry(window, index, count, opts)
        entry = {
            "address": window["address"],
            "monitor": monitor["name"],
            "monitorX": monitor["x"],
            "monitorY": monitor["y"],
            "x": round_px(x - monitor["x"]),
            "y": round_px(y - monitor["y"]),
            "width": round_px(width),
            "height": round_px(height),
            "textPadding": int(opts["text_padding"]),
            "iconEdge": ICON_EDGE,
            "focused": window.get("focus_history") == 0,
        }
        for key in ("tipApp", "tipMedia", "tipSink"):
            if window.get(key):
                entry[key] = window[key]
        found.append(entry)
    found.sort(key=lambda entry: (entry["monitor"], entry["x"], entry["address"]))
    return found


def as_bool(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return value is True


def as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def sink_by_output(pw_objects: list) -> dict:
    descriptions = {}
    for obj in pw_objects:
        if not isinstance(obj, dict) or obj.get("type") != "PipeWire:Interface:Node":
            continue
        description = ((obj.get("info") or {}).get("props") or {}).get("node.description")
        if description:
            descriptions[obj.get("id")] = description
    found = {}
    for obj in pw_objects:
        if not isinstance(obj, dict) or obj.get("type") != "PipeWire:Interface:Link":
            continue
        props = (obj.get("info") or {}).get("props") or {}
        output_id = props.get("link.output.node")
        if output_id in found:
            continue
        description = descriptions.get(props.get("link.input.node"))
        if description:
            found[output_id] = description
    return found


def playback_nodes(pw_objects: list) -> list[dict]:
    sinks = sink_by_output(pw_objects)
    nodes = []
    for obj in pw_objects:
        if not isinstance(obj, dict) or obj.get("type") != "PipeWire:Interface:Node":
            continue
        info = obj.get("info") or {}
        props = info.get("props") or {}
        if props.get("media.class") != "Stream/Output/Audio":
            continue
        if info.get("state") != "running":
            continue
        pid = as_int(props.get("application.process.id"))
        if pid is None or pid <= 0:
            continue
        props_list = (info.get("params") or {}).get("Props") or []
        mute = False
        if isinstance(props_list, list) and props_list and isinstance(props_list[0], dict):
            mute = as_bool(props_list[0].get("mute"))
        nodes.append(
            {
                "pid": pid,
                "state": info.get("state") or "",
                "media_class": props.get("media.class") or "",
                "serial": as_int(props.get("object.serial")),
                "name": str(props.get("application.name") or ""),
                "binary": str(props.get("application.process.binary") or ""),
                "media_name": str(props.get("media.name") or ""),
                "target": sinks.get(obj.get("id")) or str(props.get("target.object") or ""),
                "mute": mute,
                "corked": as_bool(props.get("pulse.corked")),
            }
        )
    return nodes


def peak_has_signal(raw: bytes) -> bool:
    count = len(raw) // 2
    peak = 0
    for index in range(count):
        sample = int.from_bytes(raw[index * 2 : index * 2 + 2], "little", signed=True)
        magnitude = abs(sample)
        if magnitude > peak:
            peak = magnitude
        if peak > PEAK_FLOOR:
            return True
    return False


def _frame_bytes() -> int:
    return PEAK_CHANNELS * PEAK_SAMPLE_BYTES


def _window_bytes() -> int:
    return PEAK_WINDOW_FRAMES * _frame_bytes()


class _Heard(Enum):
    LOUD = "loud"
    QUIET = "quiet"
    FAILED = "failed"


class _Meter:
    def __init__(self, serial: int, proc: subprocess.Popen[bytes]) -> None:
        self.serial = serial
        self.proc = proc
        self._view: tuple[bytes | None, bool] = (None, False)
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None

    def view(self) -> tuple[bytes | None, bool]:
        return self._view

    def wait_ready(self, timeout: float) -> None:
        self._ready.wait(timeout)

    def start_reader(self) -> None:
        self._thread = threading.Thread(
            target=self._read_forever,
            name=f"pw-cat-{self.serial}",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        # Clear proc before kill returns so a second close cannot signal a reused pid.
        proc = self.proc
        self.proc = None
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
                proc.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass
        self._mark_dead()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=0.2)
        _unlink_pid(self.serial)

    def _publish(self, pcm: bytes) -> None:
        self._view = (pcm, False)
        self._ready.set()

    def _mark_dead(self) -> None:
        pcm, _dead = self._view
        self._view = (pcm, True)
        self._ready.set()

    def _read_forever(self) -> None:
        buf = bytearray()
        window = _window_bytes()
        frame = _frame_bytes()
        proc = self.proc
        stream = proc.stdout if proc is not None else None
        try:
            if stream is None:
                return
            while True:
                try:
                    chunk = stream.read(window)
                except OSError:
                    return
                if not chunk:
                    return
                buf.extend(chunk)
                if len(buf) > window:
                    extra = len(buf) - window
                    drop = extra - (extra % frame)
                    if drop:
                        del buf[:drop]
                aligned = len(buf) - (len(buf) % frame)
                if aligned >= frame:
                    self._publish(bytes(buf[:aligned]))
        finally:
            self._mark_dead()


def _pw_cat_argv(serial: int) -> list[str]:
    return [
        "pw-cat",
        "-r",
        "-a",
        "--target",
        str(serial),
        "--format",
        "s16",
        "--channels",
        str(PEAK_CHANNELS),
        "--rate",
        str(PEAK_RATE),
        "-",
    ]


def _capture_meter(serial: int) -> _Meter:
    proc = subprocess.Popen(
        _pw_cat_argv(serial),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0,
    )
    try:
        _write_pid(serial, proc.pid)
        meter = _Meter(serial, proc)
        meter.start_reader()
        return meter
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            pass
        _unlink_pid(serial)
        raise


def _meter_dir() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(runtime) / "groupbar-audio" / "meters"


def _write_pid(serial: int, pid: int) -> None:
    directory = _meter_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / str(serial)
    tmp = directory / f".{serial}.tmp"
    tmp.write_text(f"{pid}\n", encoding="utf-8")
    os.replace(tmp, path)


def _unlink_pid(serial: int) -> None:
    (_meter_dir() / str(serial)).unlink(missing_ok=True)


def _cmdline(pid: int) -> list[str] | None:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    if not raw:
        return None
    return [part.decode("utf-8", "surrogateescape") for part in raw.split(b"\0") if part]


def _is_meter_pid(pid: int, serial: int) -> bool:
    args = _cmdline(pid)
    if not args or pid <= 0:
        return False
    if Path(args[0]).name != "pw-cat":
        return False
    target = str(serial)
    for index, arg in enumerate(args[:-1]):
        if arg == "--target" and args[index + 1] == target:
            return True
    return False


def _reap_orphans() -> None:
    directory = _meter_dir()
    if not directory.is_dir():
        return
    for path in list(directory.iterdir()):
        if not path.name.isdigit():
            continue
        serial = int(path.name)
        try:
            pid = int(path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            path.unlink(missing_ok=True)
            continue
        if _is_meter_pid(pid, serial):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        path.unlink(missing_ok=True)


class PeakCache:
    def __init__(self, source=None, now=None, hold: float = PEAK_HOLD_SEC) -> None:
        self.source = _capture_meter if source is None else source
        self.now = now or time.monotonic
        self.hold = hold
        self.meters: dict[int, _Meter] = {}
        self.saved: dict[int, tuple[bool, float]] = {}
        if source is None:
            _reap_orphans()
            atexit.register(self.close_all)

    def reconcile(self, wanted: set[int]) -> None:
        wanted = set(wanted)
        for serial in list(self.meters):
            gone = serial not in wanted
            _pcm, dead = self.meters[serial].view()
            if gone or dead:
                self.meters[serial].close()
                del self.meters[serial]
                if gone:
                    self.saved.pop(serial, None)
        for serial in wanted:
            if serial not in self.meters:
                self._start(serial)

    def has_signal(self, serial: int) -> bool:
        now = self.now()
        saved = self.saved.get(serial)
        verdict = self._listen(serial)
        if verdict is _Heard.QUIET:
            fresh = False
        elif verdict is _Heard.LOUD or verdict is _Heard.FAILED:
            fresh = True
        else:
            raise AssertionError(verdict)
        heard_until = saved[1] if saved is not None else 0.0
        if fresh:
            heard_until = now + self.hold
        # A quiet window can sit inside speech. Hold the last loud mark.
        shown = fresh or now < heard_until
        self.saved[serial] = (shown, heard_until)
        return shown

    def _listen(self, serial: int) -> _Heard:
        meter = self.meters.get(serial)
        if meter is None:
            return _Heard.FAILED
        pcm, dead = meter.view()
        if not dead and (pcm is None or len(pcm) < _frame_bytes()):
            meter.wait_ready(METER_FIRST_SEC)
            pcm, dead = meter.view()
        if dead or pcm is None or len(pcm) < _frame_bytes():
            return _Heard.FAILED
        if peak_has_signal(pcm):
            return _Heard.LOUD
        return _Heard.QUIET

    def _start(self, serial: int) -> None:
        try:
            self.meters[serial] = self.source(serial)
        except Exception as exc:
            log(f"meter {serial} failed to start: {exc}")

    def close_all(self) -> None:
        for serial in list(self.meters):
            self.meters.pop(serial).close()


def stamp_signal(nodes: list[dict], peaks: PeakCache) -> None:
    wanted: set[int] = set()
    pending: list[tuple[dict, int]] = []
    for node in nodes:
        if node.get("state") != "running" or node.get("mute") or node.get("corked"):
            continue
        serial = node.get("serial")
        if not isinstance(serial, int):
            continue
        wanted.add(serial)
        pending.append((node, serial))
    peaks.reconcile(wanted)
    for node, serial in pending:
        node["has_signal"] = peaks.has_signal(serial)


def client_to_window(client: dict) -> dict | None:
    if client.get("mapped") is False:
        return None
    workspace = client.get("workspace") or {}
    try:
        at = client.get("at") or [0, 0]
        size = client.get("size") or [0, 0]
        return {
            "address": str(client.get("address") or ""),
            "pid": int(client.get("pid") or 0),
            "title": client.get("title") or "",
            "inhibiting": bool(client.get("inhibitingIdle")),
            "grouped": [str(address) for address in (client.get("grouped") or [])],
            "fullscreen": int(client.get("fullscreen") or 0),
            "workspace_id": workspace.get("id"),
            "monitor_id": client.get("monitor"),
            "at": [float(at[0]), float(at[1])],
            "size": [float(size[0]), float(size[1])],
            "focus_history": int(client.get("focusHistoryID") if client.get("focusHistoryID") is not None else 99),
        }
    except (TypeError, ValueError, IndexError):
        return None


def monitors_by_id(monitors: list) -> dict:
    found = {}
    for monitor in monitors:
        if not isinstance(monitor, dict):
            continue
        workspace = monitor.get("activeWorkspace") or {}
        found[monitor.get("id")] = {
            "name": monitor.get("name") or "",
            "x": int(monitor.get("x") or 0),
            "y": int(monitor.get("y") or 0),
            "active_workspace": workspace.get("id"),
        }
    return found


def snapshot(windows: list[dict], nodes: list[dict], players: list[dict], monitors: dict, opts: dict, parent_of, memory: dict | None = None, counts: dict | None = None, prior: list[str] | None = None) -> dict:
    marked = resolve_audible(
        windows,
        nodes,
        players,
        parent_of,
        {} if memory is None else memory,
        {} if counts is None else counts,
        prior,
    )
    return {"windows": placements(marked, monitors, opts)}


def held_addresses(path: Path) -> list[str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    windows = payload.get("windows") if isinstance(payload, dict) else None
    if not isinstance(windows, list):
        return []
    return [str(entry.get("address")) for entry in windows if isinstance(entry, dict) and entry.get("address")]


def state_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state")
    return Path(base) / "omarchy" / "group-audio.json"


def write_state(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def run_text(argv: list[str], timeout: float) -> str:
    completed = subprocess.run(
        argv,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return completed.stdout


def run_json(argv: list[str], timeout: float):
    return json.loads(run_text(argv, timeout))


def fetch_options() -> dict:
    opts = dict(DEFAULT_OPTS)
    for key in OPTION_KEYS:
        text = run_text(["hyprctl", "getoption", f"group:groupbar:{key}"], 3)
        opts[key] = parse_getoption(text)
    return opts


def fetch_players() -> list[dict]:
    listing = run_text(["busctl", "--user", "list"], 3)
    names = []
    for line in listing.splitlines():
        name = line.split()[0] if line.split() else ""
        if name.startswith("org.mpris.MediaPlayer2.") and name != "org.mpris.MediaPlayer2":
            names.append(name)
    players = []
    for name in names:
        try:
            status_raw = run_json(
                [
                    "busctl",
                    "--user",
                    "--json=short",
                    "get-property",
                    name,
                    "/org/mpris/MediaPlayer2",
                    "org.mpris.MediaPlayer2.Player",
                    "PlaybackStatus",
                ],
                3,
            )
            meta_raw = run_json(
                [
                    "busctl",
                    "--user",
                    "--json=short",
                    "get-property",
                    name,
                    "/org/mpris/MediaPlayer2",
                    "org.mpris.MediaPlayer2.Player",
                    "Metadata",
                ],
                3,
            )
            status_text = run_text(["busctl", "--user", "status", name], 3)
        except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
            continue
        pid = None
        for row in status_text.splitlines():
            if row.startswith("PID="):
                try:
                    pid = int(row.split("=", 1)[1])
                except ValueError:
                    pid = None
                break
        meta = meta_raw.get("data") if isinstance(meta_raw, dict) else {}
        title = ""
        if isinstance(meta, dict):
            titled = meta.get("xesam:title") or {}
            if isinstance(titled, dict):
                title = titled.get("data") or ""
        status = status_raw.get("data") if isinstance(status_raw, dict) else ""
        if status != "Playing" or pid is None:
            continue
        players.append({"pid": pid, "status": "Playing", "title": title})
    return players


def collect_snapshot(options: dict | None, memory: dict | None = None, counts: dict | None = None, prior: list[str] | None = None, peaks: PeakCache | None = None) -> tuple[dict, dict]:
    opts = options if options is not None else fetch_options()
    clients = run_json(["hyprctl", "clients", "-j"], 3)
    monitors = run_json(["hyprctl", "monitors", "-j"], 3)
    pw = run_json(["pw-dump"], 5)
    windows = []
    for client in clients:
        window = client_to_window(client)
        if window and window["address"] and window["pid"] > 0:
            windows.append(window)
    nodes = playback_nodes(pw)
    if peaks is not None:
        stamp_signal(nodes, peaks)
    players = fetch_players() if any(node.get("state") == "running" for node in nodes) else []
    payload = snapshot(windows, nodes, players, monitors_by_id(monitors), opts, proc_parent, memory, counts, prior)
    return payload, opts


def resolve_signature() -> tuple[str, str]:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    signature = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") or ""
    if signature and Path(runtime, "hypr", signature, ".socket2.sock").exists():
        return runtime, signature
    base = Path(runtime) / "hypr"
    candidates = []
    if base.is_dir():
        for child in base.iterdir():
            sock = child / ".socket2.sock"
            if sock.exists():
                candidates.append((sock.stat().st_mtime, child.name))
    if not candidates:
        raise OSError("no Hyprland socket2")
    candidates.sort(reverse=True)
    return runtime, candidates[0][1]


def connect_events() -> socket.socket:
    runtime, signature = resolve_signature()
    os.environ["HYPRLAND_INSTANCE_SIGNATURE"] = signature
    os.environ.setdefault("XDG_RUNTIME_DIR", runtime)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(str(Path(runtime) / "hypr" / signature / ".socket2.sock"))
    sock.setblocking(False)
    return sock


def set_nonblock(fd: int) -> None:
    import fcntl

    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)


def drain_fd(fd: int) -> tuple[bytes, bool]:
    chunks = []
    while True:
        try:
            chunk = os.read(fd, 65536)
        except BlockingIOError:
            break
        except OSError:
            return b"".join(chunks), True
        if not chunk:
            return b"".join(chunks), True
        chunks.append(chunk)
    return b"".join(chunks), False


class Publisher:
    def __init__(self) -> None:
        self.sock: socket.socket | None = None
        self.pactl: subprocess.Popen | None = None
        self.options: dict | None = None
        self.options_dirty = True
        self.last_text = ""
        self.last_marks = None
        self.memory: dict = {}
        self.counts: dict = {}
        self.seeded = False
        self.peaks = PeakCache()

    def ensure(self) -> None:
        if self.sock is None:
            self.sock = connect_events()
        if self.pactl is None or self.pactl.poll() is not None:
            self.pactl = subprocess.Popen(
                ["pactl", "subscribe"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            if self.pactl.stdout is not None:
                set_nonblock(self.pactl.stdout.fileno())

    def fds(self) -> list:
        found = []
        if self.sock is not None:
            found.append(self.sock)
        if self.pactl is not None and self.pactl.stdout is not None:
            found.append(self.pactl.stdout)
        return found

    def drain(self) -> None:
        if self.sock is not None:
            chunk, dead = drain_fd(self.sock.fileno())
            if b"configreloaded" in chunk:
                self.options_dirty = True
            if dead:
                self.sock.close()
                self.sock = None
        if self.pactl is not None and self.pactl.stdout is not None:
            _chunk, dead = drain_fd(self.pactl.stdout.fileno())
            if dead or self.pactl.poll() is not None:
                self.pactl.kill()
                self.pactl.wait(timeout=2)
                self.pactl = None

    def publish(self) -> bool:
        try:
            if self.options_dirty or self.options is None:
                self.options = fetch_options()
                self.options_dirty = False
            prior = None
            if not self.seeded:
                self.seeded = True
                prior = held_addresses(state_path())
            payload, self.options = collect_snapshot(self.options, self.memory, self.counts, prior, self.peaks)
        except (subprocess.SubprocessError, json.JSONDecodeError, OSError, ValueError) as exc:
            log(f"collect failed: {exc}")
            self.options_dirty = True
            return False
        text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        marks = tuple(entry["address"] for entry in payload["windows"])
        if text != self.last_text:
            write_state(state_path(), payload)
            self.last_text = text
        if marks != self.last_marks:
            log("marks=" + (",".join(marks) if marks else "0"))
            self.last_marks = marks
        return True


def idle_select_timeout(pending: bool, deadline: float, now: float, hold: float | None) -> float | None:
    timeout = max(0.0, deadline - now) if pending else None
    if hold is None:
        return timeout
    if timeout is None or timeout > hold:
        return hold
    return timeout


def serve() -> None:
    publisher = Publisher()
    pending = True
    deadline = time.monotonic()
    while True:
        try:
            publisher.ensure()
        except OSError as exc:
            log(f"waiting for session: {exc}")
            time.sleep(1)
            continue
        now = time.monotonic()
        hold = publisher.peaks.hold if publisher.peaks.meters else None
        timeout = idle_select_timeout(pending, deadline, now, hold)
        try:
            ready, _, _ = select.select(publisher.fds(), [], [], timeout)
        except InterruptedError:
            continue
        now = time.monotonic()
        if not ready:
            if (pending and now >= deadline) or publisher.peaks.meters:
                publisher.publish()
                pending = False
            continue
        publisher.drain()
        pending = True
        deadline = time.monotonic() + COALESCE_SEC


def main(argv: list[str]) -> int:
    if "--once" in argv:
        publisher = Publisher()
        return 0 if publisher.publish() else 1
    serve()
    return 0


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda _signum, _frame: sys.exit(0))
    signal.signal(signal.SIGINT, lambda _signum, _frame: sys.exit(0))
    try:
        raise SystemExit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        raise SystemExit(0) from None
