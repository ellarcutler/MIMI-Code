import os
import sys
import asyncio
import aioconsole
import winsound
#from gpiozero import LED
#from time import sleep

#PIN = 17
#led = LED(PIN)

script_dir = os.path.dirname(os.path.abspath(__file__))  # folder where the script lives
BUZZER_FILE = os.path.join(script_dir, "buzzer_2s.wav")

async def outer_security():
    try:
        print("Outer Security LED and buzzer on")      
                  
        # play buzzer sound asynchronously
        await asyncio.to_thread(winsound.PlaySound, BUZZER_FILE, winsound.SND_FILENAME | winsound.SND_ASYNC)

        # keep on for 5 seconds
        await asyncio.sleep(5)                          

        # stop sound
        winsound.PlaySound(None, winsound.SND_PURGE)  
        print("Outer Security Alert done")

    except asyncio.CancelledError:
        # turn off if canceled early
        print("Outer Security Alert cancelled")

        # stop sound
        winsound.PlaySound(None, winsound.SND_PURGE)  
        raise

async def missile_launch():
    try:
        i = 0
        while True:
            print(f"Missile Launch running {i}")
            await asyncio.sleep(0.7)
            i += 1
    except asyncio.CancelledError:
        print("Missile Launch cancelled")
        raise

async def read_input(queue): 
    while True: 
        cmd = await aioconsole.ainput("Command (a, b, q): ") 
        await queue.put(cmd.strip())

# main function
async def strategic_alert_status():
    # queue for keyboard inputs
    queue = asyncio.Queue()
    input_task = asyncio.create_task(read_input(queue))
    action_task = None

    try:
        while True:
            # wait for next input
            cmd = await queue.get()

            if action_task and not action_task.done():
                print("Main: cancelling current action")
                action_task.cancel()
                try:
                    await action_task
                except asyncio.CancelledError:
                    pass

            if cmd == "a":
                action_task = asyncio.create_task(outer_security())
                print("Started Outer Security Alert")

            elif cmd == "b":
                action_task = asyncio.create_task(missile_launch())
                print("Started Missile Launch Sequence")

            elif cmd == "q":
                print("Quitting...")
                action_task.cancel()
                try:
                    await action_task
                except asyncio.CancelledError:
                    pass
                break

            else:
                print("Unknown command")

    finally:
        input_task.cancel()

    print("Main exited")

asyncio.run(strategic_alert_status())
