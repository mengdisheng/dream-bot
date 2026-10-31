"""Fixed fresh-process entrypoint. No untrusted code executes before isolation."""
from __future__ import annotations

import contextlib
import ctypes
import errno
import importlib
import io
import json
import os
from pathlib import Path
import sys


def isolate(job: Path) -> dict:
    if os.uname().machine != "x86_64" or os.getuid() != 31004:
        raise RuntimeError("Unsupported sandbox architecture or identity")
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    if libc.prctl(38, 1, 0, 0, 0):
        raise OSError(ctypes.get_errno(), "no_new_privs")
    abi = libc.syscall(444, 0, 0, 1)
    if abi < 3:
        raise RuntimeError("Landlock ABI >=3 required")

    class Ruleset(ctypes.Structure):
        _fields_ = [("handled_access_fs", ctypes.c_uint64)]

    class PathBeneath(ctypes.Structure):
        _pack_ = 1
        _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]

    handled = (1 << (16 if abi >= 5 else 15)) - 1
    attributes = Ruleset(handled)
    ruleset = libc.syscall(444, ctypes.byref(attributes), ctypes.sizeof(attributes), 0)
    if ruleset < 0:
        raise OSError(ctypes.get_errno(), "landlock_create_ruleset")
    read = (1 << 2) | (1 << 3)
    rw = read | (1 << 1) | (1 << 4) | (1 << 5) | (1 << 7) | (1 << 8) | (1 << 12) | (1 << 13) | (1 << 14)
    paths = [(job, rw)]
    for p in [sys.base_prefix + "/lib", "/usr/local/lib", "/usr/lib", "/lib", "/lib64", "/usr/share/fonts", "/usr/share/fontconfig"]:
        if Path(p).is_dir():
            paths.append((Path(p), read))
    try:
        for path, access in paths:
            fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                rule = PathBeneath(access, fd)
                if libc.syscall(445, ruleset, 1, ctypes.byref(rule), 0):
                    raise OSError(ctypes.get_errno(), "landlock_add_rule")
            finally:
                os.close(fd)
        if libc.syscall(446, ruleset, 0):
            raise OSError(ctypes.get_errno(), "landlock_restrict_self")
    finally:
        os.close(ruleset)

    class Filter(ctypes.Structure):
        _fields_ = [("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte), ("jf", ctypes.c_ubyte), ("k", ctypes.c_uint32)]

    class Program(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ushort), ("filter", ctypes.POINTER(Filter))]

    # Native x86_64 only. All network, exec, fork/clone, ptrace, namespace and
    # io_uring operations are absent. Unknown/x32 syscalls return EPERM.
    allowed = {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 17, 18,
               19, 20, 21, 23, 24, 25, 28, 32, 33, 35, 39, 40, 60, 63, 72,
               73, 74, 75, 76, 77, 78, 79, 80, 81, 82, 83, 84, 85, 86, 87,
               88, 89, 95, 96, 97, 98, 99, 102, 104, 107, 108, 110, 111,
               131, 158, 186, 202, 204, 217, 218, 219, 228, 229, 230, 231,
               257, 258, 259, 260, 262, 263, 264, 265, 266, 267, 269, 270,
               271, 273, 281, 292, 293, 295, 296, 318, 332, 334, 436, 437}
    filters = [Filter(0x20, 0, 0, 4), Filter(0x15, 1, 0, 0xC000003E),
               Filter(0x06, 0, 0, 0x80000000), Filter(0x20, 0, 0, 0)]
    # CPython verifies close-on-exec on descriptors supplied by an opener
    # (including tempfile.NamedTemporaryFile) using FIOCLEX/FIONCLEX. Permit
    # only those two descriptor flags, never general device/terminal ioctls.
    filters.extend([Filter(0x15, 0, 5, 16), Filter(0x20, 0, 0, 24),
                    Filter(0x15, 2, 0, 0x5451), Filter(0x15, 1, 0, 0x5450),
                    Filter(0x06, 0, 0, 0x00050000 | errno.EPERM),
                    Filter(0x06, 0, 0, 0x7FFF0000)])
    for number in sorted(allowed):
        filters.extend([Filter(0x15, 0, 1, number), Filter(0x06, 0, 0, 0x7FFF0000)])
    filters.append(Filter(0x06, 0, 0, 0x00050000 | errno.EPERM))
    array = (Filter * len(filters))(*filters)
    program = Program(len(filters), array)
    # prctl's pointer argument has to be passed as an unsigned machine word.
    if libc.prctl(22, 2, ctypes.addressof(program), 0, 0):
        raise OSError(ctypes.get_errno(), "seccomp_filter")
    return {"landlock_abi": abi, "seccomp": True, "uid": os.getuid()}


class LimitedText(io.StringIO):
    def __init__(self,budget: dict):
        super().__init__()
        self.budget=budget

    def write(self, value: str) -> int:
        size=len(value.encode('utf-8'))
        if self.budget['bytes'] + size > 10 * 1024 * 1024:
            raise RuntimeError("Text output limit exceeded")
        self.budget['bytes']+=size
        return super().write(value)


def preload_dependencies(request: dict) -> dict:
    """Load only fixed trusted libraries before applying the isolation filter.

    Filenames choose among constants, never an arbitrary import or module path.
    General Python jobs keep the complete existing document/image environment;
    plain-text parsing need not pay for all of those libraries in a fresh child.
    """
    modules=set()
    action=request.get('action')
    if action in {'python','execute'}:
        modules.update(('pypdf','docx','openpyxl','reportlab','PIL.Image'))
    elif action=='parse':
        for item in request.get('input_files',[]):
            name=item['name'].lower()
            if name.endswith('.pdf'):
                modules.add('pypdf')
            elif name.endswith('.docx'):
                modules.add('docx')
            elif name.endswith('.xlsx'):
                modules.add('openpyxl')
    return {name:importlib.import_module(name)
            for name in ('pypdf','docx','openpyxl','reportlab','PIL.Image') if name in modules}


def main() -> None:
    import resource

    job = Path(sys.argv[1])
    request = json.loads((job / "request.json").read_text())
    # Trusted imports only, before the filter; no optional unsafe fallback.
    dependencies=preload_dependencies(request)
    if len(os.listdir('/proc/self/task'))!=1:
        raise RuntimeError('Executor must be single-threaded before isolation')
    limits = request.get("limits", {})
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (min(int(limits.get("cpu_seconds", 60)), 60),) * 2)
    resource.setrlimit(resource.RLIMIT_AS, (min(int(limits.get("address_space_bytes", limits.get("memory_bytes", 2 * 1024**3))), 2 * 1024**3),) * 2)
    resource.setrlimit(resource.RLIMIT_FSIZE, (200 * 1024**2,) * 2)
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_NPROC, (1, 1))
    security = isolate(job)
    os.chdir(job / "outputs")
    budget={'bytes':0}
    output, error = LimitedText(budget), LimitedText(budget)
    result = {"status": "succeeded", "security": security}
    try:
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            action = request["action"]
            if action == "selftest":
                import socket
                import subprocess
                tests = {}
                for name, operation in [("filesystem", lambda: Path('/etc/hostname').read_text()),
                                        ("network", lambda: socket.socket()),
                                        ("exec", lambda: subprocess.run(['/bin/true'])),
                                        ("fork", os.fork)]:
                    try:
                        operation()
                        tests[name] = False
                    except OSError:
                        tests[name] = True
                result["checks"] = tests
                if not all(tests.values()):
                    raise RuntimeError("Sandbox isolation self-test failed")
            elif action == "parse":
                chunks = []
                for item in request["input_files"]:
                    path = job / "inputs" / item["id"]
                    name = item["name"].lower()
                    if name.endswith('.pdf'):
                        text = '\n'.join(page.extract_text() or '' for page in dependencies['pypdf'].PdfReader(str(path)).pages)
                    elif name.endswith('.docx'):
                        document = dependencies['docx'].Document(str(path))
                        text = '\n'.join([p.text for p in document.paragraphs] + ['\t'.join(c.text for c in row.cells) for t in document.tables for row in t.rows])
                    elif name.endswith('.xlsx'):
                        workbook = dependencies['openpyxl'].load_workbook(path.open('rb'), read_only=True, data_only=True)
                        text = '\n'.join(f'[{sheet.title}]\n' + '\n'.join('\t'.join('' if c is None else str(c) for c in row) for row in sheet.iter_rows(values_only=True)) for sheet in workbook)
                        workbook.close()
                    elif name.endswith(('.txt', '.md', '.csv', '.json')):
                        text = path.read_text(encoding='utf-8-sig')
                    else:
                        raise ValueError('Unsupported file type; OCR and legacy Office are not provided')
                    chunks.append({"id": item["id"], "name": item["name"], "text": text[:4*1024*1024],
                                   "original_chars":len(text),"truncated":len(text)>4*1024*1024})
                result['documents'] = chunks
                result['text'] = '\n\n'.join(x['text'] for x in chunks)
                result['parser_truncated']=any(x['truncated'] for x in chunks)
            elif action in {'python', 'execute'}:
                context = {"__name__": "__sandbox__", "input_files": request["input_files"],
                           "input_dir": str(job/'inputs'), "output_dir": str(job/'outputs')}
                exec(compile(request.get('code') or '', '<task>', 'exec'), context, context)
            else:
                raise ValueError('Unsupported sandbox action')
    except BaseException as exc:
        result['status'] = 'failed'
        result['error'] = {'code': 'execution_failed', 'message': str(exc)[:500]}
    result['stdout'] = output.getvalue()
    result['stderr'] = error.getvalue()
    (job / 'result.json').write_text(json.dumps(result, ensure_ascii=False),encoding='utf-8')


if __name__ == '__main__':
    main()
