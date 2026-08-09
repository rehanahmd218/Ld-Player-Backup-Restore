import os
import subprocess
import time


def remove_instances():
    for i in range(6,44):
        subprocess.run(["ldconsole.exe", "remove", "--index", str(i)])
        time.sleep(0.2)


remove_instances()