from flask import Flask, render_template, request, redirect, url_for
import subprocess
import shlex
import os
import sys
import time
import threading
from collections import deque
import datetime
from contextlib import contextmanager

app = Flask(__name__)

import glob
import json
import re

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
SETTINGS_FILE = os.path.join(BASE_DIR, "settings.json")
PROFILE_GLOB = os.path.join(BASE_DIR, "profile_*.json")

DEFAULT_SETTINGS = {
    "port": 5000,
    "media_dir": "/media",
    "output_dir": "/media",
    "debug": False,
}

DEFAULT_PROFILE = {
    "name": "name",
    "audio_bitrate": "192k",
    "description": "",
    "encoder": "h264",
    "crf": "21",
    "audio": "copy",
    "preset": "medium",
    "movflags": "",
    "other_input_options": "",
    "other_output_options": "",
}

TASK_QUEUE = deque()
TASK_LOCK = threading.RLock()
NEXT_TASK_ID = 1
CURRENT_TASK = {
    "id": None,
    "command": "",
    "status": "idle",
    "last_line": "",
    "start_time": None,
    "output_file": None,
}

CURRENT_PROCESS = None
TASK_LOG = []

def debug_log(message):
    print(f"[DEBUG {datetime.datetime.now().isoformat(sep=' ', timespec='seconds')}] {message}", flush=True)

@contextmanager
def debug_lock(name):
    debug_log(f"waiting for lock: {name}")
    TASK_LOCK.acquire()
    debug_log(f"acquired lock: {name}")
    try:
        yield
    finally:
        TASK_LOCK.release()
        debug_log(f"released lock: {name}")


def log_event(message):
    timestamp = datetime.datetime.now().isoformat(sep=" ", timespec="seconds")
    entry = f"[{timestamp}] {message}"
    with debug_lock("log_event"):
        TASK_LOG.append(entry)


def get_last_line_from_file(file_path):
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
            if lines:
                return lines[-1].rstrip("\n")
    except Exception:
        pass
    return ""


def get_log_items():
    with debug_lock("get_log_items"):
        return list(TASK_LOG)


def stop_current_task():
    global CURRENT_PROCESS
    with debug_lock("stop_current_task"):
        if CURRENT_TASK["status"] != "running" or CURRENT_PROCESS is None:
            return False
        try:
            CURRENT_PROCESS.terminate()
            CURRENT_TASK["status"] = "stopping"
            CURRENT_TASK["last_line"] = "Stopping command..."
            log_event(f"Command {CURRENT_TASK['command']} (task {CURRENT_TASK['id']}) stopped by user")
            return True
        except Exception:
            return False


def queue_task(command, args):
    global NEXT_TASK_ID
    with debug_lock("queue_task"):
        task_id = NEXT_TASK_ID
        NEXT_TASK_ID += 1
        TASK_QUEUE.append({
            "id": task_id,
            "command": command,
            "args": args,
        })
        should_start = CURRENT_TASK["status"] != "running"

    if should_start:
        thread = threading.Thread(target=_run_queue_worker, daemon=True)
        thread.start()

    return task_id


def build_ffmpeg_task(selected_file, profile_file, nice_value, home_input_opts, home_output_opts, output_file_name, media_dir, output_dir):
    if not selected_file:
        raise ValueError("No input file selected.")
    if not profile_file or profile_file == "---":
        raise ValueError("No profile selected.")

    profile = load_profile_file(profile_file)
    if not profile:
        raise ValueError("Selected profile could not be loaded.")

    input_path = safe_media_file(selected_file, media_dir)
    if not input_path:
        raise ValueError("Selected input file is invalid.")

    output_name = output_file_name.strip() or f"{os.path.splitext(selected_file)[0]}_out.mp4"
    output_name = os.path.basename(output_name)
    if not output_name:
        raise ValueError("Output file name is invalid.")
    target_dir = output_dir.strip() or os.path.dirname(input_path)
    output_path = os.path.join(target_dir, output_name)

    if os.path.abspath(input_path) == os.path.abspath(output_path):
        raise ValueError("Input and output files must be different.")

    encoder = profile.get("encoder", "h264")
    codec_map = {
        "h264": "libx264",
        "h265": "libx265",
        "av1": "libaom-av1",
    }
    vcodec = codec_map.get(encoder, encoder)

    args = ["ffmpeg", "-y"]
    if home_input_opts:
        args.extend(shlex.split(home_input_opts))
    if profile.get("other_input_options"):
        args.extend(shlex.split(profile.get("other_input_options")))
    args.extend(["-i", input_path])
    args.extend(["-c:v", vcodec])

    crf = profile.get("crf")
    if crf:
        args.extend(["-crf", str(crf)])

    preset = profile.get("preset")
    if preset:
        args.extend(["-preset", preset])

    audio = profile.get("audio", "copy")
    if audio == "copy":
        args.extend(["-c:a", "copy"])
    elif audio == "aac":
        args.extend(["-c:a", "aac"])
    elif audio == "mp3":
        args.extend(["-c:a", "libmp3lame"])
    else:
        args.extend(["-c:a", audio])

    audio_bitrate = profile.get("audio_bitrate", "")
    if audio_bitrate and audio != "copy":
        args.extend(["-b:a", str(audio_bitrate)])

    movflags = str(profile.get("movflags", "")).strip()
    if movflags:
        args.extend(["-movflags", movflags])

    if profile.get("other_output_options"):
        args.extend(shlex.split(profile.get("other_output_options")))
    if home_output_opts:
        args.extend(shlex.split(home_output_opts))

    args.append(output_path)

    nice_value = nice_value.strip() if isinstance(nice_value, str) else nice_value
    if nice_value:
        try:
            nice_num = int(nice_value)
            if nice_num < -20 or nice_num > 19:
                raise ValueError
            args = ["nice", "-n", str(nice_num)] + args
        except ValueError:
            raise ValueError("Nice value must be a number between -20 and 19.")

    return f"{' '.join(shlex.quote(part) for part in args)}", args


def get_queue_items():
    with debug_lock("get_queue_items"):
        return list(TASK_QUEUE)


def remove_queue_item(task_id):
    with debug_lock("remove_queue_item"):
        for item in list(TASK_QUEUE):
            if item.get("id") == task_id:
                TASK_QUEUE.remove(item)
                log_event(f"Removed task {task_id} from queue")
                return True
    return False


def get_current_task():
    with debug_lock("get_current_task"):
        return dict(CURRENT_TASK)


def cleanup_stale_task_files():
    for path in glob.glob(os.path.join(BASE_DIR, ".task_*_output.txt")):
        try:
            os.remove(path)
            debug_log(f"Removed stale task output file: {path}")
        except Exception:
            pass


cleanup_stale_task_files()


def _run_queue_worker():
    while True:
        output_file = None
        with debug_lock("_run_queue_worker_outer"):
            if not TASK_QUEUE:
                CURRENT_TASK.update({
                    "id": None,
                    "command": "",
                    "status": "idle",
                    "last_line": "",
                    "start_time": None,
                    "output_file": None,
                })
                return
            task = TASK_QUEUE.popleft()
            output_file = os.path.join(BASE_DIR, f".task_{task['id']}_output.txt")
            CURRENT_TASK.update({
                "id": task["id"],
                "command": task["command"],
                "status": "running",
                "last_line": "",
                "start_time": datetime.datetime.now().isoformat(),
                "output_file": output_file,
            })

        if not task.get("args"):
            with debug_lock("_run_queue_worker_no_args"):
                CURRENT_TASK["last_line"] = "Task has no command arguments."
                CURRENT_TASK["status"] = "idle"
                CURRENT_TASK["output_file"] = None
                log_event(f"Task {task['id']} failed: missing arguments")
            continue

        try:
            with open(output_file, "w", encoding="utf-8") as out_f:
                process = subprocess.Popen(
                    task.get("args", []),
                    stdout=out_f,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                with debug_lock("_run_queue_worker_set_current_process"):
                    global CURRENT_PROCESS
                    CURRENT_PROCESS = process
                    log_event(f"Started command {task['command']} (task {task['id']})")

                return_code = process.wait()

            with debug_lock("_run_queue_worker_finalize"):
                CURRENT_PROCESS = None
                last_line = get_last_line_from_file(output_file)
                CURRENT_TASK["last_line"] = last_line
                if return_code == 0:
                    log_event(f"Command {task['command']} (task {task['id']}) finished with exit code 0")
                else:
                    log_event(f"Command {task['command']} (task {task['id']}) finished with exit code {return_code}")
        except Exception as ex:
            with debug_lock("_run_queue_worker_exception"):
                CURRENT_PROCESS = None
                CURRENT_TASK["last_line"] = f"Execution failure: {ex}"
                log_event(f"Command {task['command']} (task {task['id']}) execution failure: {ex}")
        finally:
            if output_file and os.path.exists(output_file):
                try:
                    os.remove(output_file)
                except Exception:
                    pass
            with debug_lock("_run_queue_worker_cleanup"):
                if not TASK_QUEUE:
                    CURRENT_TASK.update({
                        "status": "idle",
                        "id": None,
                        "command": "",
                        "start_time": None,
                        "output_file": None,
                    })
                    return


def load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def load_settings():
    if not os.path.exists(SETTINGS_FILE):
        save_json(SETTINGS_FILE, DEFAULT_SETTINGS)
        return DEFAULT_SETTINGS.copy()

    settings = load_json(SETTINGS_FILE)
    if isinstance(settings, dict):
        return {**DEFAULT_SETTINGS, **settings}

    save_json(SETTINGS_FILE, DEFAULT_SETTINGS)
    return DEFAULT_SETTINGS.copy()


def save_settings(settings):
    save_json(SETTINGS_FILE, settings)


def list_profiles():
    profiles = []
    for path in sorted(glob.glob(PROFILE_GLOB)):
        file_name = os.path.basename(path)
        data = load_json(path)
        display_name = None
        if isinstance(data, dict):
            display_name = data.get("name")
        if not display_name:
            display_name = os.path.splitext(file_name)[0]
        profiles.append({"file": file_name, "name": display_name})
    return profiles


def load_profile_file(profile_file):
    if not profile_file:
        return None
    if os.path.basename(profile_file) != profile_file:
        return None
    if not profile_file.startswith("profile_") or not profile_file.endswith(".json"):
        return None
    path = os.path.join(BASE_DIR, profile_file)
    if not os.path.isfile(path):
        return None
    data = load_json(path)
    return data if isinstance(data, dict) else None


def normalize_profile(profile):
    if not isinstance(profile, dict):
        return DEFAULT_PROFILE.copy()
    normalized = DEFAULT_PROFILE.copy()
    normalized.update(profile)
    return normalized


def schedule_restart(shutdown_func=None):
    time.sleep(0.5)
    if shutdown_func is not None:
        shutdown_func()
    else:
        os.execv(sys.executable, [sys.executable] + sys.argv)


def profile_filename_from_name(name):
    normalized = re.sub(r"[^a-zA-Z0-9_-]+", "_", name.strip())
    if not normalized:
        normalized = "profile"
    base_name = f"profile_{normalized}.json"
    path = os.path.join(BASE_DIR, base_name)
    if not os.path.exists(path):
        return base_name
    index = 1
    while True:
        candidate = f"profile_{normalized}_{index}.json"
        if not os.path.exists(os.path.join(BASE_DIR, candidate)):
            return candidate
        index += 1


def load_media_files(media_dir):
    files = []
    if os.path.isdir(media_dir):
        entries = sorted(os.scandir(media_dir), key=lambda entry: entry.name)
        for entry in entries:
            if entry.is_file():
                files.append({
                    "name": entry.name,
                    "path": os.path.join(media_dir, entry.name),
                    "size": entry.stat().st_size,
                })
    return files


def safe_media_file(name, media_dir):
    if not name:
        return None
    normalized = os.path.basename(name)
    candidate = os.path.join(media_dir, normalized)
    try:
        if os.path.commonpath([os.path.abspath(candidate), os.path.abspath(media_dir)]) != os.path.abspath(media_dir):
            return None
    except ValueError:
        return None
    return candidate if os.path.isfile(candidate) else None


def read_preview(path):
    try:
        return "TODO"
        # Scan the file with ffmpeg
    except Exception:
        return None


@app.route("/log")
def get_log():
    current_task = get_current_task()
    # Read last line from output file only for actually running tasks
    if current_task.get("status") == "running" and current_task.get("output_file") and os.path.exists(current_task["output_file"]):
        last_line = get_last_line_from_file(current_task["output_file"])
        if last_line:
            current_task["last_line"] = last_line
    return {
        "log": get_log_items(),
        "current_task": current_task,
        "queue": get_queue_items(),
    }


@app.route("/preview")
def preview():
    settings = load_settings()
    media_dir = settings.get("media_dir", DEFAULT_SETTINGS["media_dir"])
    selected_file = request.args.get("file", "").strip()
    selected_media_path = safe_media_file(selected_file, media_dir)
    if not selected_media_path:
        return {"error": "Invalid file"}, 400

    content = read_preview(selected_media_path)
    if content is None:
        return {"error": "Cannot read file"}, 500

    return {
        "name": selected_file,
        "content": content,
    }


@app.route("/profile-data")
def profile_data():
    profile_file = request.args.get("file", "").strip()
    profile = load_profile_file(profile_file)
    if not profile:
        return {"error": "Profile not found"}, 404
    return normalize_profile(profile)


@app.route("/settings", methods=["GET", "POST"])
def settings():
    error = None
    message = None
    settings = load_settings()

    if request.method == "POST":
        action = request.form.get("action", "save")
        port_value = request.form.get("port", "").strip()
        media_dir_value = request.form.get("media_dir", "").strip() or DEFAULT_SETTINGS["media_dir"]

        try:
            port = int(port_value)
            if port < 1 or port > 65535:
                raise ValueError("Port must be between 1 and 65535.")
            settings["port"] = port
        except ValueError:
            error = "Port must be a valid number between 1 and 65535."

        settings["media_dir"] = media_dir_value
        settings["output_dir"] = request.form.get("output_dir", "").strip()
        settings["debug"] = bool(request.form.get("debug"))
        if error is None:
            save_settings(settings)
            if action == "restart":
                message = "Server restart requested. The app will restart shortly."
                shutdown_func = request.environ.get("werkzeug.server.shutdown")
                threading.Thread(target=schedule_restart, args=(shutdown_func,), daemon=True).start()
            else:
                message = "Settings saved. Restart server to apply port changes."

    return render_template("settings.html", settings=settings, message=message, error=error)


@app.route("/profile", methods=["GET", "POST"])
def profile():
    profiles = list_profiles()
    error = None
    message = None
    selected_file = request.values.get("profile_file", "")
    current_profile = {
        "name": "",
        "audio_bitrate": "",
        "description": "",
        "encoder": "h264",
        "crf": "",
        "audio": "copy",
        "preset": "medium",
        "movflags": "",
        "other_input_options": "",
        "other_output_options": "",
    }

    if request.method == "POST":
        action = request.form.get("action", "save")
        selected_file = request.form.get("profile_file", "")
        name = request.form.get("name", "").strip()
        audio_bitrate = request.form.get("audio_bitrate", "").strip()
        description = request.form.get("description", "").strip()
        encoder = request.form.get("encoder", "h264")
        crf = request.form.get("crf", "").strip()
        audio = request.form.get("audio", "copy")
        preset = request.form.get("preset", "medium")
        movflags = request.form.get("movflags", "").strip()
        other_input_options = request.form.get("other_input_options", "").strip()
        other_output_options = request.form.get("other_output_options", "").strip()

        if action == "save":
            if not name:
                error = "Name is required to save a profile."
            else:
                if selected_file:
                    profile_file = selected_file
                else:
                    profile_file = profile_filename_from_name(name)
                profile_path = os.path.join(BASE_DIR, profile_file)
                save_json(profile_path, {
                    "name": name,
                    "audio_bitrate": audio_bitrate,
                    "description": description,
                    "encoder": encoder,
                    "crf": crf,
                    "audio": audio,
                    "preset": preset,
                    "movflags": movflags,
                    "other_input_options": other_input_options,
                    "other_output_options": other_output_options,
                })
                message = "Profile saved."
                return redirect(url_for("profile", profile_file=profile_file))
        elif action == "delete":
            if selected_file:
                profile_path = os.path.join(BASE_DIR, selected_file)
                if os.path.exists(profile_path):
                    os.remove(profile_path)
                    message = "Profile deleted."
                return redirect(url_for("profile"))
            else:
                error = "No profile selected to delete."

    if selected_file:
        loaded = load_profile_file(selected_file)
        if loaded:
            current_profile.update(normalize_profile(loaded))
        else:
            selected_file = ""
            if request.method == "GET":
                error = "Selected profile could not be loaded."

    return render_template(
        "profile.html",
        profiles=profiles,
        selected_file=selected_file,
        current_profile=current_profile,
        message=message,
        error=error,
    )


@app.route("/", methods=["GET", "POST"])
def index():
    output = None
    error = None
    preview_content = None
    current_inputs = {
        "nice": request.form.get("nice", "0") if request.method == "POST" else "0",
        "selected_file": request.form.get("selected_file", "") if request.method == "POST" else "",
        "selected_profile": request.form.get("selected_profile", "---") if request.method == "POST" else "---",
        "other_input_options": request.form.get("other_input_options", "") if request.method == "POST" else "",
        "other_output_options": request.form.get("other_output_options", "") if request.method == "POST" else "",
        "output_file_name": request.form.get("output_file_name", "") if request.method == "POST" else "",
    }
    settings = load_settings()
    media_dir = settings.get("media_dir", DEFAULT_SETTINGS["media_dir"])
    media_files = load_media_files(media_dir)
    profile_options = list_profiles()
    queue_items = get_queue_items()
    current_task = get_current_task()

    if request.method == "POST":
        action = request.form.get("action", "queue")
        remove_task_id = request.form.get("remove_queue_item")

        if action == "stop":
            stopped = stop_current_task()
            output = "Stop requested." if stopped else "No running command to stop."
        elif remove_task_id:
            try:
                task_id = int(remove_task_id)
                removed = remove_queue_item(task_id)
                output = "Removed queued task." if removed else "Queue item not found."
            except ValueError:
                error = "Invalid queue item selected."
        else:
            try:
                command, args = build_ffmpeg_task(
                    current_inputs["selected_file"],
                    current_inputs["selected_profile"],
                    request.form.get("nice", "0"),
                    request.form.get("other_input_options", ""),
                    request.form.get("other_output_options", ""),
                    request.form.get("output_file_name", ""),
                    media_dir,
                    settings.get("output_dir", ""),
                )
                if settings.get("debug"):
                    output = f"Debug mode: {command}"
                else:
                    task_id = queue_task(command, args)
                    output = f"Queued ffmpeg task {task_id}."
            except ValueError as exc:
                error = str(exc)
        queue_items = get_queue_items()
        current_task = get_current_task()

    return render_template(
        "index.html",
        output=output,
        error=error,
        inputs=current_inputs,
        media_files=media_files,
        preview_content=preview_content,
        profiles=profile_options,
        settings=settings,
        queue_items=queue_items,
        current_task=current_task,
        log_items=get_log_items(),
    )


if __name__ == "__main__":
    current_settings = load_settings()
    app.run(host="0.0.0.0", port=current_settings.get("port", DEFAULT_SETTINGS["port"]), debug=True)
