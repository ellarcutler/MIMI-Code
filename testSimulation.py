import asyncio
import random
import sys
from enum import IntFlag, auto
from concurrent.futures import ThreadPoolExecutor

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

PANELS = ["A-LEFT", "A-RIGHT", "B-LEFT", "B-RIGHT", "C-LEFT"] 

panel_state = {i: State.STRATEGIC_ALERT for i in range(len(PANELS))}
panel_alarms = {i: "" for i in range(len(PANELS))}
background_tasks = set()

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
    print("\n === STRATEGIC SILO STATUS INDICATOR ===\n")
    
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
    show_panels("\n[1] Outer Sec  [2] Inner Sec  [3] LAUNCH  [0] Reset > ")

# ---------- SEQUENCES FROM LEFT IMAGE ----------

async def rand_delay():
    await asyncio.sleep(random.uniform(2.0, 3.0))

async def home():
    for task in background_tasks:
        task.cancel()
    if background_tasks: await asyncio.sleep(0.1)
    background_tasks.clear()

    # Default State : Strategic Alert ON, Sequence ON
    for i in range(len(PANELS)):
        panel_state[i] = State.STRATEGIC_ALERT
        panel_alarms[i] = ""
    
    show_panels("\n[1] Outer Sec  [2] Inner Sec  [3] LAUNCH  [0] Reset > ")

async def outer_security_sequence(panel: int):
    try:
        # Outer Security ON, Buzzer X
        base = State.STRATEGIC_ALERT
        update_panel(panel, base | State.OUTER_SECURITY, "BUZZER")

        # Turn off buzzer after 2 seconds
        await asyncio.sleep(2.0)
        update_panel(panel, base | State.OUTER_SECURITY, "") # Silence alarm

        # Hold state for 5 seconds
        await asyncio.sleep(3.0)
        update_panel(panel, base, "") # Reset to home state
    
    except asyncio.CancelledError:
        # handle task cancellation
        update_panel(panel, State.STRATEGIC_ALERT, "")
        raise

async def inner_security_sequence(panel: int):
    try:
        # Inner Security ON, Buzzer X
        base = State.STRATEGIC_ALERT
        update_panel(panel, base | State.INNER_SECURITY, "BUZZER")

        # Turn off buzzer after 2 seconds
        await asyncio.sleep(2.0)
        update_panel(panel, base | State.INNER_SECURITY, "") # Silence alarm

        # Hold state for 5 seconds
        await asyncio.sleep(3.0)
        update_panel(panel, base, "") # Reset to home state
    
    except asyncio.CancelledError:
        # handle task cancellation
        update_panel(panel, State.STRATEGIC_ALERT, "")
        raise

async def launch_sequence(panel: int):
    try:
        current_flags = State.STRATEGIC_ALERT
        
        # Go through launch sequence
        current_flags |= State.ENABLED
        for panel in range(len(PANELS)):
            update_panel(panel, current_flags, "BELL")
        await rand_delay()
        
        current_flags |= State.LAUNCH_CMD
        for panel in range(len(PANELS)):
            update_panel(panel, current_flags, "BELL")
        await rand_delay()
        
        current_flags |= State.LAUNCH_PROC
        for panel in range(len(PANELS)):
            update_panel(panel, current_flags, "BELL")
        await rand_delay()
        
        current_flags |= State.INNER_SECURITY
        for panel in range(len(PANELS)):
            update_panel(panel, current_flags, "BUZZER")
        await rand_delay()

        current_flags |= State.OUTER_SECURITY
        for panel in range(len(PANELS)):
            update_panel(panel, current_flags, "BUZZER")
        await rand_delay()
        
        current_flags |= State.MISSILE_AWAY
        for panel in range(len(PANELS)):
            update_panel(panel, current_flags, "LIFTOFF")
        
        # Switch to "after launch" state after 10 seconds
        await asyncio.sleep(10.0)
        current_flags = (State.NOT_AUTH | State.FAULT | State.WARHEAD_ALM | State.MISSILE_AWAY |
                         State.OUTER_SECURITY | State.INNER_SECURITY)
        for panel in range(len(PANELS)):
            update_panel(panel, current_flags, "BUZZER")

        # Turn off buzzer after 2 seconds
        await asyncio.sleep(2.0)
        for panel in range(len(PANELS)):
            update_panel(panel, current_flags, "") # Silence alarm

        # Hold state for 5 seconds
        await asyncio.sleep(3.0)
        current_flags = State.STRATEGIC_ALERT
        for panel in range(len(PANELS)):
            update_panel(panel, current_flags, "") # Reset to home state

    except asyncio.CancelledError:
        # handle task cancellation
        for panel in range(len(PANELS)):
            update_panel(panel, State.STRATEGIC_ALERT, "")
        raise

# ---------- HELPERS ----------

# Might not free resources!!!
async def schedule_task(coro):
    # cancel existing tasks
    for task in list(background_tasks):
        task.cancel()
    if background_tasks: await asyncio.sleep(0.1)
    background_tasks.clear()    

    # start new task
    task = asyncio.create_task(coro)
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)

async def ainput(prompt: str = ""):
    with ThreadPoolExecutor(1, "AsyncInput") as executor:
        return await asyncio.get_running_loop().run_in_executor(
            executor, input, prompt
        )

# ---------- MAIN ----------

async def main():
    await home()
    while True:
        cmd = await ainput()
        cmd = cmd.strip()

        if cmd == "1":
            # Outer Security 
            await schedule_task(outer_security_sequence(random.randint(0, 1)))
        elif cmd == "2":
            # Inner Security
            await schedule_task(inner_security_sequence(random.randint(0, 1)))
        elif cmd == "3":
            await schedule_task(launch_sequence(random.randint(0, len(PANELS)-1)))
        elif cmd == "0":
            await home()
        elif cmd == "q":
            break
        
        show_panels("\n[1] Outer Sec  [2] Inner Sec  [3] LAUNCH  [0] Reset > ")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass