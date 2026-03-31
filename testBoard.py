import asyncio
import random
import sys
from enum import IntFlag, auto
from concurrent.futures import ThreadPoolExecutor
import evdev
from evdev import InputDevice, categorize, ecodes
import contextlib
from gpiozero import OutputDevice, Button
import spidev
import time
import subprocess
from smbus2 import SMBus
import alsaaudio
import os

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
    ecodes.KEY_1: "1", # OUTER 
    ecodes.KEY_2: "2", # INNER
    ecodes.KEY_3: "3", # LAUNCH
    ecodes.KEY_4: "4", # NOT AUTH
    ecodes.KEY_5: "5", # LAMP TEST
    ecodes.KEY_6: "6", # PLAY AUDIO
    ecodes.KEY_HOMEPAGE: "0", # HOME
    ecodes.KEY_BACKSPACE: "q", # QUIT
    ecodes.KEY_VOLUMEUP: "+", # VOLUME UP
    ecodes.KEY_VOLUMEDOWN: "-", # VOLUME DOWN
}

# ---------- DIRECTORIES -----------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SOUNDS_DIR = os.path.join(BASE_DIR, "..", "sounds")
VOLUME_FILE = os.path.join(BASE_DIR, "volume.txt")

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

# ---------- I2C SETUP ----------
AMP_I2C_BUS = 1
AMP_I2C_ADDR = 0x4D

AMP4_INIT_WRITES = [
        (0x35, 0x58), # standard i2s
        (0x36, 0x53), # enable limiter + default i2s sck polarity

        (0x1D, 0x02), # power mode profile
    ]

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
launch_task = None

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

# ---------- SEQUENCES FROM LEFT IMAGE ----------

async def home():
    global launch_task
    
    # Cancel launch task if running
    if launch_task:
        launch_task.cancel()
        try:
            await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            pass
        launch_task = None

    # Cancel all panel specific tasks
    for task in list(panel_tasks.values()):
        task.cancel()
    await asyncio.sleep(0.1)
    panel_tasks.clear()

    # Reset all panels to default state
    for i in range(len(PANELS)):
        panel_state[i] = State.STRATEGIC_ALERT
        set_panel(i, State.STRATEGIC_ALERT)
        panel_alarms[i] = ""
    
    show_panels("\n[1] Out [2] In [3] Launch [4] Not Auth [5] Lamp Test [0] Reset > ")


async def not_authenticated_sequence(panel: int):
    try:
        # Not Auth (2) and Outer Sec (3) turn ON.
        active_state = State.NOT_AUTH | State.OUTER_SECURITY
        
        # Turn lights on + Buzzer
        update_panel(panel, active_state, "BUZZER")
        set_panel(panel, active_state)
        play_buzzer_2s()

        # Hold buzzer for 2 seconds
        await asyncio.sleep(2.0)
        
        # Silence buzzer, keep lights red
        update_panel(panel, active_state, "") 

        # Hold state for 3 seconds
        await asyncio.sleep(3.0)
        
        # Reset to home (Green Light)
        update_panel(panel, State.STRATEGIC_ALERT, "") 
        set_panel(panel, State.STRATEGIC_ALERT)
    
    except asyncio.CancelledError:
        update_panel(panel, State.STRATEGIC_ALERT, "")
        set_panel(panel, State.STRATEGIC_ALERT)
        stop_sound()
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
            set_panel(panel, all_on)

            
        # Hold for 3 seconds
        await asyncio.sleep(3.0)
        
        # Return to home
        await home()
        
    except asyncio.CancelledError:
        # If interrupted, just let it go (home() handles cleanup)
        pass


async def outer_security_sequence(panel: int):
    try:
        # Outer Security ON, Buzzer X
        base = State.STRATEGIC_ALERT
        update_panel(panel, base | State.OUTER_SECURITY, "BUZZER")
        set_panel(panel, base | State.OUTER_SECURITY)
        play_buzzer_1s()

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
        stop_sound()
        raise

async def inner_security_sequence(panel: int):
    try:
        # Inner Security ON, Buzzer X
        base = State.STRATEGIC_ALERT
        update_panel(panel, base | State.INNER_SECURITY, "BUZZER")
        set_panel(panel, base | State.INNER_SECURITY)
        play_buzzer_1s()

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
        stop_sound()
        raise


async def launch_sequence_per_panel(panel: int, start_delay: float):
    try:
        current_flags = State.STRATEGIC_ALERT

        await asyncio.sleep(start_delay)

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

        # Hold state for 5 seconds
        await asyncio.sleep(3.0)
        current_flags = State.STRATEGIC_ALERT
        update_panel(panel, current_flags, "") # Reset to home state
        set_panel(panel, current_flags)

    except asyncio.CancelledError:
        # handle task cancellation
        update_panel(panel, State.STRATEGIC_ALERT, "")
        set_panel(panel, current_flags)
        stop_sound()
        raise

# ---------- HELPERS ----------

async def rand_delay():
    await asyncio.sleep(random.uniform(1.0, 4.0))

async def schedule_task(panel, coro, is_launch=False):
    global launch_task

    if is_launch:
        # Cancel launch if already running
        if launch_task:
            launch_task.cancel()
            try:
                await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                pass
        # Cancel all panel tasks while launch starts
        for task in list(panel_tasks.values()):
            task.cancel()
        await asyncio.sleep(0.1)
        panel_tasks.clear()

        # Start launch and store globally
        for panel in range(len(PANELS)):
            launch_task = asyncio.create_task(coro)
            launch_task.add_done_callback(lambda t: globals().__setitem__('launch_task', None))

    else:
        # Cancel only the task on this panel
        if panel in panel_tasks:
            panel_tasks[panel].cancel()
            await asyncio.sleep(0.1)
            #panel_tasks.pop(panel)

        # If launch is running, cancel it
        if launch_task:
            launch_task.cancel()
            await asyncio.sleep(0.1)
            launch_task = None

        # Start the new panel task
        task = asyncio.create_task(coro)
        panel_tasks[panel] = task
        task.add_done_callback(lambda t: panel_tasks.pop(panel, None))

async def evdev_listener(dev_path: str, cmd_q: asyncio.Queue):
    dev = InputDevice(dev_path)

    try:
        dev.grab()
    except OSError:
        pass

    async for event in dev.async_read_loop():
        if event.type == ecodes.EV_KEY:
            if event.value == 1 and event.code in KEY_TO_CMD:
                await cmd_q.put(KEY_TO_CMD[event.code])

async def dispatch_cmd(cmd: str):
    cmd = cmd.strip()
    if cmd == "1":
        await home()
        # Outer Security 
        panel = random.randint(0, 1)
        await schedule_task(panel, outer_security_sequence(panel))
    elif cmd == "2":
        await home()
        # Inner Security
        panel = random.randint(0, 1)
        await schedule_task(panel, inner_security_sequence(panel))
    elif cmd == "3":
        await home()
        # Launch Sequence
        for panel in range(len(PANELS)):
            delay = random.uniform(1.0, 4.0)
            task = asyncio.create_task(launch_sequence_per_panel(panel, delay))
            panel_tasks[panel] = task
        #await schedule_task(0, launch_sequence(delay), is_launch=True)
    elif cmd == "4":
        await home()
        # Not Authenticated
        panel = random.randint(0, 1)
        await schedule_task(panel, not_authenticated_sequence(panel))
    elif cmd == "5":
        await home()
        # Lamp Test
        await schedule_task(0, lamp_test_sequence(), is_launch=True)
    elif cmd == "6":
        await home()
        play_pas()
    elif cmd == "0":
        await home()
    elif cmd == "q":
        raise SystemExit
    elif cmd == "+":
        change_volume(+2)
    elif cmd == "-":
        change_volume(-2)
    
    show_panels("\n[1] Out [2] In [3] Launch [4] Not Auth [5] Lamp Test [0] Reset > ")


def initialize_display():
    OE_ALL_U_L.on() # disable

    # add function to drive all LEDs to off

    time.sleep(10e-6)

    OE_ALL_U_L.off()

MAX_VOLUME = 90
MIN_VOLUME = 0
DEFAULT_VOLUME = 30

def initialize_audio():
    vol = load_volume()
    apply_volume(vol)
    return vol

def clamp_volume(vol: int) -> int:
    return max(MIN_VOLUME, min(MAX_VOLUME, vol))

def save_volume(volume: int):
    volume = clamp_volume(volume)
    with open(VOLUME_FILE, "w", encoding="utf-8") as f:
        f.write(f"{volume}\n")

def load_volume() -> int:
    try:
        with open(VOLUME_FILE, "r", encoding="utf-8") as f:
            return clamp_volume(int(f.read().strip()))
    except (FileNotFoundError, ValueError):
        return DEFAULT_VOLUME  # Default volume

def apply_volume(vol: int):
    vol = clamp_volume(vol)
    m = alsaaudio.Mixer('Digital')
    m.setvolume(vol)

def change_volume(delta: int) -> int:
    m = alsaaudio.Mixer('Digital')
    current_volume = m.getvolume()[0]
    new_vol = clamp_volume(current_volume + delta)
    m.setvolume(new_vol)
    save_volume(new_vol)
    
    return new_vol

audio_process = None

def stop_sound():
    global audio_process

    # Stop current sound if playing
    if audio_process and audio_process.poll() is None:
        mute_audio()
        #time.sleep(0.02)
        audio_process.terminate()
        try:
            audio_process.wait(timeout=0.001)
        except subprocess.TimeoutExpired:
            audio_process.kill()
            audio_process.wait()
    audio_process = None

def play_sound(filename: str):
    global audio_process

    stop_sound()
    sound_path = os.path.join(SOUNDS_DIR, filename)

    mute_audio()
    audio_process = subprocess.Popen(["aplay", sound_path])
    time.sleep(0.02) # let amp settle
    unmute_audio()

def play_bell_1s():
    play_sound("bell_1s2.wav")

def play_bell_2s():
    play_sound("bell_2s.wav")

def play_buzzer_1s():
    play_sound("buzzer_1s.wav")

def play_buzzer_2s():
    play_sound("buzzer_2s.wav")

def play_pas():
    play_sound("pas_3s.wav")

def mute_audio():
    AUDIO_MUTE.on()

def unmute_audio():
    AUDIO_MUTE.off()

    #with SMBus(AMP_I2C_BUS) as bus:
    #    for reg, val in AMP4_INIT_WRITES:
    #        bus.write_byte_data(AMP_I2C_ADDR, reg & 0xFF, val & 0xFF)
    #        time.sleep(delay_s)


def select_panel(n: int):
    MUX3.value = (n >> 3) & 1
    MUX2.value = (n >> 2) & 1
    MUX1.value = (n >> 1) & 1
    MUX0.value = (n >> 0) & 1

def strobe_latch():
    STROBE_CS_L.off()
 
    time.sleep(10e-6)
    STROBE_CS_L.on()

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
    #strobe_latch()

def set_panel(panel: int, flags: State):
    panel_state[panel] = flags
    write_panel(panel, flags)


def handle_button_press(loop, q, cmd):
    loop.call_soon_threadsafe(q.put_nowait, cmd)

# ---------- MAIN ----------

async def main():
    initialize_display()
    initialize_audio()

    await home()
    
    cmd_q: asyncio.Queue[str] = asyncio.Queue()

    dev_path = "/dev/input/event0"

    main_loop = asyncio.get_running_loop()
    BTN0.when_pressed = lambda: handle_button_press(main_loop, cmd_q, "1")
    BTN1.when_pressed = lambda: handle_button_press(main_loop, cmd_q, "2")
    BTN2.when_pressed = lambda: handle_button_press(main_loop, cmd_q, "3")
    BTN3.when_pressed = lambda: handle_button_press(main_loop, cmd_q, "4")
    BTN4.when_pressed = lambda: handle_button_press(main_loop, cmd_q, "5")

    #try:
     #   devices = [InputDevice(path) for path in evdev.list_devices()]
      #  dev_path = devices[0].path if devices else "/dev/input/event0"
    #except OSError:
     #   dev_path = "/dev/input/event0"

    listener_task = asyncio.create_task(evdev_listener(dev_path, cmd_q))

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
        listener_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await listener_task
        
        show_panels("\n[1] Out [2] In [3] Launch [4] Not Auth [5] Lamp Test [0] Reset > ")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
