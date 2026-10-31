"""Opt-in, real Linux browser/broker acceptance probe; no model or mock calls.

Run in an isolated acceptance container as root with
PERSONAL_BOT_ACCEPTANCE_PROBE=1 python tests/linux_browser_probe.py

The root coordinator starts API-UID and Worker-UID subprocesses. Browser calls
and WebSocket handshakes use the actual API UID and actual Unix broker. Root
only inspects nonsecret process/isolation metadata. It never reads cookie or
Xauthority contents. It closes the acceptance browser on exit.

Public navigation uses normal public HTTPS URLs. A network failure is reported
separately; no data/file/private URL is substituted. RFB keyboard navigation is
tested using fragments of an available public page. The optional httpbin cookie
roundtrip tests persistent browser restart, not container deletion/recreation.

Root must additionally test gateway HTTPS/cookies/CSRF, real noVNC UI, mobile
layout, actual container recreation, idle deletion and original host workloads.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
import errno
import json
import os
from pathlib import Path
import re
import socket
import stat
import struct
import subprocess
import sys
import time
from urllib.parse import quote, urlsplit, urlunsplit
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PUBLIC_URLS = ["https://example.com/", "https://www.baidu.com/"]
REPORTS: list[dict] = []


class ProbeFailure(RuntimeError):
    pass


def report(check: str, status: str = "passed", **evidence) -> None:
    REPORTS.append({"check": check, "status": status, **evidence})


def require(condition: bool, check: str) -> None:
    if not condition:
        raise ProbeFailure(check)


class RFB:
    """Minimal actual RFB 3.8 client, requesting only bounded raw rectangles."""
    def __init__(self, websocket):
        self.ws = websocket
        self.buffer = bytearray()
        self.width = self.height = 0
        self.bytes_per_pixel = 0

    async def read(self, length: int) -> bytes:
        require(0 <= length <= 8 * 1024**2, "rfb_read_bound")
        while len(self.buffer) < length:
            message = await asyncio.wait_for(self.ws.recv(), 15)
            require(isinstance(message, bytes), "rfb_binary_transport")
            self.buffer.extend(message)
            require(len(self.buffer) <= 12 * 1024**2, "rfb_buffer_bound")
        value = bytes(self.buffer[:length])
        del self.buffer[:length]
        return value

    async def handshake(self) -> None:
        require(await self.read(12) == b"RFB 003.008\n", "rfb_server_version")
        await self.ws.send(b"RFB 003.008\n")
        count = (await self.read(1))[0]
        require(count > 0, "rfb_security_types")
        require(1 in await self.read(count), "rfb_private_transport_security")
        await self.ws.send(b"\x01")
        require(await self.read(4) == b"\x00" * 4, "rfb_security_result")
        await self.ws.send(b"\x01")  # Shared; exclusive clients are forbidden.
        header = await self.read(24)
        self.width, self.height = struct.unpack("!HH", header[:4])
        self.bytes_per_pixel = header[4] // 8
        require(0 < self.width <= 4096 and 0 < self.height <= 4096, "rfb_dimensions")
        require(self.bytes_per_pixel in {1, 2, 4}, "rfb_pixel_format")
        name_size = struct.unpack("!I", header[20:24])[0]
        require(name_size <= 4096, "rfb_name_bound")
        await self.read(name_size)  # Never print a server desktop name.
        pixel_format = struct.pack("!BBBBHHHBBB3x", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)
        await self.ws.send(b"\x00\x00\x00\x00" + pixel_format)
        self.bytes_per_pixel = 4
        await self.ws.send(b"\x02\x00\x00\x01" + struct.pack("!i", 0))

    async def frame(self) -> dict:
        await self.ws.send(b"\x03\x00" + struct.pack("!HHHH", 0, 0, self.width, self.height))
        for _ in range(20):
            kind = (await self.read(1))[0]
            if kind == 2:  # Bell.
                continue
            if kind == 3:  # ServerCutText is discarded, never exposed.
                tail = await self.read(7)
                size = struct.unpack("!I", tail[3:7])[0]
                require(size <= 65536, "rfb_clipboard_bound")
                await self.read(size)
                continue
            require(kind == 0, "rfb_server_message")
            tail = await self.read(3)
            rectangles = struct.unpack("!H", tail[1:3])[0]
            require(rectangles <= 4096, "rfb_rectangle_bound")
            byte_count = 0
            varied = False
            for _ in range(rectangles):
                x, y, width, height, encoding = struct.unpack("!HHHHi", await self.read(12))
                require(encoding == 0, "rfb_raw_encoding")
                require(x + width <= self.width and y + height <= self.height, "rfb_rectangle_geometry")
                payload = await self.read(width * height * self.bytes_per_pixel)
                byte_count += len(payload)
                stride = max(self.bytes_per_pixel, (len(payload) // 1024 // self.bytes_per_pixel) * self.bytes_per_pixel)
                varied |= len({payload[index:index + self.bytes_per_pixel] for index in range(0, len(payload), stride)}) > 1
            if byte_count:
                return {"width": self.width, "height": self.height, "bytes": byte_count, "varied_pixels": varied}
        raise ProbeFailure("rfb_no_framebuffer")

    @staticmethod
    def key(symbol: int, down: bool) -> bytes:
        return b"\x04" + bytes([int(down)]) + b"\x00\x00" + struct.pack("!I", symbol)

    async def navigate(self, url: str, *, pointer: bool = False) -> None:
        # All test targets are public HTTPS and use only synthetic fragments.
        require(urlsplit(url).scheme == "https", "rfb_navigation_https")
        if pointer:
            await self.ws.send(b"\x05\x01" + struct.pack("!HH", 350, 60))
            await self.ws.send(b"\x05\x00" + struct.pack("!HH", 350, 60))
            chord = 0x61  # Ctrl+A in the omnibox after a real pointer click.
        else:
            # Focus the actual browser window before sending the shortcut.
            await self.ws.send(b"\x05\x01" + struct.pack("!HH", 200, 100))
            await self.ws.send(b"\x05\x00" + struct.pack("!HH", 200, 100))
            chord = 0x6C  # Ctrl+L.
        # XTest delivery and Chrome's omnibox activation are asynchronous. Give
        # the actual UI time to process each stage instead of queuing the entire
        # navigation in one burst; success still requires the real URL change.
        await asyncio.sleep(0.15)
        await self.ws.send(self.key(0xFFE3, True) + self.key(chord, True) + self.key(chord, False) + self.key(0xFFE3, False))
        await asyncio.sleep(0.15)
        for value in url:
            await self.ws.send(self.key(ord(value), True) + self.key(ord(value), False))
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.15)
        await self.ws.send(self.key(0xFF0D, True) + self.key(0xFF0D, False))


@asynccontextmanager
async def viewer(device: str, owner: str, session: str):
    from personal_bot import browser
    from websockets.asyncio.client import unix_connect
    ticket = await browser.create_view_session(device, owner, session_id=session)
    async with unix_connect(browser.SOCKET, uri="ws://browser/view/" + ticket["view_session_id"],
                            additional_headers={"X-Owner-ID": owner, "X-Session-ID": session},
                            max_size=4 * 1024**2, open_timeout=10, close_timeout=5) as websocket:
        rfb = RFB(websocket)
        await rfb.handshake()
        yield rfb


async def wait_url(expected: str, seconds: float = 8) -> bool:
    from personal_bot import browser
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if (await browser.status())["url"] == expected:
            return True
        await asyncio.sleep(0.1)
    return False


def fragment_url(base: str, label: str) -> str:
    parts = urlsplit(base)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, "personal-bot-probe-" + label + "-" + uuid.uuid4().hex))


async def api_phase(phase: str) -> dict:
    import httpx
    from personal_bot import browser
    require(os.getuid() == 31001, "actual_api_uid")
    task, attempt = str(uuid.uuid4()), str(uuid.uuid4())
    owner, session1, session2 = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    device1, device2 = str(uuid.uuid4()), str(uuid.uuid4())

    async def navigate(url: str):
        return await browser.perform({"type": "navigate", "url": url}, task, attempt)

    if phase == "start":
        await browser.quiesce()
        await browser.perform({"type": "observe"}, task, attempt)
        agent_only = await browser.status()
        require(agent_only["running"] and not agent_only["display_running"] and agent_only["viewer_count"] == 0,
                "agent_only_browser_has_no_display_bridge")
        report("agent_only_browser_starts_without_x11vnc", browser_start_seconds=agent_only.get("browser_start_seconds"))
        async with viewer(device1, owner, session1) as rfb:
            frame = await rfb.frame()
            require(frame["varied_pixels"], "actual_visible_framebuffer")
            report("actual_rfb_handshake_and_framebuffer", **frame)
            require((await browser.status())["running"] is True, "chromium_started")
            report("chromium_started_through_actual_broker")
            for target in ["http://127.0.0.1/", "http://[::1]/", "http://10.0.0.1/",
                           "http://169.254.169.254/latest/meta-data/", "http://100.100.100.200/"]:
                old = (await browser.status())["url"]
                try:
                    await navigate(target)
                except httpx.HTTPStatusError as exc:
                    detail = exc.response.json().get("detail", {})
                    require(exc.response.status_code == 409 and isinstance(detail, dict) and
                            "Non-public" in detail.get("message", ""), "private_destination_explicit_policy_rejection")
                else:
                    raise ProbeFailure("private_destination_was_not_rejected")
                require((await browser.status())["url"] == old, "private_destination_no_navigation")
            report("private_loopback_rfc1918_metadata_cgnat_targets_rejected", cases=5)
            for index, target in enumerate(PUBLIC_URLS):
                try:
                    observation = await navigate(target)
                    require(observation.get("origin", "").startswith("https://"), "public_https_origin")
                    report("public_https_navigation", public_source=index, available=True)
                    return {"public_source": index}
                except (httpx.HTTPError, ProbeFailure):
                    pass
            report("public_https_navigation", "not_tested", reason="public_sources_unavailable")
            return {"public_source": None}

    if phase == "display":
        async with viewer(device1, owner, session1) as rfb:
            await rfb.frame()
            before = await browser.status()
            require(before["display_running"] and before["viewer_count"] == 1, "connected_viewer_owns_display")
        started = time.monotonic()
        while time.monotonic() - started < 40:
            state = await browser.status()
            if not state["display_running"]:
                break
            await asyncio.sleep(.2)
        elapsed = time.monotonic() - started
        require(not state["display_running"] and state["viewer_count"] == 0, "display_stops_after_last_viewer_grace")
        require(29 <= elapsed <= 40, "actual_thirty_second_display_grace")
        require(state["running"] and state["url"] == before["url"] and state["browser_epoch"] == before["browser_epoch"],
                "display_retirement_preserves_browser_page_and_epoch")
        report("unwatched_display_stops_after_thirty_seconds_preserving_browser", elapsed_seconds=elapsed)
        async with viewer(device1, owner, session1) as rfb:
            frame = await rfb.frame()
            require(frame["varied_pixels"], "display_restart_has_real_framebuffer")
            require((await browser.status())["display_running"], "valid_viewer_restarts_display")
        report("valid_viewer_restarts_display_after_grace")
        return {}

    if phase == "rfb":
        initial = await browser.status()
        current = initial.get("url") or ""
        if not current.startswith("https://") or urlsplit(current).hostname not in {"example.com", "www.baidu.com", "baidu.com"}:
            report("real_rfb_input_and_transfer", "not_tested", reason="no_available_public_page")
            return {}
        base = urlunsplit((*urlsplit(current)[:4], ""))
        async with viewer(device1, owner, session1) as readonly:
            await readonly.navigate(fragment_url(base, "readonly"))
            await asyncio.sleep(1)
            require((await browser.status())["url"] == current, "readonly_rfb_cannot_navigate")
            report("readonly_rfb_key_events_rejected_by_server")
            state = await browser.control("takeover", device1, initial["browser_epoch"], session_id=session1)
            await asyncio.wait_for(readonly.ws.wait_closed(), 5)
            report("takeover_closes_prior_epoch_viewer")
        try:
            async with viewer(device1, owner, session1) as controlled:
                await controlled.frame()
                target = fragment_url(base, "keyboard")
                await controlled.navigate(target)
                require(await wait_url(target), "real_rfb_keyboard_navigation")
                report("real_rfb_keyboard_controls_chromium")
                pointer_target = fragment_url(base, "pointer")
                await controlled.navigate(pointer_target, pointer=True)
                if await wait_url(pointer_target, 3):
                    report("real_rfb_mouse_click_changes_focus_and_navigation")
                else:
                    report("real_rfb_mouse_click_changes_focus_and_navigation", "not_tested", reason="toolbar_coordinates_require_ui_verification")
                    await controlled.navigate(target)
                    require(await wait_url(target), "keyboard_restores_public_test_page")
                state = await browser.control("transfer", device2, state["browser_epoch"], session_id=session2)
                await asyncio.wait_for(controlled.ws.wait_closed(), 5)
                report("transfer_closes_old_controller_before_acknowledged_new_epoch")
            async with viewer(device2, owner, session2) as transferred:
                target = fragment_url(base, "transferred")
                await transferred.navigate(target)
                require(await wait_url(target), "transferred_controller_input")
                async with viewer(str(uuid.uuid4()), owner, str(uuid.uuid4())) as other:
                    await other.navigate(fragment_url(base, "forged"))
                    await asyncio.sleep(1)
                    require((await browser.status())["url"] == target, "other_device_rfb_input_rejected")
                report("other_device_cannot_forge_control_with_rfb_frames")
                await browser.revoke_session(session2)
                await asyncio.wait_for(transferred.ws.wait_closed(), 5)
                revoked = await browser.status()
                require(revoked["mode"] == "human" and revoked["device_id"] is None, "revocation_does_not_resume_agent")
                report("controller_session_revocation_closes_stream_and_input_without_agent_resume")
        finally:
            await browser.quiesce()
        return {}

    if phase == "profile":
        name = "personal_bot_probe_" + uuid.uuid4().hex[:12]
        value = uuid.uuid4().hex
        set_url = "https://httpbin.org/response-headers?Set-Cookie=" + quote(name + "=" + value + "; Max-Age=600; Path=/; Secure; SameSite=Lax", safe="")
        clear_url = "https://httpbin.org/response-headers?Set-Cookie=" + quote(name + "=; Max-Age=0; Path=/; Secure; SameSite=Lax", safe="")
        cookie_was_set = False
        try:
            empty = await navigate("https://httpbin.org/cookies")
            await navigate(set_url)
            cookie_was_set = True
            before = await navigate("https://httpbin.org/cookies")
            require(before["account_fingerprint"] != empty["account_fingerprint"], "public_fixture_cookie_was_set")
            await browser.quiesce()
            after = await navigate("https://httpbin.org/cookies")
            require(before["account_fingerprint"] == after["account_fingerprint"], "persistent_profile_browser_restart_cookie")
            report("persistent_profile_cookie_survives_real_browser_restart", scope="browser_restart_only")
        except httpx.HTTPError:
            report("persistent_profile_cookie_survives_real_browser_restart", "not_tested", reason="public_cookie_fixture_unavailable")
        finally:
            if cookie_was_set:
                try:
                    await navigate(clear_url)
                except httpx.HTTPError:
                    report("public_fixture_cookie_cleanup", "not_tested", reason="public_cookie_fixture_unavailable")
            await browser.quiesce()
        return {}

    if phase == "finish":
        state = await browser.quiesce()
        require(state["running"] is False, "browser_quiesce_stops_chromium")
        report("browser_quiesce_stops_chromium")
        return {}
    raise ProbeFailure("unknown_api_phase")


def worker_phase(arguments: dict) -> dict:
    require(os.getuid() == 31002, "actual_worker_uid")
    for key in ["authority", "socket"]:
        try:
            if key == "authority":
                descriptor = os.open(arguments[key], os.O_RDONLY)
                os.close(descriptor)
            else:
                connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    connection.connect(arguments[key])
                finally:
                    connection.close()
        except PermissionError:
            report("worker_cannot_access_browser_" + key)
        else:
            raise ProbeFailure("worker_accessed_browser_" + key)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(3)
        try:
            connection.connect("/tmp/.X11-unix/X99")
            connection.sendall(struct.pack("<BBHHHHH", ord("l"), 0, 11, 0, 0, 0, 0))
            response = connection.recv(256)
            require(bool(response) and response[0] in {0, 2}, "worker_x11_unauthenticated_setup_rejected")
        except PermissionError:
            pass
    report("worker_x11_setup_without_private_cookie_rejected")
    return {}


def processes() -> list[dict]:
    result = []
    for directory in Path("/proc").iterdir():
        if not directory.name.isdigit():
            continue
        try:
            status = dict(line.split(":", 1) for line in (directory / "status").read_text().splitlines() if ":" in line)
            cmdline_permission_denied = False
            try:
                arguments = [item.decode(errors="replace") for item in (directory / "cmdline").read_bytes().split(b"\x00") if item]
                # Chromium can rewrite a child's process title as a complete
                # space-separated command in argv[0]. Parse only our fixed
                # Playwright binary layout and exact kernel process name. These
                # tokens are inspected in memory, never executed or reported.
                if (arguments and status.get("Name", "").strip() == "chrome"
                        and re.match(r"^/opt/browser/chromium-[0-9]+/chrome-linux(?:64)?/chrome (?=--)", arguments[0])):
                    arguments = arguments[0].split() + arguments[1:]
            except PermissionError:
                arguments = []
                cmdline_permission_denied = True
            if arguments or cmdline_permission_denied:
                result.append({"path": directory, "arguments": arguments, "status": status,
                               "cmdline_permission_denied": cmdline_permission_denied})
        except (OSError, ValueError):
            continue
    return result


def inspect_browser() -> dict:
    rows = processes()
    vnc = [row for row in rows if row["arguments"] and Path(row["arguments"][0]).name == "x11vnc"]
    xvfb = [row for row in rows if row["arguments"] and Path(row["arguments"][0]).name == "Xvfb"]
    require(len(vnc) == len(xvfb) == 1, "one_x11_and_vnc_process")
    vnc_args, x_args = vnc[0]["arguments"], xvfb[0]["arguments"]
    authority = Path(x_args[x_args.index("-auth") + 1])
    socket_path = Path(vnc_args[vnc_args.index("-unixsock") + 1])
    require(authority.parent == socket_path.parent and authority.parent.parent == Path("/tmp"), "private_browser_transport_parent")
    for path, mode in [(authority.parent, 0o700), (authority, 0o600), (socket_path, 0o600)]:
        metadata = path.stat()
        require(metadata.st_uid == 31003 and stat.S_IMODE(metadata.st_mode) == mode, "private_browser_transport_ownership_and_mode")
    require(socket_path.is_socket(), "private_vnc_is_unix_socket")
    report("browser_private_directory_xauthority_and_vnc_socket_modes")
    for proc_name in ["tcp", "tcp6"]:
        source = Path("/proc/net") / proc_name
        if source.exists():
            for line in source.read_text().splitlines()[1:]:
                columns = line.split()
                require(not (columns[3] == "0A" and int(columns[1].split(":")[1], 16) in {5900, 6099}), "no_public_or_loopback_vnc_x11_tcp_listener")
    report("vnc_and_x11_have_no_ipv4_or_ipv6_tcp_listener")
    chrome_names = {"chrome", "chromium", "chromium-browser"}
    chrome = [row for row in rows if row["arguments"] and Path(row["arguments"][0]).name in chrome_names]
    main = [row for row in chrome if not any(value.startswith("--type=") for value in row["arguments"])]
    renderers = [row for row in chrome if "--type=renderer" in row["arguments"]]
    disabled = {"--no-sandbox", "--disable-seccomp-filter-sandbox", "--disable-namespace-sandbox", "--disable-setuid-sandbox"}
    require(not any(set(row["arguments"]) & disabled for row in chrome), "chromium_sandbox_not_disabled")
    inaccessible_browser_metadata = [row for row in rows
                                     if row["cmdline_permission_denied"]
                                     and row["status"].get("Name", "").strip() in chrome_names
                                     and row["status"].get("Uid", "").split()[:1] == ["31003"]]
    if inaccessible_browser_metadata:
        # A capability-free container root cannot necessarily inspect Chromium's
        # nondumpable children in a nested user namespace. Do not add SYS_PTRACE
        # or infer renderer identity/sandbox state from an unreadable cmdline.
        # Keep real RFB, cross-UID transport and input/revocation checks running;
        # the host-root coordinator must provide the missing process evidence.
        for check in ["actual_chromium_renderer_process", "chromium_has_no_sandbox_disabling_flags",
                      "renderers_add_chromium_seccomp_filters_beyond_container_policy",
                      "renderer_user_namespace_mappings_differ_from_browser"]:
            report(check, "not_tested", reason="proc_process_metadata_permission_requires_host_root")
        return {"authority": str(authority), "socket": str(socket_path)}
    require(len(main) == 1 and bool(renderers), "actual_chromium_renderer_process")
    report("chromium_has_no_sandbox_disabling_flags", renderers=len(renderers))
    outer_filters = int(main[0]["status"].get("Seccomp_filters", "0"))
    require(outer_filters > 0, "container_seccomp_filter_present")
    for row in renderers:
        fields = row["status"]
        require(fields.get("NoNewPrivs", "").strip() == "1" and fields.get("Seccomp", "").strip() == "2", "renderer_no_new_privs_and_seccomp")
        require(int(fields.get("Seccomp_filters", "0")) > outer_filters, "renderer_added_own_seccomp_filter")
        require(int(fields.get("CapEff", "0"), 16) == 0, "renderer_has_no_effective_capabilities")
    report("renderers_add_chromium_seccomp_filters_beyond_container_policy", minimum_filters=min(int(row["status"]["Seccomp_filters"]) for row in renderers))
    try:
        main_map = (main[0]["path"] / "uid_map").read_text().strip()
        require(all((row["path"] / "uid_map").read_text().strip() != main_map for row in renderers), "renderer_has_distinct_user_namespace_mapping")
        report("renderer_user_namespace_mappings_differ_from_browser")
    except PermissionError:
        report("renderer_user_namespace_mappings_differ_from_browser", "not_tested", reason="proc_namespace_metadata_permission")
    return {"authority": str(authority), "socket": str(socket_path)}


def child(phase: str, *, worker_arguments: dict | None = None) -> dict:
    command = [sys.executable, str(Path(__file__).resolve()), "--phase", phase]
    uid = 31002 if phase == "worker" else 31001
    result = subprocess.run(command, input=json.dumps(worker_arguments or {}), capture_output=True, text=True,
                            timeout=170, user=uid, group=31000, extra_groups=[])
    try:
        payload = json.loads(result.stdout)
    except (ValueError, TypeError):
        raise ProbeFailure("child_phase_did_not_return_bounded_report_" + phase) from None
    REPORTS.extend(payload.get("checks", []))
    require(result.returncode == 0, "child_phase_failed_" + phase)
    return payload.get("result", {})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=["start", "worker", "display", "rfb", "profile", "finish"])
    args = parser.parse_args()
    require(sys.platform == "linux" and os.environ.get("PERSONAL_BOT_ACCEPTANCE_PROBE") == "1", "explicit_isolated_linux_acceptance_required")
    result = {}
    try:
        if args.phase:
            if args.phase == "worker":
                result = worker_phase(json.loads(sys.stdin.read()))
            else:
                result = asyncio.run(api_phase(args.phase))
        else:
            require(os.getuid() == 0, "root_coordinator_required_for_real_uid_negative_tests")
            try:
                started = child("start")
                metadata = inspect_browser()
                child("worker", worker_arguments=metadata)
                child("display")
                if started.get("public_source") is not None:
                    child("rfb")
                else:
                    report("real_rfb_input_and_transfer", "not_tested", reason="no_available_public_page")
                child("profile")
            finally:
                child("finish")
            report("https_gateway_and_real_novnc_ui", "not_tested", reason="root_must_run_separate_https_ui_acceptance")
            report("profile_survives_container_recreation", "not_tested", reason="root_must_recreate_temporary_runtime_and_recheck")
    except Exception as exc:
        report(str(exc) if isinstance(exc, ProbeFailure) else "probe_exception_" + type(exc).__name__, "failed")
    failed = any(item["status"] == "failed" for item in REPORTS)
    print(json.dumps({"checks": REPORTS, "result": result, "native": True, "model_calls": 0}, ensure_ascii=True), flush=True)
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
