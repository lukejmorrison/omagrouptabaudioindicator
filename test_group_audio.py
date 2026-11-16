#!/usr/bin/env python3
"""Matcher and group-bar slot tests. No live desktop required."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import group_audio


def parents(mapping):
    def parent_of(pid):
        return mapping.get(pid)

    return parent_of


def window(**overrides):
    base = {
        "address": "0x1",
        "pid": 50,
        "title": "Untitled window title long enough",
        "inhibiting": False,
        "grouped": ["0x1"],
        "fullscreen": 0,
        "workspace_id": 23,
        "monitor_id": 2,
        "at": [-1908.0, 64.0],
        "size": [1896.0, 1004.0],
        "focus_history": 4,
    }
    base.update(overrides)
    return base


LIVE_OPTS = dict(group_audio.DEFAULT_OPTS)
ELON = "Elon Musk: A Different Conversation w/ Nikhil Kamath | Full Episode | People by WTF Ep. 16"
JENSEN = "(1) Jensen's Departure, NVIDIA's succession plan, AI doomer response"


def chromium_windows():
    titles = [
        "Starship Flight 15 / X",
        "How to unfloat and regroup an app window - Grok - Chromium",
        "(1) " + ELON,
        "New Tab - Chromium",
        JENSEN,
        "Integrations - Chromium",
        "X",
        "What is the Self Hosted Worker option in Cursor vs the Local",
    ]
    addresses = [f"0x{index}" for index in range(8)]
    windows = []
    for index, title in enumerate(titles):
        windows.append(
            window(
                address=addresses[index],
                title=title,
                inhibiting=index == 2,
                grouped=addresses,
                focus_history=0 if index == 0 else index + 1,
            )
        )
    return windows


def dp2():
    return {
        2: {"name": "DP-2", "x": -1920, "y": 0, "active_workspace": 23},
    }


class TitleTests(unittest.TestCase):
    def test_strips_count_prefix_and_case(self):
        self.assertTrue(group_audio.titles_match("(1) " + ELON.lower(), ELON))

    def test_short_title_does_not_match(self):
        self.assertFalse(group_audio.titles_match("Hello there", "Hello there friend from a longer page title"))


class MatcherTests(unittest.TestCase):
    def test_chromium_group_marks_the_playing_window_only(self):
        windows = chromium_windows()
        windows.append(
            window(
                address="0xcursor",
                pid=9,
                title="Cursor Agents",
                inhibiting=True,
                grouped=["0xcursor", "0xother"],
                monitor_id=1,
                workspace_id=12,
            )
        )
        nodes = [
            {"pid": 100, "state": "running", "media_class": "Stream/Output/Audio"},
            {"pid": 100, "state": "running", "media_class": "Stream/Output/Audio"},
            {"pid": 200, "state": "running", "media_class": "Stream/Output/Audio"},
        ]
        players = [{"pid": 50, "status": "Playing", "title": ELON}]
        marked = group_audio.audible_windows(windows, nodes, players, parents({100: 50, 50: 1, 200: 1, 9: 1}))
        self.assertEqual([item["address"] for item in marked], ["0x2"])

    def test_two_inhibitors_both_stay_marked_when_the_title_matches_neither(self):
        windows = chromium_windows()
        windows[2]["inhibiting"] = True
        windows[4]["inhibiting"] = True
        nodes = [{"pid": 100, "state": "running", "media_class": "Stream/Output/Audio"}]
        players = [{"pid": 50, "status": "Playing", "title": "A song title that matches no open window at all"}]
        marked = group_audio.audible_windows(windows, nodes, players, parents({100: 50, 50: 1}))
        self.assertEqual([item["address"] for item in marked], ["0x2", "0x4"])

    def test_same_title_on_two_windows_marks_both(self):
        windows = [
            window(address="0xa", title="(1) " + ELON, inhibiting=True, grouped=["0xa", "0xb"]),
            window(address="0xb", title="(1) " + ELON, inhibiting=False, grouped=["0xa", "0xb"]),
        ]
        nodes = [
            {"pid": 50, "state": "running", "media_class": "Stream/Output/Audio"},
            {"pid": 51, "state": "running", "media_class": "Stream/Output/Audio"},
        ]
        players = [{"pid": 50, "status": "Playing", "title": ELON}]
        marked = group_audio.audible_windows(windows, nodes, players, parents({50: 1, 51: 50}))
        self.assertEqual([item["address"] for item in marked], ["0xa", "0xb"])

    def test_title_match_keeps_a_second_inhibitor(self):
        windows = [
            window(address="0xa", title=ELON, inhibiting=False, grouped=["0xa", "0xb"]),
            window(address="0xb", title=JENSEN, inhibiting=True, grouped=["0xa", "0xb"]),
        ]
        nodes = [{"pid": 50, "state": "running", "media_class": "Stream/Output/Audio"}]
        players = [{"pid": 50, "status": "Playing", "title": ELON}]
        marked = group_audio.audible_windows(windows, nodes, players, parents({50: 1}))
        self.assertEqual([item["address"] for item in marked], ["0xa", "0xb"])

    def test_x_title_shrinking_to_x_keeps_both_marks(self):
        memory = {}
        counts = {}
        youtube = window(address="0xy", title="(1) " + ELON, focus_history=0, grouped=["0xy", "0xx"])
        x_tab = window(address="0xx", title="Starship Flight 15 / X", focus_history=4, grouped=["0xy", "0xx"])
        nodes = [
            {"pid": 100, "state": "running", "media_class": "Stream/Output/Audio"},
            {"pid": 101, "state": "running", "media_class": "Stream/Output/Audio"},
        ]
        parent = parents({100: 50, 101: 50, 50: 1})
        on_youtube = group_audio.resolve_audible(
            [youtube, x_tab],
            nodes,
            [{"pid": 50, "status": "Playing", "title": ELON}],
            parent,
            memory,
            counts,
        )
        self.assertEqual([item["address"] for item in on_youtube], ["0xy"])
        youtube["focus_history"] = 4
        x_tab["focus_history"] = 0
        on_starship = group_audio.resolve_audible(
            [youtube, x_tab],
            nodes,
            [{"pid": 50, "status": "Playing", "title": "Starship Flight 15"}],
            parent,
            memory,
            counts,
        )
        self.assertEqual([item["address"] for item in on_starship], ["0xx", "0xy"])
        x_tab["title"] = "X"
        on_short_title = group_audio.resolve_audible(
            [youtube, x_tab],
            nodes,
            [{"pid": 50, "status": "Playing", "title": "Starship Flight 15"}],
            parent,
            memory,
            counts,
        )
        self.assertEqual([item["address"] for item in on_short_title], ["0xx", "0xy"])

    def test_two_unique_titles_mark_both_windows(self):
        windows = [
            window(address="0xa", title=ELON, grouped=["0xa", "0xb"]),
            window(address="0xb", title=JENSEN, grouped=["0xa", "0xb"]),
        ]
        nodes = [{"pid": 50, "state": "running", "media_class": "Stream/Output/Audio"}]
        players = [
            {"pid": 50, "status": "Playing", "title": ELON},
            {"pid": 50, "status": "Playing", "title": JENSEN},
        ]
        marked = group_audio.audible_windows(windows, nodes, players, parents({50: 1}))
        self.assertEqual([item["address"] for item in marked], ["0xa", "0xb"])

    def test_x_livestream_uses_the_short_title_window(self):
        windows = [
            window(address="0xx", title="X", focus_history=2, grouped=["0xx", "0xy"]),
            window(address="0xold", title="X", focus_history=11, grouped=["0xold"]),
            window(address="0xy", title="(1) " + ELON, focus_history=1, grouped=["0xx", "0xy"]),
        ]
        nodes = [
            {"pid": 100, "state": "running", "media_class": "Stream/Output/Audio"},
            {"pid": 101, "state": "running", "media_class": "Stream/Output/Audio"},
        ]
        parent = parents({100: 50, 101: 50, 50: 1})
        marked = group_audio.audible_windows(
            windows,
            nodes,
            [{"pid": 50, "status": "Playing", "title": "Starship Flight 15"}],
            parent,
        )
        self.assertEqual([item["address"] for item in marked], ["0xx"])
        still_youtube = group_audio.audible_windows(
            windows,
            nodes,
            [{"pid": 50, "status": "Playing", "title": ELON}],
            parent,
        )
        self.assertEqual([item["address"] for item in still_youtube], ["0xy"])

    def test_single_toplevel_marks_without_mpris(self):
        windows = [window(address="0xmpv", pid=7, title="a film", grouped=["0xmpv", "0xterm"])]
        nodes = [{"pid": 7, "state": "running", "media_class": "Stream/Output/Audio"}]
        marked = group_audio.audible_windows(windows, nodes, [], parents({7: 1}))
        self.assertEqual([item["address"] for item in marked], ["0xmpv"])

    def test_idle_inhibit_without_playback_is_not_audio(self):
        windows = [window(address="0xcursor", pid=9, title="Cursor Agents", inhibiting=True)]
        marked = group_audio.audible_windows(windows, [], [], parents({9: 1}))
        self.assertEqual(marked, [])

    def test_idle_stream_does_not_mark(self):
        windows = [window(address="0xmpv", pid=7, grouped=["0xmpv"])]
        nodes = [{"pid": 7, "state": "idle", "media_class": "Stream/Output/Audio"}]
        marked = group_audio.audible_windows(windows, nodes, [], parents({7: 1}))
        self.assertEqual(marked, [])

    def test_silent_running_stream_does_not_mark_a_single_toplevel(self):
        windows = [window(address="0xue", pid=7, title="MyProject - Unreal Editor", grouped=["0xue", "0xterm"])]
        nodes = [
            {
                "pid": 7,
                "state": "running",
                "media_class": "Stream/Output/Audio",
                "name": "SDL Application",
                "binary": "UnrealEditor",
                "media_name": "Audio Stream",
                "has_signal": False,
            }
        ]
        marked = group_audio.audible_windows(windows, nodes, [], parents({7: 1}))
        self.assertEqual(marked, [])

    def test_silent_running_stream_drops_a_held_mark(self):
        windows = [window(address="0xue", pid=7, title="MyProject - Unreal Editor", grouped=["0xue", "0xterm"])]
        nodes = [{"pid": 7, "state": "running", "media_class": "Stream/Output/Audio", "has_signal": False}]
        marked = group_audio.resolve_audible(windows, nodes, [], parents({7: 1}), {7: ["0xue"]}, {7: 1})
        self.assertEqual(marked, [])

    def test_running_stream_with_signal_marks_a_single_toplevel(self):
        windows = [window(address="0xmpv", pid=7, title="a film", grouped=["0xmpv", "0xterm"])]
        nodes = [{"pid": 7, "state": "running", "media_class": "Stream/Output/Audio", "has_signal": True, "name": "mpv"}]
        marked = group_audio.audible_windows(windows, nodes, [], parents({7: 1}))
        self.assertEqual([item["address"] for item in marked], ["0xmpv"])

    def test_muted_or_corked_running_stream_does_not_mark(self):
        windows = [window(address="0xmpv", pid=7, grouped=["0xmpv", "0xterm"])]
        parent = parents({7: 1})
        muted = [{"pid": 7, "state": "running", "media_class": "Stream/Output/Audio", "mute": True, "has_signal": True}]
        corked = [{"pid": 7, "state": "running", "media_class": "Stream/Output/Audio", "corked": True, "has_signal": True}]
        self.assertEqual(group_audio.audible_windows(windows, muted, [], parent), [])
        self.assertEqual(group_audio.audible_windows(windows, corked, [], parent), [])

    def test_foreign_mpris_does_not_mark_the_browser(self):
        windows = chromium_windows()
        for item in windows:
            item["inhibiting"] = False
        nodes = [{"pid": 100, "state": "running", "media_class": "Stream/Output/Audio"}]
        players = [{"pid": 9, "status": "Playing", "title": ELON}]
        marked = group_audio.audible_windows(windows, nodes, players, parents({100: 50, 50: 1, 9: 1}))
        self.assertEqual(marked, [])


class SlotTests(unittest.TestCase):
    def test_equal_width_slot_on_a_negative_monitor(self):
        playing = chromium_windows()[2]
        payload = group_audio.snapshot([playing], [{"pid": 100, "state": "running", "media_class": "Stream/Output/Audio"}], [{"pid": 50, "status": "Playing", "title": ELON}], dp2(), LIVE_OPTS, parents({100: 50, 50: 1}))
        self.assertEqual(len(payload["windows"]), 1)
        slot = payload["windows"][0]
        self.assertEqual(slot["address"], "0x2")
        self.assertEqual(slot["monitor"], "DP-2")
        self.assertEqual(slot["width"], 230.0)
        self.assertEqual(slot["x"], 488.0)
        self.assertEqual(slot["y"], 38.0)
        self.assertEqual(slot["height"], 26.0)
        self.assertEqual(slot["textPadding"], 10)
        self.assertEqual(slot["iconEdge"], 6)
        self.assertFalse(slot["focused"])

    def test_speaker_clears_the_title_by_eight_pixels(self):
        opts = dict(LIVE_OPTS)
        opts["text_padding"] = group_audio.ICON_PX + group_audio.ICON_GAP + group_audio.ICON_EDGE
        playing = chromium_windows()[2]
        payload = group_audio.snapshot(
            [playing],
            [{"pid": 100, "state": "running", "media_class": "Stream/Output/Audio"}],
            [{"pid": 50, "status": "Playing", "title": ELON}],
            dp2(),
            opts,
            parents({100: 50, 50: 1}),
        )
        slot = payload["windows"][0]
        text_end = slot["x"] + slot["width"] - slot["textPadding"]
        icon_x = slot["x"] + slot["width"] - slot["iconEdge"] - group_audio.ICON_PX
        self.assertEqual(icon_x - text_end, 8)

    def test_first_slot_is_the_window_origin_in_screen_space(self):
        playing = chromium_windows()[0]
        playing["focus_history"] = 0
        playing["grouped"] = ["0x0"]
        payload = group_audio.snapshot(
            [playing],
            [{"pid": 50, "state": "running", "media_class": "Stream/Output/Audio"}],
            [],
            dp2(),
            LIVE_OPTS,
            parents({50: 1}),
        )
        slot = payload["windows"][0]
        self.assertEqual(slot["x"], 12.0)
        self.assertEqual(slot["y"], 38.0)
        self.assertTrue(slot["focused"])

    def test_fullscreen_workspace_and_stacked_skip_the_icon(self):
        playing = chromium_windows()[2]
        nodes = [{"pid": 100, "state": "running", "media_class": "Stream/Output/Audio"}]
        players = [{"pid": 50, "status": "Playing", "title": ELON}]
        parent = parents({100: 50, 50: 1})
        playing["fullscreen"] = 1
        self.assertEqual(group_audio.snapshot([playing], nodes, players, dp2(), LIVE_OPTS, parent)["windows"], [])
        playing["fullscreen"] = 0
        playing["workspace_id"] = 1
        self.assertEqual(group_audio.snapshot([playing], nodes, players, dp2(), LIVE_OPTS, parent)["windows"], [])
        playing["workspace_id"] = 23
        stacked = dict(LIVE_OPTS)
        stacked["stacked"] = True
        self.assertEqual(group_audio.snapshot([playing], nodes, players, dp2(), stacked, parent)["windows"], [])

    def test_ungrouped_window_has_no_pill(self):
        playing = window(address="0xmpv", pid=7, grouped=[], monitor_id=2)
        payload = group_audio.snapshot(
            [playing],
            [{"pid": 7, "state": "running", "media_class": "Stream/Output/Audio"}],
            [],
            dp2(),
            LIVE_OPTS,
            parents({7: 1}),
        )
        self.assertEqual(payload["windows"], [])

    def test_disable_when_only_hides_a_single_member(self):
        playing = window(address="0xmpv", pid=7, grouped=["0xmpv"], monitor_id=2)
        opts = dict(LIVE_OPTS)
        opts["disable_when_only"] = True
        payload = group_audio.snapshot(
            [playing],
            [{"pid": 7, "state": "running", "media_class": "Stream/Output/Audio"}],
            [],
            dp2(),
            opts,
            parents({7: 1}),
        )
        self.assertEqual(payload["windows"], [])

    def test_placement_names_the_stream(self):
        playing = window(address="0xmpv", pid=7, title="a film", grouped=["0xmpv", "0xterm"], monitor_id=2)
        nodes = [
            {
                "pid": 7,
                "state": "running",
                "media_class": "Stream/Output/Audio",
                "has_signal": True,
                "name": "Chromium",
                "binary": "chromium",
                "media_name": "Playback",
                "target": "Jabra SPEAK 410 Analog Stereo",
            }
        ]
        payload = group_audio.snapshot([playing], nodes, [], dp2(), LIVE_OPTS, parents({7: 1}))
        self.assertEqual(len(payload["windows"]), 1)
        slot = payload["windows"][0]
        self.assertEqual(slot.get("tipApp"), "Chromium")
        self.assertEqual(slot.get("tipMedia"), "Playback")
        self.assertEqual(slot.get("tipSink"), "Jabra SPEAK 410 Analog Stereo")

    def test_placement_uses_the_binary_when_the_app_name_is_empty(self):
        playing = window(address="0xue", pid=7, title="MyProject - Unreal Editor", grouped=["0xue", "0xterm"], monitor_id=2)
        nodes = [
            {
                "pid": 7,
                "state": "running",
                "media_class": "Stream/Output/Audio",
                "has_signal": True,
                "name": "",
                "binary": "UnrealEditor",
                "media_name": "Audio Stream",
                "target": "Jabra SPEAK 410 Analog Stereo",
            }
        ]
        payload = group_audio.snapshot([playing], nodes, [], dp2(), LIVE_OPTS, parents({7: 1}))
        slot = payload["windows"][0]
        self.assertEqual(slot.get("tipApp"), "UnrealEditor")
        self.assertEqual(slot.get("tipMedia"), "Audio Stream")


class ParseTests(unittest.TestCase):
    def test_hyprctl_option_lines(self):
        self.assertEqual(group_audio.parse_getoption("int: 26\nset: true\n"), 26)
        self.assertEqual(group_audio.parse_getoption("int: -12\nset: true\n"), -12)
        self.assertIs(group_audio.parse_getoption("bool: true\nset: false\n"), True)
        self.assertIs(group_audio.parse_getoption("bool: false\nset: false\n"), False)

    def test_playback_nodes_keep_running_output_streams(self):
        nodes = group_audio.playback_nodes(
            [
                {
                    "type": "PipeWire:Interface:Node",
                    "info": {
                        "state": "running",
                        "props": {
                            "media.class": "Stream/Output/Audio",
                            "application.process.id": "100",
                            "media.name": "Playback",
                        },
                    },
                },
                {
                    "type": "PipeWire:Interface:Node",
                    "info": {
                        "state": "running",
                        "props": {"media.class": "Stream/Output/Audio", "media.name": "ALSA Playback"},
                    },
                },
                {
                    "type": "PipeWire:Interface:Node",
                    "info": {
                        "state": "suspended",
                        "props": {"media.class": "Stream/Output/Audio", "application.process.id": 7},
                    },
                },
            ]
        )
        self.assertEqual(
            nodes,
            [
                {
                    "pid": 100,
                    "state": "running",
                    "media_class": "Stream/Output/Audio",
                    "serial": None,
                    "name": "",
                    "binary": "",
                    "media_name": "Playback",
                    "target": "",
                    "mute": False,
                    "corked": False,
                }
            ],
        )

    def test_playback_nodes_carry_stream_identity(self):
        nodes = group_audio.playback_nodes(
            [
                {
                    "type": "PipeWire:Interface:Node",
                    "id": 116,
                    "info": {
                        "state": "running",
                        "props": {
                            "media.class": "Stream/Output/Audio",
                            "application.process.id": "2464936",
                            "application.name": "SDL Application",
                            "application.process.binary": "UnrealEditor",
                            "media.name": "Audio Stream",
                            "object.serial": 983469,
                            "target.object": "alsa_output.usb-jabra",
                            "pulse.corked": False,
                        },
                        "params": {"Props": [{"mute": False}]},
                    },
                },
                {
                    "type": "PipeWire:Interface:Node",
                    "id": 33,
                    "info": {
                        "state": "running",
                        "props": {
                            "media.class": "Audio/Sink",
                            "node.description": "Jabra SPEAK 410 Analog Stereo",
                        },
                    },
                },
                {
                    "type": "PipeWire:Interface:Link",
                    "info": {"props": {"link.output.node": 116, "link.input.node": 33}},
                },
            ]
        )
        self.assertEqual(
            nodes,
            [
                {
                    "pid": 2464936,
                    "state": "running",
                    "media_class": "Stream/Output/Audio",
                    "serial": 983469,
                    "name": "SDL Application",
                    "binary": "UnrealEditor",
                    "media_name": "Audio Stream",
                    "target": "Jabra SPEAK 410 Analog Stereo",
                    "mute": False,
                    "corked": False,
                }
            ],
        )

    def test_peak_above_the_floor_is_signal(self):
        self.assertFalse(group_audio.peak_has_signal(b"\x00\x00" * 64))
        self.assertFalse(group_audio.peak_has_signal((32).to_bytes(2, "little", signed=True)))
        self.assertTrue(group_audio.peak_has_signal((33).to_bytes(2, "little", signed=True)))

    def test_peak_cache_reuses_a_serial_inside_the_window(self):
        calls = []
        clock = {"t": 0.0}

        def probe(serial):
            calls.append(serial)
            return False

        cache = group_audio.PeakCache(probe=probe, now=lambda: clock["t"], ttl=0.75)
        self.assertFalse(cache.has_signal(5))
        clock["t"] = 0.4
        self.assertFalse(cache.has_signal(5))
        self.assertEqual(calls, [5])
        clock["t"] = 0.8
        cache.has_signal(5)
        self.assertEqual(calls, [5, 5])

    def test_peak_cache_keeps_a_recent_signal_through_a_quiet_probe(self):
        answers = iter([True, False, False])
        clock = {"t": 0.0}
        cache = group_audio.PeakCache(
            probe=lambda serial: next(answers),
            now=lambda: clock["t"],
            ttl=0.75,
            hold=1.5,
        )
        self.assertTrue(cache.has_signal(5))
        clock["t"] = 0.8
        self.assertTrue(cache.has_signal(5))
        clock["t"] = 2.4
        self.assertFalse(cache.has_signal(5))

    def test_proc_parent_of_this_process(self):
        parent = group_audio.proc_parent(os.getpid())
        self.assertIsInstance(parent, int)
        self.assertGreater(parent, 1)


if __name__ == "__main__":
    unittest.main()
