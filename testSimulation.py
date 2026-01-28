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
    print("\n === MSIP STATUS INDICATOR ===\n")
    
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

async def rand_delay():
    await asyncio.sleep(random.uniform(2.0, 3.0))

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
        panel_alarms[i] = ""
    
    show_panels("\n[1] Out [2] In [3] Launch [4] Not Auth [5] Lamp Test [0] Reset > ")


async def not_authenticated_sequence(panel: int):
    try:
        # STEP 1: Strategic Alert turns OFF ("1 Off")
        update_panel(panel, State.OFF, "") 

        # STEP 2: Not Authenticated turns ON ("2 On")
        current_state = State.NOT_AUTH
        update_panel(panel, current_state, "BUZZER")
        await asyncio.sleep(1.0) 

        # STEP 3: Outer Security turns ON ("3 On")
        current_state |= State.OUTER_SECURITY
        update_panel(panel, current_state, "BUZZER")

        # HOLD: Keep the alarm going for 3 seconds
        await asyncio.sleep(3.0)
        
        # SILENCE: Turn off buzzer, but keep lights red
        update_panel(panel, current_state, "") 
        await asyncio.sleep(3.0)
        
        # RESET: Return to Green
        update_panel(panel, State.STRATEGIC_ALERT, "") 
    
    except asyncio.CancelledError:
        update_panel(panel, State.STRATEGIC_ALERT, "")
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
            update_panel(panel, all_on, "LAMP TEST")
            
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

async def launch_sequence():
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
            panel = random.randint(0, 1)
            await schedule_task(panel, outer_security_sequence(panel))
        elif cmd == "2":
            # Inner Security
            panel = random.randint(0, 1)
            await schedule_task(panel, inner_security_sequence(panel))
        elif cmd == "3":
            await schedule_task(0, launch_sequence(), is_launch=True)
        elif cmd == "4":
            # NOT AUTHENTICATED SEQUENCE
            panel = random.randint(0, 1)
            await schedule_task(panel, not_authenticated_sequence(panel))
        elif cmd == "5":
            # LAMP TEST
            await schedule_task(0, lamp_test_sequence(), is_launch=True)
        elif cmd == "0":
            await home()
        elif cmd == "q":
            break
        
        show_panels("\n[1] Out [2] In [3] Launch [4] Not Auth [5] Lamp Test [0] Reset > ")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass