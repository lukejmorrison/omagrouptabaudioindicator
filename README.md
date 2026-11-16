# Group Tab Audio Indicator

A speaker on the Hyprland group pill of the window that is playing audio.

Hyprland's group bar paints the window title and has no icon slot. This plugin is two pieces:

- `group_audio.py` watches PipeWire and Hyprland, then writes `~/.local/state/omarchy/group-audio.json`.
- `Service.qml` draws a click-through speaker on that pill. Clicks still reach the group bar.

A process with one window is marked while its playback stream is running and a short sample of that stream is louder than silence. A browser with several windows is marked when a Playing MPRIS title, or an idle inhibitor, picks the toplevel. Capture, monitor, and loopback nodes are ignored. Muted and corked streams are ignored.

Plugin id: `wizwam.group-audio`.

## Install

Requires Omarchy with shell plugins, Hyprland, PipeWire, and `pactl`.

```bash
omarchy plugin add https://github.com/lukejmorrison/omagrouptabaudioindicator.git --enable --yes
~/.config/omarchy/plugins/wizwam.group-audio/install.sh
```

`omarchy plugin add` installs the painter. `install.sh` adds the user service `groupbar-audio.service`. Neither command edits Hyprland config.

Give the title room for the glyph. In `~/.config/hypr/looknfeel.lua`, inside `group.groupbar`:

```lua
text_padding = 28,
```

That is 14px for the speaker, 8px of gap, and 6px from the pill edge. Hyprland insets the title by that amount on both sides. Reload Hyprland after saving.

Update the checkout with:

```bash
omarchy plugin update wizwam.group-audio
```

Then run `install.sh` again so the service restarts on the new code.

## Remove

```bash
~/.config/omarchy/plugins/wizwam.group-audio/uninstall.sh
omarchy plugin remove wizwam.group-audio
```

`uninstall.sh` disables and deletes `groupbar-audio.service`. `omarchy plugin remove` drops the shell plugin. The `text_padding` line stays until you edit it.

## Dependencies

No extra packages and no network. The service uses the Python 3 standard library, `hyprctl`, `pw-dump`, `pw-cat`, `pactl`, and `busctl`.

The pill rectangle matches Hyprland 0.56.2's group bar: equal-width slots in the strip above the window. Height, gaps, and indicator values are read from `hyprctl getoption`.

## Tests

```bash
python3 -m unittest test_group_audio.py
```

## License

MIT. See [LICENSE](LICENSE).
