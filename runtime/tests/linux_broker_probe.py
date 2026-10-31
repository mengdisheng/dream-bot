"""Opt-in real-container acceptance probe. Run as API UID 31001.

Uses only freshly generated fixtures; calls the actual executor broker and
cleans only UUIDs created by this process. No mock backend or model calls.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
from pathlib import Path
import shutil
import time
import uuid

from personal_bot.sandbox import execute


async def main() -> None:
    assert os.uname().machine == "x86_64" and os.getuid() == 31001
    from docx import Document
    from openpyxl import Workbook, load_workbook
    from PIL import Image
    from pypdf import PdfReader
    from reportlab.pdfgen import canvas

    fixtures: list[Path] = []
    exports: list[Path] = []
    checks: list[str] = []

    async def run(action="python", code=None, inputs=None, wall=20):
        result = await execute(action=action, code=code, input_files=inputs or [],
                               output_dir="/scratch/outputs", limits={"wall_seconds": wall,
                               "cpu_seconds": 10, "address_space_bytes": 512 * 1024**2},
                               task_id=str(uuid.uuid4()), run_id=str(uuid.uuid4()))
        for item in result.get("artifacts", []):
            exports.append(Path(item["path"]))
        return result

    def fixture(name: str, data: bytes) -> dict:
        identity = str(uuid.uuid4())
        path = Path("/data/files") / identity
        with path.open("xb") as sink:
            sink.write(data)
        path.chmod(0o600)
        fixtures.append(path)
        return {"id": identity, "path": str(path), "name": name, "media_type": "application/octet-stream"}

    def passed(label: str) -> None:
        checks.append(label)
        print("PASS", label, flush=True)

    try:
        result = await run(action="selftest")
        assert result["status"] == "succeeded", result.get("error")
        assert result["security"]["uid"] == 31004
        assert result["security"]["landlock_abi"] >= 3 and result["security"]["seccomp"] is True
        assert all(result["checks"].get(key) is True for key in ["filesystem", "network", "exec", "fork"])
        passed("native_landlock_seccomp_uid_and_denials")

        text = fixture("fixture.txt", "Personal Bot fixture 中文\n".encode())
        csv = fixture("fixture.csv", b"name,value\nalpha,17\n")
        buffer = io.BytesIO()
        document = Document()
        document.add_paragraph("Document fixture 23")
        document.save(buffer)
        docx = fixture("fixture.docx", buffer.getvalue())
        buffer = io.BytesIO()
        workbook = Workbook()
        workbook.active.append(["Spreadsheet fixture", 29])
        workbook.save(buffer)
        workbook.close()
        xlsx = fixture("fixture.xlsx", buffer.getvalue())
        buffer = io.BytesIO()
        pdf = canvas.Canvas(buffer)
        pdf.drawString(60, 700, "PDF fixture 31")
        pdf.save()
        pdf_file = fixture("fixture.pdf", buffer.getvalue())
        result = await run(action="parse", inputs=[text, csv, docx, xlsx, pdf_file])
        assert result["status"] == "succeeded", result.get("error")
        assert len(result["documents"]) == 5
        for expected in ["中文", "alpha", "Document fixture 23", "Spreadsheet fixture", "PDF fixture 31"]:
            assert expected in result["text"]
        passed("real_txt_csv_docx_xlsx_pdf_parsers")

        result = await run(code="""
from pathlib import Path
from PIL import Image
from docx import Document
from openpyxl import Workbook
from reportlab.pdfgen import canvas
Image.new('RGB',(64,32),(10,80,120)).save('fixture.png')
doc=Document(); doc.add_paragraph('Generated document'); doc.save('fixture.docx')
book=Workbook(); book.active.append(['Generated workbook',41]); book.save('fixture.xlsx'); book.close()
pdf=canvas.Canvas('fixture.pdf'); pdf.drawString(60,700,'Generated PDF'); pdf.save()
Path('fixture.md').write_text('# Generated artifact\\n',encoding='utf-8')
print('Generated five artifacts')
"""
        )
        assert result["status"] == "succeeded", result.get("error")
        assert len(result["artifacts"]) == 5
        for artifact in result["artifacts"]:
            path, name = Path(artifact["path"]), artifact["name"]
            assert path.stat().st_uid == 31001 and path.stat().st_nlink == 1
            if name.endswith(".png"):
                with Image.open(path) as image:
                    assert image.size == (64, 32)
                    image.verify()
            elif name.endswith(".pdf"):
                assert "Generated PDF" in PdfReader(path).pages[0].extract_text()
            elif name.endswith(".docx"):
                assert Document(path).paragraphs[0].text == "Generated document"
            elif name.endswith(".xlsx"):
                with path.open("rb") as source:
                    book = load_workbook(source, read_only=True)
                    assert book.active["B1"].value == 41
                    book.close()
            elif name.endswith(".md"):
                assert "Generated artifact" in path.read_text()
        passed("real_office_pdf_png_artifact_generation_and_api_ownership")

        # A caller can grant only its chosen staged input, even though another
        # immutable API file exists in the same host-mounted directory.
        denied_path = fixture("ungranted.txt", b"private fixture; never output")
        denial = f"""
from pathlib import Path
import os,socket,subprocess,ctypes
targets=['/data/app/master.key','/data/browser/Default/Cookies',{denied_path['path']!r},'/proc/self/environ','/run/personal-bot/worker.sock']
checks=[]
for target in targets:
    try: Path(target).read_bytes(); checks.append(False)
    except OSError: checks.append(True)
for operation in [lambda:socket.socket(),lambda:os.fork(),lambda:subprocess.run(['/bin/sh','-c','true']),lambda:ctypes.CDLL(None).syscall(0x40000029,2,1,0)]:
    try:
        value=operation()
        checks.append(value==-1 if isinstance(value,int) else False)
    except OSError: checks.append(True)
assert all(checks), 'isolation denial failed'
print('denials passed')
"""
        result = await run(code=denial, inputs=[text])
        assert result["status"] == "succeeded", result.get("error")
        passed("ungranted_files_secrets_proc_sockets_shell_fork_x32_denied")

        try:
            result = await run(code="import os; os.symlink('/etc/hostname','outside-link')")
            assert result["status"] == "failed" and not result.get("artifacts")
        except Exception as exc:
            import httpx
            if not isinstance(exc, httpx.HTTPStatusError) or exc.response.status_code != 422:
                raise
        passed("symlink_export_rejected")

        before = time.monotonic()
        result = await run(code="while True: pass", wall=1)
        assert result["status"] == "failed" and result["error"]["code"] == "execution_timeout"
        assert time.monotonic() - before < 10
        passed("timeout_kills_and_reaps_executor")
        print(json.dumps({"passed": len(checks), "model_calls": 0, "native": True}))
    finally:
        for path in fixtures:
            path.unlink(missing_ok=True)
        for path in exports:
            path.unlink(missing_ok=True)
            try:
                path.parent.rmdir()
            except OSError:
                pass


if __name__ == "__main__":
    asyncio.run(main())
