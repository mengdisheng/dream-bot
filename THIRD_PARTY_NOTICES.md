# Third-party notices

Dream Bot's original code and documentation are MIT licensed. That license does **not** replace the licenses of dependencies or copied upstream material. The initial public release contains source and configuration, not a container image, browser binary or vendored operating-system package set.

## Material carried by this repository

| Material | Upstream and license | Treatment |
| --- | --- | --- |
| Outer Docker seccomp profile | [Microsoft Playwright](https://github.com/microsoft/playwright), Apache-2.0 | Derived from the documented fixed upstream version, with changes in `docs/sandbox-profiles.md`; license in `LICENSES/Apache-2.0.txt` |
| noVNC dependency | [noVNC](https://github.com/novnc/noVNC), MPL-2.0 | Unmodified npm dependency; MPL text, upstream authors and bundled component notices retained under `web/public/third-party-licenses/` |
| React / React DOM | [React](https://github.com/facebook/react), MIT | Dependency license texts retained with the web notices |
| Lucide icons | [Lucide](https://github.com/lucide-icons/lucide), ISC | Dependency license retained with the web notices |
| Noto Sans SC | [Noto fonts](https://github.com/notofonts/noto-cjk), OFL-1.1 | Unmodified Fontsource dependency; its supplied license is retained |
| IBM Plex Sans / Mono | [IBM Plex](https://github.com/IBM/plex), OFL-1.1 | Unmodified Fontsource dependencies; their supplied licenses are retained |

Public web notices are copied unchanged from the installed dependency packages and are served with a frontend build. Additional noVNC component notices are retained alongside its primary MPL text. Dependency versions and their transitive package licenses are indexed in `docs/DEPENDENCIES.json`; web and Go resolution also use `web/package-lock.json` and `host/go.sum`.

MPL-2.0 applies at file scope to noVNC source, including modifications if a downstream distributor makes them; it does not automatically relicense Dream Bot's independent files. The pinned package's corresponding source is available through the upstream repository/npm package referenced by the lockfile. A downstream distributor modifying covered files must provide the corresponding source and notices as required by MPL.

## Build-time and runtime dependencies

Go dependencies include age, webpush-go, robfig/cron, the Go x packages and modernc SQLite. Python dependencies include FastAPI, Uvicorn, HTTPX, cryptography, LangGraph, Playwright, croniter, pypdf, python-docx, openpyxl, reportlab and Pillow. Follow each upstream license; the inventory is a starting point, not a legal conclusion about arbitrary downstream bundles.

The Dockerfile obtains Debian packages, s6, Xvfb, x11vnc, fonts, Chromium and SQLite from their upstream/distribution sources. These include GPL, LGPL, BSD, Apache, OFL and public-domain components. If you distribute a built image or binary, preserve its package copyright files and satisfy the applicable notices and corresponding-source requirements, including GPL/LGPL components. The MIT license on this repository does not eliminate those obligations. No upstream binary or container image is being published by this source-only release.

SQLite is public domain; the official source URL and verified hash are recorded by `deploy/build-sqlite.py` and included in its build output. For the exact outer seccomp derivation and retained architecture limitations, see [sandbox-profiles.md](docs/sandbox-profiles.md).
