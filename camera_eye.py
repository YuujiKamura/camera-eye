"""camera-eye: ONVIF + RTSP camera as a CLI tool.

usage:
  python camera_eye.py capture           # grab one frame -> latest_path
  python camera_eye.py pan left 0.6      # pan/tilt for 0.6 seconds
  python camera_eye.py status            # show config + connectivity

env:
  CAMERA_EYE_PASS  required, the ONVIF account password.
  CAMERA_EYE_CONFIG  optional, path to config.toml (default ~/.camera-eye/config.toml).
"""

import argparse
import base64
import datetime
import hashlib
import os
import pathlib
import secrets
import signal
import string
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request


def load_config():
    path = os.environ.get(
        "CAMERA_EYE_CONFIG",
        str(pathlib.Path.home() / ".camera-eye" / "config.toml"),
    )
    if not os.path.exists(path):
        sys.exit(f"config not found: {path}")
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    pw = os.environ.get("CAMERA_EYE_PASS", "")
    if not pw:
        sys.exit("CAMERA_EYE_PASS env var is required")
    cfg["auth"]["password"] = pw
    latest = cfg["capture"]["latest_path"]
    latest = string.Template(latest).safe_substitute(HOME=str(pathlib.Path.home()))
    cfg["capture"]["latest_path"] = latest
    return cfg


def onvif_endpoint(cfg):
    c = cfg["camera"]
    return f"http://{c['host']}:{c['onvif_port']}/onvif/service"


def rtsp_url(cfg):
    c = cfg["camera"]
    a = cfg["auth"]
    return (
        f"rtsp://{a['user']}:{a['password']}@{c['host']}:{c['rtsp_port']}/{c['rtsp_path']}"
    )


def wsse_header(user, password):
    nonce_raw = secrets.token_bytes(16)
    nonce_b64 = base64.b64encode(nonce_raw).decode("ascii")
    created = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    digest_raw = hashlib.sha1(
        nonce_raw + created.encode("ascii") + password.encode("utf-8")
    ).digest()
    digest_b64 = base64.b64encode(digest_raw).decode("ascii")
    return (
        '<wsse:Security xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd" '
        'xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">'
        "<wsse:UsernameToken>"
        f"<wsse:Username>{user}</wsse:Username>"
        '<wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">'
        f"{digest_b64}</wsse:Password>"
        '<wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">'
        f"{nonce_b64}</wsse:Nonce>"
        f"<wsu:Created>{created}</wsu:Created>"
        "</wsse:UsernameToken>"
        "</wsse:Security>"
    )


def soap(cfg, action, body_inner):
    envelope = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
        f"<s:Header>{wsse_header(cfg['auth']['user'], cfg['auth']['password'])}</s:Header>"
        f"<s:Body>{body_inner}</s:Body>"
        "</s:Envelope>"
    )
    req = urllib.request.Request(
        onvif_endpoint(cfg),
        data=envelope.encode("utf-8"),
        headers={
            "Content-Type": f'application/soap+xml; charset=utf-8; action="{action}"'
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=6) as r:
            return r.status, r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")


def get_profile_token(cfg):
    body = '<trt:GetProfiles xmlns:trt="http://www.onvif.org/ver10/media/wsdl"/>'
    status, out = soap(
        cfg, "http://www.onvif.org/ver10/media/wsdl/GetProfiles", body
    )
    if status != 200:
        sys.exit(f"GetProfiles failed: HTTP {status}\n{out[:400]}")
    import re

    m = re.search(r'<trt:Profiles[^>]*token="([^"]+)"', out) or re.search(
        r'token="([^"]+)"', out
    )
    if not m:
        sys.exit("no profile token in GetProfiles response")
    return m.group(1)


def watch_pid_path():
    return pathlib.Path.home() / ".camera-eye" / "watch.pid"


def watch_is_running():
    p = watch_pid_path()
    if not p.exists():
        return None
    try:
        pid = int(p.read_text().strip())
    except Exception:
        return None
    if os.name == "nt":
        r = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
            capture_output=True, text=True,
        )
        if "ffmpeg" in r.stdout.lower() and str(pid) in r.stdout:
            return pid
        return None
    try:
        os.kill(pid, 0)
        return pid
    except OSError:
        return None


def cmd_capture(cfg):
    out_path = cfg["capture"]["latest_path"]
    pathlib.Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    if watch_is_running() and pathlib.Path(out_path).exists():
        age = time.time() - pathlib.Path(out_path).stat().st_mtime
        if age < 5:
            print(out_path)
            return
    r = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-rtsp_transport",
            "tcp",
            "-i",
            rtsp_url(cfg),
            "-frames:v",
            "1",
            "-q:v",
            "3",
            out_path,
        ],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        sys.exit(f"ffmpeg failed (rc={r.returncode})\n{r.stderr[-800:]}")
    print(out_path)


def cmd_watch_start(cfg):
    if watch_is_running():
        sys.exit("already running")
    out_path = cfg["capture"]["latest_path"]
    pathlib.Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    log_path = pathlib.Path.home() / ".camera-eye" / "watch.log"
    cmd = [
        "ffmpeg",
        "-y",
        "-rtsp_transport",
        "tcp",
        "-i",
        rtsp_url(cfg),
        "-vf",
        "fps=1",
        "-update",
        "1",
        "-q:v",
        "3",
        out_path,
    ]
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    with open(log_path, "ab") as logf:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=logf,
            stderr=subprocess.STDOUT,
            creationflags=flags if os.name == "nt" else 0,
            close_fds=True,
        )
    watch_pid_path().write_text(str(proc.pid))
    print(f"started pid={proc.pid}, writing {out_path} at 1 fps")


def cmd_watch_stop(cfg):
    pid = watch_is_running()
    if not pid:
        sys.exit("not running")
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], check=False)
        else:
            os.kill(pid, signal.SIGTERM)
    finally:
        try:
            watch_pid_path().unlink()
        except FileNotFoundError:
            pass
    print(f"stopped pid={pid}")


def cmd_watch_status(cfg):
    pid = watch_is_running()
    if not pid:
        print("not running")
        return
    out_path = pathlib.Path(cfg["capture"]["latest_path"])
    age = "?"
    if out_path.exists():
        age = f"{time.time() - out_path.stat().st_mtime:.1f}s ago"
    print(f"running pid={pid}, last frame: {age}")


VEL = {
    "left": ("0.5", "0.0"),
    "right": ("-0.5", "0.0"),
    "up": ("0.0", "-0.5"),
    "down": ("0.0", "0.5"),
}


def cmd_pan(cfg, direction, seconds):
    if direction not in VEL:
        sys.exit(f"direction must be one of {list(VEL)}")
    x, y = VEL[direction]
    token = get_profile_token(cfg)
    move = (
        '<tptz:ContinuousMove xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl" '
        'xmlns:tt="http://www.onvif.org/ver10/schema">'
        f"<tptz:ProfileToken>{token}</tptz:ProfileToken>"
        '<tptz:Velocity>'
        f'<tt:PanTilt x="{x}" y="{y}"/>'
        '</tptz:Velocity>'
        "</tptz:ContinuousMove>"
    )
    s1, _ = soap(cfg, "http://www.onvif.org/ver20/ptz/wsdl/ContinuousMove", move)
    if s1 != 200:
        sys.exit(f"ContinuousMove failed: HTTP {s1}")
    import time

    time.sleep(seconds)
    stop = (
        '<tptz:Stop xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl">'
        f"<tptz:ProfileToken>{token}</tptz:ProfileToken>"
        "<tptz:PanTilt>true</tptz:PanTilt>"
        "<tptz:Zoom>true</tptz:Zoom>"
        "</tptz:Stop>"
    )
    s2, _ = soap(cfg, "http://www.onvif.org/ver20/ptz/wsdl/Stop", stop)
    if s2 != 200:
        sys.exit(f"Stop failed: HTTP {s2}")
    print(f"panned {direction} {seconds}s ok")


def cmd_status(cfg):
    print("camera host:", cfg["camera"]["host"])
    print("onvif:", onvif_endpoint(cfg))
    body = (
        '<tds:GetSystemDateAndTime '
        'xmlns:tds="http://www.onvif.org/ver10/device/wsdl"/>'
    )
    s, out = soap(
        cfg,
        "http://www.onvif.org/ver10/device/wsdl/GetSystemDateAndTime",
        body,
    )
    print("onvif probe:", "OK" if s == 200 else f"HTTP {s}")
    if s == 200:
        import re
        m = re.search(r"<tt:UTCDateTime>(.+?)</tt:UTCDateTime>", out)
        if m:
            print("camera utc:", m.group(1))
    print("latest_path:", cfg["capture"]["latest_path"])


def prompt(label, default=None):
    suffix = f" [{default}]" if default is not None else ""
    val = input(f"{label}{suffix}: ").strip()
    return val or (default if default is not None else "")


def cmd_setup():
    target = pathlib.Path(
        os.environ.get(
            "CAMERA_EYE_CONFIG",
            str(pathlib.Path.home() / ".camera-eye" / "config.toml"),
        )
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        ans = input(f"{target} already exists, overwrite? [y/N]: ").strip().lower()
        if ans != "y":
            print("aborted")
            return
    print("camera-eye setup. press Enter to accept the default.")
    host = prompt("camera IP")
    if not host:
        sys.exit("host is required")
    onvif_port = int(prompt("ONVIF port", "2020"))
    rtsp_port = int(prompt("RTSP port", "554"))
    rtsp_path = prompt("RTSP stream path", "stream1")
    user = prompt("ONVIF username")
    if not user:
        sys.exit("user is required")
    body = (
        "[camera]\n"
        f'host = "{host}"\n'
        f"onvif_port = {onvif_port}\n"
        f"rtsp_port = {rtsp_port}\n"
        f'rtsp_path = "{rtsp_path}"\n'
        "\n"
        "[auth]\n"
        f'user = "{user}"\n'
        "# password is read from env var CAMERA_EYE_PASS only.\n"
        "# Never set it in this file.\n"
        "\n"
        "[capture]\n"
        'latest_path = "${HOME}/.camera-eye/latest.jpg"\n'
    )
    target.write_text(body, encoding="utf-8")
    print(f"wrote {target}")
    print("next: set CAMERA_EYE_PASS env var, then run `camera-eye status`.")


def main():
    p = argparse.ArgumentParser(prog="camera-eye")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("capture")
    sub.add_parser("status")
    sub.add_parser("setup")
    pan = sub.add_parser("pan")
    pan.add_argument("direction", choices=list(VEL))
    pan.add_argument("seconds", type=float, nargs="?", default=0.5)
    watch = sub.add_parser("watch")
    watch.add_argument("action", choices=["start", "stop", "status"])
    args = p.parse_args()
    if args.cmd == "setup":
        cmd_setup()
        return
    cfg = load_config()
    if args.cmd == "capture":
        cmd_capture(cfg)
    elif args.cmd == "pan":
        cmd_pan(cfg, args.direction, args.seconds)
    elif args.cmd == "status":
        cmd_status(cfg)
    elif args.cmd == "watch":
        if args.action == "start":
            cmd_watch_start(cfg)
        elif args.action == "stop":
            cmd_watch_stop(cfg)
        else:
            cmd_watch_status(cfg)


if __name__ == "__main__":
    main()
