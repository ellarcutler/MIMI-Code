# MSIPController

Controller for the Minuteman Missile Silo visitor center status indicator board. Runs on a Raspberry Pi and drives 10 LED display panels via SPI, responding to a USB remote and physical buttons.

---

## Definitions

### `State` (IntFlag)

Bitmask flags representing every possible indicator light on a panel. Multiple flags can be active simultaneously using bitwise OR.

| Flag | LED Color | Description |
|---|---|---|
| `OFF` | — | No flags set |
| `STRATEGIC_ALERT` | Green | Silo is on alert |
| `NOT_AUTH` | Red | Not authenticated |
| `STANDBY` | White/Grey | Standby mode |
| `CLIP_CMD` | White/Grey | Clip commanded |
| `FAULT` | Red | Fault condition |
| `WARHEAD_ALM` | Red | Warhead alarm |
| `ENABLE_CMD` | White/Grey | Enable commanded |
| `ENABLED` | Yellow | Enabled |
| `LAUNCH_CMD` | Yellow | Launch commanded |
| `LAUNCH_INHIBIT` | White/Grey | Launch inhibited |
| `LAUNCH_PROC` | White | Launch in process |
| `MISSILE_AWAY` | Green | Missile away |
| `OUTER_SECURITY` | Red | Outer security breach |
| `INNER_SECURITY` | Red | Inner security breach |
| `ANTI_JAM` | White/Grey | Anti-jam mode |

### `STATE_TO_MASK`

Maps each `State` flag to its 16-bit SPI word bit position, used when serializing state to hardware.

### `KEY_TO_CMD`

Maps USB remote keycodes (`ecodes`) to internal command strings dispatched by `dispatch_cmd`.

| Key | Command | Action |
|---|---|---|
| `1` | `1a` | Outer security (remote) |
| `2` | `2a` | Inner security (remote) |
| `3` | `3a` | Not authenticated (remote) |
| `4` | `4a` | Lamp test (remote) |
| `7` | `7a` | Step-through launch (remote) |
| `8` | `8a` | Auto launch all panels (remote) |
| `9` | `9a` | Single panel auto launch (remote) |
| Next Song | `step` | Advance one step in step-through launch |
| Home | `home` | Reset to home state |
| Volume Up | `+` | Increase volume by 2 |
| Volume Down | `-` | Decrease volume by 2 |

### Directory / File Constants

| Constant | Value | Purpose |
|---|---|---|
| `BASE_DIR` | Script directory | Base path for relative lookups |
| `SOUNDS_DIR` | `../sounds/` | WAV audio clips |
| `VOLUME_FILE` | `/boot/firmware/volume.txt` | Persistent volume storage on SD card |
| `VOLUME_FMT` | `"{:04d}\n"` | Fixed 5-byte format for in-place volume writes |

### GPIO Pins (BCM numbering)

| Constant | BCM | Physical | Purpose |
|---|---|---|---|
| `MUX0`–`MUX3` | 22, 23, 24, 25 | 15, 16, 18, 22 | Panel address multiplexer bits |
| `STROBE_CS_L` | 26 | 37 | SPI latch strobe (active-low) |
| `BTN0`–`BTN5` | 15, 14, 8, 6, 17, 27 | — | Physical push buttons |
| `OE_ALL_U_L` | 12 | 32 | Output enable for all LED drivers |
| `AUDIO_MUTE` | 4 | 7 | Hardware audio mute (active-low) |

### SPI

Opened on bus 0, device 0 at 1 MHz, SPI mode 3.

### `VISUAL_SLOTS`

Ordered list of `(label, State flag)` tuples defining the row layout of the terminal display, top to bottom.

### `PANELS`

List of 10 panel names: `A-LEFT` through `E-RIGHT`. The panel index (0–9) is used throughout to address hardware and track state.

### Panel State Globals

| Variable | Purpose |
|---|---|
| `panel_state` | Dict mapping panel index → current `State` flags |
| `panel_alarms` | Dict mapping panel index → alarm label string (e.g. `"BUZZER"`) |
| `panel_tasks` | Dict mapping panel index → running asyncio `Task` |

### Audio Constants

| Constant | Value | Purpose |
|---|---|---|
| `MAX_VOLUME` | 90 | Upper bound for ALSA and pygame volume |
| `MIN_VOLUME` | 0 | Lower bound |
| `DEFAULT_VOLUME` | 65 | Fallback when volume file is missing or invalid |
| `BUZZER_BOOST` | 1.2 | Volume multiplier for buzzer sounds |
| `BELL_BOOST` | 1.5 | Volume multiplier for bell sounds |

### Audio Globals

`bell_1s_sound`, `bell_2s_sound`, `buzzer_1s_sound`, `buzzer_2s_sound`, `pas_sound` — pygame `Sound` objects loaded at startup. `BELL_CHANNEL`, `BUZZER_CHANNEL`, `PAS_CHANNEL` — dedicated pygame mixer channels.

### Other Globals

| Variable | Purpose |
|---|---|
| `step_event` | `asyncio.Event` used to gate each step in a step-through launch sequence |
| `launch_reset_task` | Background task that waits for a launch group to finish then resets all panels |

---

## Functions

### Display

#### `clear_screen()`
Clears the terminal using ANSI escape codes.

#### `get_styled_text(label, active_flags, target_flag) -> str`
Returns an ANSI-colored string for one cell in the panel grid. Color is determined by which `State` flag is active: green for `STRATEGIC_ALERT`/`MISSILE_AWAY`, yellow for `ENABLED`/`LAUNCH_CMD`, red for security/fault flags, white/grey for everything else, and dim grey when inactive.

#### `show_panels(prompt_text="")`
Redraws the full terminal display: header row with panel names, a row per `VISUAL_SLOTS` entry with colored state cells, an alarm row, and an optional prompt string.

#### `update_panel(panel, state, alarm_text="")`
Updates `panel_state` and `panel_alarms` for one panel then calls `show_panels` to refresh the terminal.

---

### Sequences (async)

Each sequence is an `asyncio` coroutine that runs as a panel task. On `CancelledError` every sequence resets its panel to `STRATEGIC_ALERT` and re-raises.

#### `home()`
Cancels all active panel tasks and the launch reset task, stops all audio, and resets every panel to `STRATEGIC_ALERT`. Safe to call from any context.

#### `not_authenticated_sequence(panel)`
Turns on `NOT_AUTH | OUTER_SECURITY` with a 2-second buzzer, holds the red lights for 3 more seconds, then resets to `STRATEGIC_ALERT`.

#### `outer_security_sequence(panel)`
Turns on `OUTER_SECURITY` with a 2-second buzzer, holds for 3 seconds, then resets.

#### `inner_security_sequence(panel)`
Turns on `INNER_SECURITY` with a 2-second buzzer, holds for 3 seconds, then resets.

#### `lamp_test_sequence()`
Sets every `State` flag on all panels simultaneously for 3 seconds, then resets all panels to `STRATEGIC_ALERT`.

#### `launch_sequence_per_panel(panel)`
Full automatic launch sequence for one panel with random 4–8 second delays between each step:
1. `ENABLED` — bell
2. `LAUNCH_CMD` — bell
3. `LAUNCH_PROC` — bell
4. `INNER_SECURITY` — buzzer
5. `OUTER_SECURITY` — buzzer
6. `MISSILE_AWAY` — holds 10 s then post-launch fault state with buzzer

#### `launch_sequence_single_panel(panel)`
Identical to `launch_sequence_per_panel`. Used when launching only one randomly selected panel.

#### `launch_sequence_step_through(panel)`
Same six-step launch sequence but pauses at each step and waits for a `step_event` signal (triggered by the remote's Next Song key) before advancing. Plays 2-second audio cues instead of 1-second at each step.

---

### Helpers

#### `wait_for_step()`
Waits for `step_event` to be set, then clears it. Used by `launch_sequence_step_through` to gate each step.

#### `wait_for_launch_group_and_reset(tasks, delay_s=5.0)`
Awaits all tasks in a launch group, waits an extra `delay_s` seconds so the final state is visible, stops audio, then resets all panels to `STRATEGIC_ALERT`. Clears `launch_reset_task` when done.

#### `start_launch_group(panels, sequence_factory, reset_delay=5.0)`
Creates a panel task for each panel index using `sequence_factory(panel)`, then spawns `wait_for_launch_group_and_reset` as a background task stored in `launch_reset_task`.

#### `initialize_display()`
Briefly disables the LED output enable line, sends the default state to panel 0 via SPI, then re-enables output.

#### `initialize_audio()`
Initializes the pygame mixer at 44100 Hz, loads all WAV clips from `SOUNDS_DIR`, sets up the three dedicated mixer channels, and applies the persisted volume. If pygame or any file fails, all audio globals are set to `None` and audio is muted.

#### `clamp_volume(vol) -> int`
Returns `vol` clamped to `[MIN_VOLUME, MAX_VOLUME]`.

#### `save_volume(volume) -> int`
Clamps and writes the volume to `VOLUME_FILE` using a fixed 5-byte in-place write (creates the file if it doesn't exist). Returns the clamped value.

#### `load_volume() -> int`
Reads `VOLUME_FILE` and returns the stored integer. Falls back to `DEFAULT_VOLUME` (and tries to save it) on any read or parse error.

#### `apply_volume(vol)`
Sets the ALSA `Digital` mixer to `vol` and updates all pygame sound object volumes, applying `BELL_BOOST` and `BUZZER_BOOST` multipliers (capped at 1.0).

#### `change_volume(delta) -> int`
Loads the current volume, adds `delta`, clamps, saves, and applies. Returns the new volume.

#### `mixer_ready() -> bool`
Returns `True` if the pygame mixer is initialized.

#### `stop_all_sounds()`
Stops all pygame mixer channels and activates the hardware mute pin.

#### `unmute_for_playback()`
Deactivates the hardware mute pin.

#### `play_on_channel(channel, sound_obj)`
Unmutes audio and plays `sound_obj` on `channel`. If the channel is already busy the sound is queued; otherwise it starts immediately. Returns the channel, or `None` if audio is unavailable.

#### `play_bell_1s()` / `play_bell_2s()`
Play the 1-second or 2-second bell clip on `BELL_CHANNEL`.

#### `play_buzzer_1s()` / `play_buzzer_2s()`
Play the 1-second or 2-second buzzer clip on `BUZZER_CHANNEL`.

#### `play_pas()`
Plays the 3-second PAS (Public Address System) clip on `PAS_CHANNEL`.

#### `mute_audio()` / `unmute_audio()`
Directly drive the `AUDIO_MUTE` GPIO pin on/off.

#### `select_panel(n)`
Writes the 4-bit panel address `n` to `MUX3`–`MUX0`.

#### `strobe_latch()`
Pulses `STROBE_CS_L` low for 10 µs to latch SPI data into the LED drivers.

#### `flags_to_word(flags) -> int`
Converts a `State` bitmask to the 16-bit SPI word by OR-ing together the `STATE_TO_MASK` entries for each active flag.

#### `write_panel(panel, flags)`
Selects the panel via `select_panel`, sends the 16-bit word over SPI as two bytes (MSB first), then strobes the latch.

#### `set_panel(panel, flags)`
Updates `panel_state[panel]` and calls `write_panel` to push the state to hardware.

#### `handle_button_press(loop, q, cmd)`
Thread-safe callback for GPIO button presses. Posts `cmd` to `q` on the given asyncio event loop.

#### `rand_delay()`
Sleeps for a random duration between 4.0 and 8.0 seconds.

#### `step_delay(panel, min_s=4.0, max_s=8.0)`
Like `rand_delay` but tick-based (0.1 s ticks) and pause-aware — exists for legacy compatibility with step-through logic.

#### `start_panel_task(panel, coro)`
Wraps `coro` in an `asyncio.Task`, stores it in `panel_tasks[panel]`, and registers a done-callback to remove it when finished.

#### `schedule_task(panel, coro)`
Cancels and awaits any existing task for `panel`, then calls `start_panel_task`.

#### `cancel_and_wait(task)`
Cancels one task and awaits it, suppressing `CancelledError`.

#### `cancel_all_tasks(tasks)`
Cancels every task in `tasks` (skipping the caller's own task), gathers results suppressing `CancelledError`, and logs any unexpected exceptions.

---

### Input / Command Handling

#### `evdev_listener(cmd_q)`
Async loop that scans `/dev/input/by-id/*-event-kbd` for a USB keyboard remote. Reconnects automatically if the device is unplugged. On each key-down event matching `KEY_TO_CMD`, puts the corresponding command string into `cmd_q`.

#### `dispatch_cmd(cmd)`
Handles a command string from either the remote (`a` suffix) or buttons (`b` suffix):

| Command | Source | Action |
|---|---|---|
| `1a` / `1b` | Remote / Button | Outer security on random panel |
| `2a` / `2b` | Remote / Button | Inner security on random panel |
| `3a` / `3b` | Remote / Button | Not authenticated on random panel |
| `4a` / `4b` | Remote / Button | Lamp test all panels |
| `7a` | Remote | Step-through launch on one random panel |
| `8a` | Remote | Auto launch all 10 panels simultaneously |
| `9a` / `5b` | Remote / Button | Auto launch on one random panel |
| `step` | Remote | Advance step-through launch one stage |
| `home` | Remote | Reset everything to home state |
| `+` / `-` | Remote | Volume up / down by 2 |

Button commands (`b`) restrict the random panel selection to panels 0–1 (A-LEFT / A-RIGHT).

---

### Entry Point

#### `main()`
Initializes display and audio, resets to home state, registers button callbacks, spawns the `evdev_listener` task, then runs the main command dispatch loop. On shutdown, cancels all tasks, stops audio, and quits the pygame mixer.
