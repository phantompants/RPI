import os
import sys
import json
import paramiko

# Pi login comes from pi_config.json next to this script (git-ignored), or PI_HOST/PI_USER/PI_PASS env vars.
# Example pi_config.json: {"host": "192.168.8.50", "user": "pi", "password": "..."}
_cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pi_config.json")
_cfg = json.load(open(_cfg_path)) if os.path.exists(_cfg_path) else {}
PI_HOST = os.environ.get("PI_HOST", _cfg.get("host", "raspberrypi.local"))
PI_USER = os.environ.get("PI_USER", _cfg.get("user", "pi"))
PI_PASS = os.environ.get("PI_PASS", _cfg.get("password", ""))

def run_remote_command(cmd: str):
    print(f"==================================================")
    print(f"Exec on RPi ({PI_HOST}): {cmd}")
    print(f"==================================================")
    
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    try:
        ssh.connect(PI_HOST, username=PI_USER, password=PI_PASS, timeout=10)
        # Wrap sudo with echo password
        if cmd.startswith("sudo "):
            cmd = f"echo {PI_PASS} | sudo -S {cmd[5:]}"
            
        stdin, stdout, stderr = ssh.exec_command(cmd)
        
        out = stdout.read().decode('utf-8', errors='ignore')
        err = stderr.read().decode('utf-8', errors='ignore')
        
        if out:
            print("STDOUT:")
            print(out)
        if err:
            print("STDERR:")
            print(err)
            
        return out, err
    except Exception as e:
        print(f"SSH Error: {e}")
        return "", str(e)
    finally:
        ssh.close()

if __name__ == "__main__":
    if len(sys.argv) > 1:
        command_to_run = " ".join(sys.argv[1:])
        run_remote_command(command_to_run)
    else:
        # Default status check
        run_remote_command("uptime; hostname -I; systemctl status rpi-gps-backend --no-pager")
