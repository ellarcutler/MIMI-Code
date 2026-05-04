import asyncio
import random
import sys
from enum import IntFlag, auto
import evdev
from evdev import InputDevice, ecodes
import contextlib
from gpiozero import OutputDevice, Button
import spidev
import time
import alsaaudio
import os
import pygame
import glob

# ---------- STATES ----------

class State(IntFlag):
    OFF             = 0
    STRATEGIC_ALERT = auto() # Green
    NOT_AUTH        = auto()
    STANDBY         = auto()
    CLIP_CMD        = auto()
    FAULT           = auto()
    WARHEAD_ALM     = auto()
    ENABLE_CMD      = auto()
    ENABLED         = auto() # Yellow
    LAUNCH_CMD      = auto() # Yellow
    LAUNCH_INHIBIT  = auto()
    LAUNCH_PROC     = auto() # White
    MISSILE_AWAY    = auto() # Green
    OUTER_SECURITY  = auto() # Red
    INNER_SECURITY  = auto() # Red
    ANTI_JAM        = auto()

STATE_TO_MASK = {
    State.ANTI_JAM:        0x0001,
    State.INNER_SECURITY:  0x0002,
    State.OUTER_SECURITY:  0x0004,
    State.MISSILE_AWAY:    0x0008,
    State.LAUNCH_PROC:     0x0010,
    State.LAUNCH_INHIBIT:  0x0020,
    State.LAUNCH_CMD:      0x0040,
    State.ENABLED:         0x0080,
    State.ENABLE_CMD:      0x0100,
    State.WARHEAD_ALM:     0x0200,
    State.FAULT:           0x0400,
    State.CLIP_CMD:        0x0800,
    State.STANDBY:         0x1000,
    State.NOT_AUTH:        0x2000,
    State.STRATEGIC_ALERT: 0x4000,
}

# ---------- KEYS -----------
KEY_TO_CMD = {
    ecodes.KEY_1: "1a", # OUTER (remote)
    ecodes.KEY_2: "2a", # INNER (remote)
    ecodes.KEY_3: "3a", # NOT AUTH (remote)
    ecodes.KEY_4: "4a", # LAMP TEST (remote)
    ecodes.KEY_7: "7a", # STEP LAUNCH (remote)
    ecodes.KEY_8: "8a", # AUTO LAUNCH (remote)
    ecodes.KEY_9: "9a", # SINGLE PANEL AUTO LAUNCH (remote)
    ecodes.KEY_NEXTSONG : "step", # LAUNCH SEQUENCE STEP (remote)
    ecodes.KEY_HOMEPAGE: "home", # HOME
    ecodes.KEY_VOLUMEUP: "+", # VOLUME UP
    ecodes.KEY_VOLUMEDOWN: "-", # VOLUME DOWN
}

# ---------- DIRECTORIES -----------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SOUNDS_DIR = os.path.join(BASE_DIR, "..", "sounds")
VOLUME_FILE = "/boot/firmware/volume.txt" # Store volume persistently on the SSD
VOLUME_FMT  = "{:04d}\n"   # always exactly 5 bytes: e.g. "0065\n"
VOLUME_SIZE = 5

# ---------- PINS -----------
# BCM numbers
MUX0 = OutputDevice(22) # physical 15
MUX1 = OutputDevice(23) # physical 16
MUX2 = OutputDevice(24) # physical 18
MUX3 = OutputDevice(25) # physical 22
STROBE_CS_L = OutputDevice(26, active_high=True, initial_value=True) # physical 37
BTN0 = Button(15, pull_up=True, bounce_time=0.1)
BTN1 = Button(14, pull_up=True, bounce_time=0.1)
BTN2 = Button(8,  pull_up=True, bounce_time=0.1)
BTN3 = Button(6,  pull_up=True, bounce_time=0.1)
BTN4 = Button(17, pull_up=True, bounce_time=0.1)
BTN5 = Button(27, pull_up=True, bounce_time=0.1)
OE_ALL_U_L = OutputDevice(12, active_high=True, initial_value=True) # physical 32
AUDIO_MUTE = OutputDevice(4, active_high=False, initial_value=True) # physical 7


# ---------- SPI SETUP ----------
spi = spidev.SpiDev()
spi.open(0,0)
spi.max_speed_hz = 1_000_000
spi.mode = 3

# ---------- VISUAL LAYOUT ----------

VISUAL_SLOTS = [
    ("STRATEGIC ALERT", State.STRATEGIC_ALERT),
    ("NOT AUTHENTICATED", State.NOT_AUTH),
    ("STANDBY", State.STANDBY),
    ("CLIP COMMANDED", State.CLIP_CMD),
    ("FAULT", State.FAULT),
    ("WARHEAD ALARM", State.WARHEAD_ALM),
    ("ENABLE COMMAND", State.ENABLE_CMD),
    ("ENABLED", State.ENABLED),
    ("LAUNCH COMMAND", State.LAUNCH_CMD),
    ("LAUNCH INHIBIT", State.LAUNCH_INHIBIT),
    ("LAUNCH IN PROCESS", State.LAUNCH_PROC),
    ("MISSILE AWAY", State.MISSILE_AWAY),
    ("OUTER SECURITY", State.OUTER_SECURITY),
    ("INNER SECURITY", State.INNER_SECURITY),
    ("ANTI-JAM MODE", State.ANTI_JAM),
    ]

PANELS = ["A-LEFT", "A-RIGHT", "B-LEFT", "B-RIGHT", "C-LEFT", "C-RIGHT", "D-LEFT", "D-RIGHT", "E-LEFT", "E-RIGHT"] 

panel_state = {i: State.STRATEGIC_ALERT for i in range(len(PANELS))}
panel_alarms = {i: "" for i in range(len(PANELS))}
panel_tasks = {}

# ---------- AUDIO SETUP ----------
MAX_VOLUME = 90
MIN_VOLUME = 0
DEFAULT_VOLUME = 65

# Pygame sound objects
bell_1s_sound = None
bell_2s_sound = None
buzzer_1s_sound = None
buzzer_2s_sound = None
pas_sound = None

# Mixer channels
BELL_CHANNEL = None
BUZZER_CHANNEL = None
PAS_CHANNEL = None

# ---------- GLOBALS ----------
step_event = asyncio.Event()
launch_reset_task = None

# ---------- DISPLAY ENGINE ----------

def clear_screen():
    sys.stdout.write("\033[H\033[J")
    sys.stdout.flush()

def get_styled_text(label, active_flags, target_flag):
    width = 17 
    text = f" {label:<{width-1}}"
    
    if target_flag in active_flags:
        if target_flag in (State.STRATEGIC_ALERT, State.MISSILE_AWAY):
            return f"\033[42;30m{text}\033[0m" # Green
        elif target_flag in (State.ENABLED, State.LAUNCH_CMD):
            return f"\033[43;30m{text}\033[0m" # Yellow
        elif target_flag in (State.OUTER_SECURITY, State.INNER_SECURITY, State.NOT_AUTH, State.FAULT, State.WARHEAD_ALM):
             return f"\033[41;37m{text}\033[0m" # Red
        else:
             return f"\033[47;30m{text}\033[0m" # White/Grey
    else:
        return f"\033[90m{text}\033[0m"

def show_panels(prompt_text=""):
    clear_screen()
    print("\n === STRATEGIC SILO STATUS INDICATOR (VISITOR CENTER) ===\n")
    
    header_row = "   "
    for name in PANELS:
        header_row += f" {name:^17} "
    print(header_row)
    print("   " + ("=" * (len(PANELS) * 19)))

    for label, flag in VISUAL_SLOTS:
        row_str = "   "
        for i in range(len(PANELS)):
            current_state = panel_state[i]
            styled_block = get_styled_text(label, current_state, flag)
            row_str += f"|{styled_block}|"
        print(row_str)

    alarm_row = "   "
    for i in range(len(PANELS)):
        alarm_text = panel_alarms[i]
        if alarm_text:
            alarm_row += f" \033[91m{alarm_text:^17}\033[0m "
        else:
            alarm_row += f" {' ':^17} "
    print("\n" + alarm_row)

    print("-" * 60)
    print(prompt_text, end="", flush=True)

def update_panel(panel: int, state: State, alarm_text: str = ""):
    panel_state[panel] = state
    panel_alarms[panel] = alarm_text
    show_panels("\n[1] Out [2] In [3] Launch [4] Not Auth [5] Lamp Test [0] Reset > ")

# ---------- FUNCTIONS ----------
async def home():
    global launch_controller, launch_reset_task
    launch_controller = None

    current = asyncio.current_task()

    # Cancel any pending "reset after launch group" task,
    # but do not cancel ourselves if home() was called from it.
    if launch_reset_task is not None and launch_reset_task is not current:
        launch_reset_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await launch_reset_task
    launch_reset_task = None

    # Cancel all panel tasks and wait for them
    tasks = list(panel_tasks.values())
    panel_tasks.clear()
    await cancel_all_tasks(tasks)

    # Stop audio after all task cleanup has finished
    stop_all_sounds()

    # Reset all panels to default state
    for i in range(len(PANELS)):
        panel_state[i] = State.STRATEGIC_ALERT
        panel_alarms[i] = ""
        set_panel(i, State.STRATEGIC_ALERT)

    show_panels("\n[1] Out [2] In [3] Launch [4] Not Auth [5] Lamp Test [0] Reset > ")

async def not_authenticated_sequence(panel: int):
    try:
        # Not Auth (2) and Outer Sec (3) turn ON.
        active_state = State.NOT_AUTH
        
        # Turn lights on + Buzzer
        update_panel(panel, active_state, "BUZZER")
        set_panel(panel, active_state)
        play_buzzer_2s()

        await rand_delay()

        # Not Auth (2) and Outer Sec (3) turn ON.
        active_state = State.NOT_AUTH | State.OUTER_SECURITY
        
        # Turn lights on + Buzzer
        update_panel(panel, active_state, "BUZZER")
        set_panel(panel, active_state)
        play_buzzer_2s()

        # Hold state for 3 seconds
        await rand_delay()

        # Silence buzzer, keep lights red
        update_panel(panel, active_state, "") 
        
        # Reset to home (Green Light)
        update_panel(panel, State.STRATEGIC_ALERT, "") 
        set_panel(panel, State.STRATEGIC_ALERT)
    
    except asyncio.CancelledError:
        update_panel(panel, State.STRATEGIC_ALERT, "")
        set_panel(panel, State.STRATEGIC_ALERT)
        stop_all_sounds()
        raise

async def lamp_test_sequence():
    try:
        # Calculate a state that is ALL flags combined
        # We start with 0 and OR (|) every possible flag into it
        all_on = State.OFF
        for flag in State:
            all_on |= flag
            
        # Apply to all panels immediately
        for panel in range(len(PANELS)):
            update_panel(panel, all_on)
            set_panel(panel, all_on)
            
        # Hold for 3 seconds
        await asyncio.sleep(3.0)

        # Clear all panels back to home state
        for panel in range(len(PANELS)):
            update_panel(panel, State.STRATEGIC_ALERT, "")
            set_panel(panel, State.STRATEGIC_ALERT)
        
    except asyncio.CancelledError:
        for panel in range(len(PANELS)):
            update_panel(panel, State.STRATEGIC_ALERT, "")
            set_panel(panel, State.STRATEGIC_ALERT)
        raise


async def outer_security_sequence(panel: int):
    try:
        # Outer Security ON, Buzzer X
        base = State.STRATEGIC_ALERT
        update_panel(panel, base | State.OUTER_SECURITY, "BUZZER")
        set_panel(panel, base | State.OUTER_SECURITY)
        play_buzzer_2s()

        # Turn off buzzer after 2 seconds
        await asyncio.sleep(2.0)
        update_panel(panel, base | State.OUTER_SECURITY, "") # Silence alarm
        set_panel(panel, base | State.OUTER_SECURITY)

        # Hold state for 5 seconds
        await asyncio.sleep(3.0)
        update_panel(panel, base, "") # Reset to home state
        set_panel(panel, base)
    
    except asyncio.CancelledError:
        # handle task cancellation
        update_panel(panel, State.STRATEGIC_ALERT, "")
        set_panel(panel, State.STRATEGIC_ALERT)
        stop_all_sounds()
        raise

async def inner_security_sequence(panel: int):
    try:
        # Inner Security ON, Buzzer X
        base = State.STRATEGIC_ALERT
        update_panel(panel, base | State.INNER_SECURITY, "BUZZER")
        set_panel(panel, base | State.INNER_SECURITY)
        play_buzzer_2s()

        # Turn off buzzer after 2 seconds
        await asyncio.sleep(2.0)
        update_panel(panel, base | State.INNER_SECURITY, "") # Silence alarm

        # Hold state for 5 seconds
        await asyncio.sleep(3.0)
        update_panel(panel, base, "") # Reset to home state
        set_panel(panel, base)
    
    except asyncio.CancelledError:
        # handle task cancellation
        update_panel(panel, State.STRATEGIC_ALERT, "")
        set_panel(panel, State.STRATEGIC_ALERT)
        stop_all_sounds()
        raise


async def launch_sequence_per_panel(panel: int):
    try:
        current_flags = State.STRATEGIC_ALERT

        await rand_delay() # random delay before starting sequence

        # Go through launch sequence
        current_flags |= State.ENABLED
        update_panel(panel, current_flags, "BELL")
        set_panel(panel, current_flags)
        play_bell_1s()
        await rand_delay()

        current_flags |= State.LAUNCH_CMD
        update_panel(panel, current_flags, "BELL")
        set_panel(panel, current_flags)
        play_bell_1s()
        await rand_delay()

        current_flags |= State.LAUNCH_PROC
        update_panel(panel, current_flags, "BELL")
        set_panel(panel, current_flags)
        play_bell_2s()
        await rand_delay()

        current_flags |= State.INNER_SECURITY
        update_panel(panel, current_flags, "BUZZER")
        set_panel(panel, current_flags)
        play_buzzer_1s()
        await rand_delay()

        current_flags |= State.OUTER_SECURITY
        update_panel(panel, current_flags, "BUZZER")
        set_panel(panel, current_flags)
        play_buzzer_1s()
        await rand_delay()

        current_flags |= State.MISSILE_AWAY
        update_panel(panel, current_flags, "LIFTOFF")
        set_panel(panel, current_flags)

        # Switch to "after launch" state after 10 seconds
        await asyncio.sleep(10.0)
        current_flags = (State.NOT_AUTH | State.FAULT | State.WARHEAD_ALM | State.MISSILE_AWAY |
                         State.OUTER_SECURITY | State.INNER_SECURITY)
        update_panel(panel, current_flags, "BUZZER")
        set_panel(panel, current_flags)
        play_buzzer_2s()

        # Turn off buzzer after 2 seconds
        await asyncio.sleep(2.0)
        update_panel(panel, current_flags, "")
        set_panel(panel, current_flags)
        

    except asyncio.CancelledError:
        # handle task cancellation
        update_panel(panel, State.STRATEGIC_ALERT, "")
        set_panel(panel, State.STRATEGIC_ALERT)
        stop_all_sounds()
        raise

async def launch_sequence_step_through(panel: int):
    try:
        current_flags = State.STRATEGIC_ALERT

        await rand_delay()
        await wait_for_step()

        current_flags |= State.ENABLED
        update_panel(panel, current_flags, "BELL")
        set_panel(panel, current_flags)
        play_bell_2s()
        await wait_for_step()

        current_flags |= State.LAUNCH_CMD
        update_panel(panel, current_flags, "BELL")
        set_panel(panel, current_flags)
        play_bell_2s()
        await wait_for_step()

        current_flags |= State.LAUNCH_PROC
        update_panel(panel, current_flags, "BELL")
        set_panel(panel, current_flags)
        play_bell_2s()
        await wait_for_step()

        current_flags |= State.INNER_SECURITY
        update_panel(panel, current_flags, "BUZZER")
        set_panel(panel, current_flags)
        play_buzzer_2s()
        await wait_for_step()

        current_flags |= State.OUTER_SECURITY
        update_panel(panel, current_flags, "BUZZER")
        set_panel(panel, current_flags)
        play_buzzer_2s()
        await wait_for_step()

        current_flags |= State.MISSILE_AWAY
        update_panel(panel, current_flags, "LIFTOFF")
        set_panel(panel, current_flags)
        await wait_for_step()

        current_flags = (
            State.NOT_AUTH | State.FAULT | State.WARHEAD_ALM |
            State.MISSILE_AWAY | State.OUTER_SECURITY | State.INNER_SECURITY
        )
        update_panel(panel, current_flags, "BUZZER")
        set_panel(panel, current_flags)
        play_buzzer_2s()
        await wait_for_step()

        update_panel(panel, State.STRATEGIC_ALERT, "")
        set_panel(panel, State.STRATEGIC_ALERT)

    except asyncio.CancelledError:
        update_panel(panel, State.STRATEGIC_ALERT, "")
        set_panel(panel, State.STRATEGIC_ALERT)
        raise


# ---------- HELPERS ----------
async def wait_for_step():
    global step_event
    await step_event.wait()
    step_event.clear()

async def wait_for_launch_group_and_reset(tasks, delay_s=5.0):
    global launch_reset_task, launch_controller

    try:
        # Wait until every launch panel task has finished
        await asyncio.gather(*tasks, return_exceptions=True)

        # Extra time to keep the final state visible
        await asyncio.sleep(delay_s)

        launch_controller = None
        stop_all_sounds()

        # Reset all panels together
        for panel in range(len(PANELS)):
            update_panel(panel, State.STRATEGIC_ALERT, "")
            set_panel(panel, State.STRATEGIC_ALERT)

        show_panels("\n[1] Out [2] In [3] Launch [4] Not Auth [5] Lamp Test [0] Reset > ")

    except asyncio.CancelledError:
        raise

    finally:
        if launch_reset_task is asyncio.current_task():
            launch_reset_task = None


def start_launch_group(panels, sequence_factory, reset_delay=5.0):
    global launch_reset_task

    tasks = []
    for panel in panels:
        tasks.append(start_panel_task(panel, sequence_factory(panel)))

    launch_reset_task = asyncio.create_task(
        wait_for_launch_group_and_reset(tasks, delay_s=reset_delay)
    )

def initialize_display():
    OE_ALL_U_L.on() # disable

    # add function to drive all LEDs to off
    update_panel(0, State.STRATEGIC_ALERT, "")
    set_panel(0, State.STRATEGIC_ALERT)

    time.sleep(10e-6)

    OE_ALL_U_L.off()

def initialize_audio():
    global bell_1s_sound, bell_2s_sound, buzzer_1s_sound, buzzer_2s_sound, pas_sound
    global BELL_CHANNEL, BUZZER_CHANNEL, PAS_CHANNEL

    vol = load_volume()

    try:
        # Start pygame mixer
        pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=1024)

        # Give ourselves several channels so sounds can overlap
        pygame.mixer.set_num_channels(16)

        BELL_CHANNEL = pygame.mixer.Channel(0)
        BUZZER_CHANNEL = pygame.mixer.Channel(1)
        PAS_CHANNEL = pygame.mixer.Channel(2)

        # Load clips once at startup
        bell_1s_sound = pygame.mixer.Sound(os.path.join(SOUNDS_DIR, "bell_1s.wav"))
        bell_2s_sound = pygame.mixer.Sound(os.path.join(SOUNDS_DIR, "bell_2s.wav"))
        buzzer_1s_sound = pygame.mixer.Sound(os.path.join(SOUNDS_DIR, "buzzer_1s.wav"))
        buzzer_2s_sound = pygame.mixer.Sound(os.path.join(SOUNDS_DIR, "buzzer_2s.wav"))
        pas_sound = pygame.mixer.Sound(os.path.join(SOUNDS_DIR, "pas_3s.wav"))

        apply_volume(vol)
        print("Audio initialized")

    except (pygame.error, FileNotFoundError, OSError) as e:
        print(f"Warning: audio disabled: {e}", file=sys.stderr)

        # Leave all audio objects as None so play_* functions safely do nothing
        bell_1s_sound = None
        bell_2s_sound = None
        buzzer_1s_sound = None
        buzzer_2s_sound = None
        pas_sound = None

        BELL_CHANNEL = None
        BUZZER_CHANNEL = None
        PAS_CHANNEL = None

        # Make sure pygame is not left half-initialized
        with contextlib.suppress(Exception):
            pygame.mixer.quit()

        mute_audio()

    return vol

def clamp_volume(vol: int) -> int:
    return max(MIN_VOLUME, min(MAX_VOLUME, vol))

def save_volume(volume: int):
    cleaned = clamp_volume(int(volume))

    try:
        # Open in r+b to overwrite in-place (never changes file size)
        with open(VOLUME_FILE, "r+b") as f:
            f.write(VOLUME_FMT.format(cleaned).encode("ascii"))
            f.flush()
            os.fsync(f.fileno())

    except (FileNotFoundError, OSError):
        # File doesn't exist yet — create it at the correct fixed size
        with open(VOLUME_FILE, "wb") as f:
            f.write(VOLUME_FMT.format(cleaned).encode("ascii"))
            f.flush()
            os.fsync(f.fileno())

    return cleaned

def load_volume() -> int:
    try:
        with open(VOLUME_FILE, "r", encoding="ascii") as f:
            raw = f.read().strip()

            if not raw:
                raise ValueError("Volume file is empty")

            parsed = int(raw)

            if parsed < MIN_VOLUME or parsed > MAX_VOLUME:
                raise ValueError(f"Volume {parsed} out of range")

            return parsed

    except (FileNotFoundError, ValueError) as e:
        print(f"Error loading volume ({e}), using default {DEFAULT_VOLUME}", file=sys.stderr)
        try:
            return save_volume(DEFAULT_VOLUME)
        except OSError:
            return DEFAULT_VOLUME  # give up writing, just use the default in memory

    except OSError as e:
        print(f"Error accessing volume file ({e}), using default {DEFAULT_VOLUME}", file=sys.stderr)
        return DEFAULT_VOLUME  # Don't try to write on a read error

def apply_volume(vol: int):
    # Apply volume to ALSA mixer and pygame sounds
    vol = clamp_volume(vol)

    # Keep hardware/ALSA volume
    try:
        m = alsaaudio.Mixer('Digital')
        m.setvolume(vol)
    except alsaaudio.ALSAAudioError as e:
        print(f"Warning: Could not set ALSA volume ({e})", file=sys.stderr)

    # pygame volume uses 0.0 to 1.0, so convert from 0-90.
    pygame_vol = vol / MAX_VOLUME if MAX_VOLUME > 0 else 0.0

    if pas_sound is not None:
        pas_sound.set_volume(pygame_vol)
    for snd in [bell_1s_sound, bell_2s_sound, buzzer_1s_sound, buzzer_2s_sound]:
        if snd is not None:
            snd.set_volume(pygame_vol)

def change_volume(delta: int) -> int:
    current_volume = load_volume()
    new_vol = clamp_volume(current_volume + delta)
    new_vol = save_volume(new_vol)
    apply_volume(new_vol)
    return new_vol

def mixer_ready() -> bool:
    try:
        return pygame.mixer.get_init() is not None
    except Exception:
        return False

def stop_all_sounds():
    # Stop every active mixer channel
    if not mixer_ready():
        return
    pygame.mixer.stop()
    mute_audio()

def unmute_for_playback():
    # Unmute before playback begins
    unmute_audio()

def play_on_channel(channel, sound_obj):
    if not mixer_ready():
        return None
    
    # Restart the sound on its dedicated channel
    if sound_obj is None or channel is None:
        return None

    unmute_for_playback()

    if channel.get_busy():
        channel.queue(sound_obj)  # extend: play after current finishes
    else:
        channel.play(sound_obj)   # nothing playing, start immediately

    return channel

def play_bell_1s():
    return play_on_channel(BELL_CHANNEL, bell_1s_sound)

def play_bell_2s():
    return play_on_channel(BELL_CHANNEL, bell_2s_sound)

def play_buzzer_1s():
    return play_on_channel(BUZZER_CHANNEL, buzzer_1s_sound)

def play_buzzer_2s():
    return play_on_channel(BUZZER_CHANNEL, buzzer_2s_sound)

def play_pas():
    return play_on_channel(PAS_CHANNEL, pas_sound)

def mute_audio():
    AUDIO_MUTE.on()

def unmute_audio():
    AUDIO_MUTE.off()

def select_panel(n: int):
    MUX3.value = (n >> 3) & 1
    MUX2.value = (n >> 2) & 1
    MUX1.value = (n >> 1) & 1
    MUX0.value = (n >> 0) & 1

def flags_to_word(flags: State) -> int:
    word = 0
    for st, mask in STATE_TO_MASK.items():
        if flags & st:
            word |= mask
    return word & 0xFFFF

def write_panel(panel: int, flags: State):
    word16 = flags_to_word(flags)
    msb = (word16 >> 8) & 0xFF
    lsb = word16 & 0xFF

    select_panel(panel)
    spi.xfer2([msb, lsb])

    STROBE_CS_L.off()
    time.sleep(10e-6)
    STROBE_CS_L.on()

def set_panel(panel: int, flags: State):
    panel_state[panel] = flags
    write_panel(panel, flags)

def handle_button_press(loop, q, cmd):
    loop.call_soon_threadsafe(q.put_nowait, cmd)

async def rand_delay():
    await asyncio.sleep(random.uniform(4.0, 8.0))

def start_panel_task(panel, coro):
    task = asyncio.create_task(coro)
    panel_tasks[panel] = task
    task.add_done_callback(lambda t, p=panel: panel_tasks.pop(p, None))
    return task

async def schedule_task(panel, coro):
    # Cancel existing task for this panel if it exists
    if panel in panel_tasks:
        old_task = panel_tasks.pop(panel)
        await cancel_and_wait(old_task)
    
    start_panel_task(panel, coro)


async def cancel_and_wait(task: asyncio.Task | None):
    # Cancel one task and wait for it to fully finish cleanup
    if task is None:
        return

    if not task.done():
        task.cancel()

    with contextlib.suppress(asyncio.CancelledError):
        await task

async def cancel_all_tasks(tasks):
    # The task currently running this function
    current = asyncio.current_task()

    tasks_to_wait_for = []

    for task in tasks:
        # Skip missing tasks and skip the current task so we never
        # cancel/await ourselves during cleanup
        if task is None or task is current:
            continue

        # Cancel tasks that are still running
        if not task.done():
            task.cancel()

        # Still wait on done tasks too, so exceptions get collected
        tasks_to_wait_for.append(task)

    if not tasks_to_wait_for:
        return

    # Gather every task result without letting one failure crash home()
    results = await asyncio.gather(*tasks_to_wait_for, return_exceptions=True)

    # Optional: print unexpected exceptions for debugging
    for result in results:
        if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
            print(f"Task cleanup warning: {result!r}", file=sys.stderr)

async def evdev_listener(cmd_q: asyncio.Queue):
    dev = None
    path_pattern = "/dev/input/by-id/*-event-kbd"

    while True:
        try:
            # If we are not connected yet, keep searching
            if dev is None:
                matches = glob.glob(path_pattern)

                if not matches:
                    await asyncio.sleep(1.0)
                    continue

                # Use the first matching input device
                dev = InputDevice(matches[0])

                try:
                    dev.grab()
                except OSError:
                    pass

                print(f"Connected to remote: {dev.path}")

            async for event in dev.async_read_loop():
                if event.type == ecodes.EV_KEY:
                    if event.value == 1 and event.code in KEY_TO_CMD:
                        await cmd_q.put(KEY_TO_CMD[event.code])

        except asyncio.CancelledError:
            raise

        except Exception:
            # Device was likely unplugged or disappeared
            if dev is not None:
                with contextlib.suppress(Exception):
                    dev.ungrab()
                with contextlib.suppress(Exception):
                    dev.close()
                dev = None

            await asyncio.sleep(1.0)

async def dispatch_cmd(cmd: str):
    global launch_controller
    cmd = cmd.strip()

    # Remote Input all 5 panels
    # Outer Security (remote)
    if cmd == "1a":
        await home()
        panel = random.randint(0, len(PANELS) - 1)
        await schedule_task(panel, outer_security_sequence(panel))
    # Inner Security (remote)
    elif cmd == "2a":
        await home()
        panel = random.randint(0, len(PANELS) - 1)
        await schedule_task(panel, inner_security_sequence(panel))
    # Not Authenticated (remote)
    elif cmd == "3a":
        await home()
        panel = random.randint(0, len(PANELS) - 1)
        await schedule_task(panel, not_authenticated_sequence(panel))
    # Lamp Test (remote)
    elif cmd == "4a":
        await home()
        await schedule_task(0, lamp_test_sequence())
    # Step through Launch Sequence (remote)
    elif cmd == "7a":
        await home()
        play_pas()
        selected_panel = random.randint(0, len(PANELS) - 1)
        step_event.clear()

        start_launch_group(
            panels=[selected_panel],
            sequence_factory=launch_sequence_step_through,
            reset_delay=5.0
    	)
    # Auto Launch Sequence (remote)
    elif cmd == "8a":
        await home()
        play_pas()
        start_launch_group(
            panels=range(len(PANELS)),
            sequence_factory=launch_sequence_per_panel,
            reset_delay=5.0,
        )
    # Single Panel Auto Launch Sequence (remote)
    elif cmd == "9a":
        await home()
        play_pas()
        start_launch_group(
            panels=[random.randint(0, len(PANELS) - 1)],
            sequence_factory=launch_sequence_per_panel,
            reset_delay=5.0,
        )
    # Step in Launch Sequence (remote)
    elif cmd == "step":
        step_event.set()
    # Home
    elif cmd == "home":
        await home()
    # Increase volume
    elif cmd == "+":
        change_volume(+2)
    # Decrease volume
    elif cmd == "-":
        change_volume(-2)

    # Button Input (b) panels 1 & 2 only

    # Outer Security (button)
    elif cmd == "1b":
        await home()
        panel = random.randint(0, 1)
        await schedule_task(panel, outer_security_sequence(panel))
    # Inner Security (button)
    elif cmd == "2b":
        await home()
        panel = random.randint(0, 1)
        await schedule_task(panel, inner_security_sequence(panel))
    # Not Authenticated (button)
    elif cmd == "3b":
        await home()
        panel = random.randint(0, 1)
        await schedule_task(panel, not_authenticated_sequence(panel))
    # Lamp Test (button)
    elif cmd == "4b":
        await home()
        await schedule_task(0, lamp_test_sequence())
    # Auto Launch Sequence (button)
    elif cmd == "5b":
        await home()
        play_pas()
        start_launch_group(
            panels=[random.randint(0, 1)],
            sequence_factory=launch_sequence_per_panel,
            reset_delay=5.0,
        )
    
    show_panels("\n[1] Out [2] In [3] Launch [4] Not Auth [5] Lamp Test [0] Reset > ")

# ---------- MAIN ----------

async def main():
    initialize_display()
    initialize_audio()

    await home()
    
    cmd_q: asyncio.Queue[str] = asyncio.Queue()

    main_loop = asyncio.get_running_loop()
    BTN0.when_pressed = lambda: handle_button_press(main_loop, cmd_q, "1b")
    BTN1.when_pressed = lambda: handle_button_press(main_loop, cmd_q, "2b")
    BTN2.when_pressed = lambda: handle_button_press(main_loop, cmd_q, "3b")
    BTN3.when_pressed = lambda: handle_button_press(main_loop, cmd_q, "4b")
    BTN4.when_pressed = lambda: handle_button_press(main_loop, cmd_q, "5b")

    listener_task = asyncio.create_task(evdev_listener(cmd_q))

    # run loop + clean shutdown
    try:
        while True:
            cmd = await cmd_q.get()
            await dispatch_cmd(cmd)
    # catch SystemExit to exit loop
    except SystemExit:
        pass
    # cancel and wait for task cleanup
    finally:
        BTN0.when_pressed = None
        BTN1.when_pressed = None
        BTN2.when_pressed = None
        BTN3.when_pressed = None
        BTN4.when_pressed = None

        tasks = list(panel_tasks.values())
        panel_tasks.clear()
        await cancel_all_tasks(tasks)

        listener_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await listener_task

        stop_all_sounds()

        if mixer_ready():
            pygame.mixer.quit()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
