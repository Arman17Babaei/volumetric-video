# install -> https://askubuntu.com/questions/1461513/help-with-installing-the-chrome-web-browser-22-04-2-lts
import time
import os
import re
import random
import subprocess
import argparse
import requests
import multiprocessing
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.desired_capabilities import DesiredCapabilities

duration = 120  # Video sequence length (in seconds)

# Parse command-line arguments
def parse_args():
    """
    Parse command-line arguments for the script.

    Returns:
        argparse.Namespace: Parsed arguments containing runs, scenario, and ABR options.
    """
    parser = argparse.ArgumentParser(description="Modify JavaScript variables for DASH scenarios.")
    parser.add_argument('--runs', type=int, required=True, help="Specify the number of runs for each scenario.")
    parser.add_argument('--srv_addr', type=str, required=False, default='localhost', help="IP address of server")
    return parser.parse_args()

# Function to run the player
def runPlayer(playerURL, duration):
    """
    Launch a headless Chrome browser to play a DASH video and log playback events.

    Args:
        playerURL (str): The URL of the player page.
        duration (int): Duration in seconds for monitoring playback.
    """
    print("start player ", playerURL.split('/')[-1])
    d = DesiredCapabilities.CHROME
    d['goog:loggingPrefs'] = {'browser': 'ALL'}
    options = Options()
    options.add_argument('--headless')
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--remote-debugging-port=0")

    options.add_argument('--no-sandbox')
    options.add_argument('--auto-open-devtools-for-tabs')
    options.add_argument('--autoplay-policy=no-user-gesture-required')
    options.add_argument('--incognito')  # Use incognito mode
    options.set_capability('cloud:options', d)

    elapsed_time = 0
    poll_interval = 10  # Time (in seconds) between each check

    service = Service("/usr/bin/google-chrome-stable", log_output=f"/tmp/chromedriver.log")
    driver = webdriver.Chrome(options=options)
    
    driver.execute_cdp_cmd("Network.enable", {})
    
    driver.execute_cdp_cmd("Network.setCacheDisabled", {"cacheDisabled":True})
    
    driver.get(playerURL)
    
    finame = f'player_log.txt'
    stop_players = False
    with open(finame, 'w') as f:
        # Wait until the player finishes or the max duration is reached or stop_players is triggered
        while elapsed_time < duration:
            # Retrieve new browser logs
            log_entries = driver.get_log('browser')
            # Write new log entries sequentially
            for entry in log_entries:
                f.write(str(entry) + '\n')
                f.flush()  # Ensure the data is written to the file immediately
                if "File successfully uploaded" in str(entry) or "Error uploading the file" in str(entry):
                    print(f"Results file uploaded succesfully... Terminating player...")
                    stop_players = True
                    break
                elif "No video bytes to push or stream is inactive" in str(entry):
                    print(f"Player stopped... Terminating player for {finame}")
                    stop_players = True
                    break

            if stop_players:
                break
            
            time.sleep(poll_interval)  # Wait before checking again
            elapsed_time += poll_interval
            
            print(f"stop_players is: {stop_players}")
            
    driver.quit()

if __name__ == "__main__":
    runs = 1
    srv_addr = 'localhost'

    # Parse the command-line argument
    args = parse_args()
    if args:
        if args.runs:
            runs = args.runs  # Get the number of runs from command-line input
        if args.srv_addr:
            srv_addr = args.srv_addr
    
    for i in range(runs):
        print(f"Run #{i + 1}/{runs}")
        duration_t = 4 * duration  # Example duration for each client
        
        # Start a process for the client with the assigned file
        # process = multiprocessing.Process(target=runPlayer, args=(f"file:///home/zenzi/Documents/LiveVMAF/client/index.html", duration_t))
        process = multiprocessing.Process(target=runPlayer, args=(f"http://{srv_addr}:8123/index.html", duration_t))
        process.start()
        process.join()
