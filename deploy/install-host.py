"""Install verified project artifacts on an existing Linux Docker host.

Run as root after native builds, with --image sha256:... . No public port is
opened. Acceptance uses a separate fixed data root from the owner's instance.
"""
from __future__ import annotations

import argparse
import grp
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess


def run(*args: str) -> None:
    subprocess.run(args, check=True)


def directory(path: Path, uid: int, gid: int, mode: int) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise RuntimeError(f"Refusing symlink: {path}")
    os.chown(path, uid, gid)
    path.chmod(mode)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--acceptance", action="store_true")
    args = parser.parse_args()
    if os.getuid() != 0 or not re.fullmatch(r"sha256:[0-9a-f]{64}", args.image):
        raise RuntimeError("Requires root and an immutable image config digest")
    if subprocess.run(["systemctl", "is-active", "personal-bot-gateway"], capture_output=True).returncode == 0:
        raise RuntimeError("Stop the project's gateway before changing its fixed configuration")
    source = Path(__file__).resolve().parents[1]
    prefix = Path("/opt/personal-bot")
    state = Path("/var/lib/personal-bot")
    data = state / "acceptance" if args.acceptance else state
    try:
        account = pwd.getpwnam("personal-bot-gateway")
    except KeyError:
        run("useradd", "--system", "--user-group", "--no-create-home", "--shell", "/usr/sbin/nologin", "personal-bot-gateway")
        account = pwd.getpwnam("personal-bot-gateway")
    try:
        ipc = grp.getgrgid(31000)
        if ipc.gr_name != "personal-bot-ipc":
            raise RuntimeError("IPC GID 31000 is already occupied")
    except KeyError:
        run("groupadd", "--gid", "31000", "personal-bot-ipc")
    run("usermod", "--append", "--groups", "personal-bot-ipc", account.pw_name)
    uid, gid = account.pw_uid, account.pw_gid
    directory(state, 0, 0, 0o711)
    directory(data, 0, 0, 0o711)
    for area, owner, group, mode in [("control", uid, gid, 0o700), ("app", 31001, 31000, 0o700),
                                     ("files", 31001, 31000, 0o700), ("browser", 31003, 31000, 0o700),
                                     ("backups", 0, gid, 0o750)]:
        directory(data / area, owner, group, mode)
    # Both fixed paths must exist for systemd ReadWritePaths in acceptance mode.
    for control in [state / "control", state / "acceptance" / "control"]:
        if not control.exists():
            directory(control, uid, gid, 0o700)
    directory(Path("/run/personal-bot-host"), 0, gid, 0o750)
    directory(Path("/run/personal-bot-gateway"), uid, gid, 0o700)
    directory(Path("/run/personal-bot-bridge"), uid, 31000, 0o750)
    config = Path("/etc/personal-bot")
    directory(config, 0, gid, 0o750)
    directory(config / "tls", 0, gid, 0o750)
    seccomp = config / "seccomp-runtime.json"
    shutil.copyfile(source / "deploy" / "seccomp-runtime.json", seccomp)
    os.chown(seccomp, 0, 0)
    seccomp.chmod(0o644)
    directory(prefix / "bin", 0, 0, 0o755)
    for name in ["personal-bot-helper", "personal-bot-gateway", "personal-botctl", "personal-bot-tls"]:
        shutil.copyfile(prefix / "build" / name, prefix / "bin" / name)
        os.chown(prefix / "bin" / name, 0, 0)
        (prefix / "bin" / name).chmod(0o755)
    web = prefix / "web"
    web.mkdir(exist_ok=True)
    shutil.copytree(source / "web" / "dist", web, dirs_exist_ok=True)
    for root, dirs, files in os.walk(web):
        os.chown(root, 0, 0)
        os.chmod(root, 0o755)
        for name in files:
            os.chown(Path(root) / name, 0, 0)
            os.chmod(Path(root) / name, 0o644)
    host = json.loads((source / "host" / "host.example.json").read_text())
    host.update(origin="https://localhost:18443", state_dir=str(data / "control"), gateway_uid=uid,
                static_dir=str(web), backup_dir=str(data / "backups"), startup_seconds=90,
                host_reserve_bytes=768 * 1024**2, project_peak_bytes=2432 * 1024**2,
                project_slice="personal-bot.slice")
    helper = json.loads((source / "host" / "helper.example.json").read_text())
    helper.update(gateway_uid=uid, gateway_gid=gid, image=args.image, data_root=str(data),
                  seccomp_profile=str(seccomp), public_key_path=str(data / "control" / "assertion.pub"))
    for name, value, mode in [("host.json", host, 0o640), ("helper.json", helper, 0o600)]:
        target = config / name
        target.write_text(json.dumps(value, indent=2) + "\n")
        os.chown(target, 0, gid)
        target.chmod(mode)
    if not (config / "tls" / "localhost.key").exists():
        run("openssl", "req", "-x509", "-newkey", "rsa:3072", "-sha256", "-nodes", "-days", "90",
            "-subj", "/CN=Personal Bot SSH Test", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
            "-addext", "basicConstraints=critical,CA:FALSE", "-addext", "extendedKeyUsage=serverAuth",
            "-keyout", str(config / "tls" / "localhost.key"), "-out", str(config / "tls" / "localhost.crt"))
        for name, mode in [("localhost.key", 0o640), ("localhost.crt", 0o644)]:
            os.chown(config / "tls" / name, 0, gid)
            (config / "tls" / name).chmod(mode)
    for unit in (source / "deploy" / "systemd").iterdir():
        if unit.is_file():
            target = Path("/etc/systemd/system") / unit.name
            shutil.copyfile(unit, target)
            os.chown(target, 0, 0)
            target.chmod(0o644)
    networks = subprocess.check_output(["docker", "network", "ls", "--format", "{{.Name}}"], text=True).splitlines()
    if "personal-bot" not in networks:
        run("docker", "network", "create", "--label", "personal-bot.project=personal-bot", "personal-bot")
    run("systemctl", "daemon-reload")
    run("systemd-analyze", "verify", "/etc/systemd/system/personal-bot-gateway.service",
        "/etc/systemd/system/personal-bot-helper.service", "/etc/systemd/system/personal-bot-tls.service")
    print(json.dumps({"installed": True, "acceptance": args.acceptance, "gateway_uid": uid,
                      "entry": "https://localhost:18443", "data_root": str(data), "started": False}))


if __name__ == "__main__":
    main()
