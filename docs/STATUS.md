# Status and validation boundaries

Initial public source release: experimental r19 implementation, published with owner authorization. This repository deliberately excludes private deployment receipts, server inventories, user records, credentials and screenshots.

## Existing implementation evidence

- Native Linux amd64 runtime regression: **280 passed**, with three opt-in real Chromium cases subsequently run separately and **3 passed**.
- Web: **62 tests across 14 files**, strict TypeScript, zero-warning lint and production build passed. The >500-kB main JavaScript chunk warning remains; no performance benefit is inferred from it.
- The running implementation was checked with authenticated HTTPS in desktop and 390×844 mobile Chrome, including real persistent conversation history, file preview, settings edit/delete cancellation and focus behavior.
- Real sandboxed Chromium checks cover same-conversation page continuity, cross-conversation fresh tabs without old back history, retained owner profile cookies and task/epoch authority regressions.
- Natural deletion after about 301 idle seconds and subsequent passive reads without recreation were observed. Existing single-runtime and 300-second policies remain implemented.
- Model protocol, recovery and conversation tests use deterministic provider substitutes where stated. Prior authorized real-provider text/stream/tool validation and bounded chat/file/research samples are separate from those tests. No model key or provider account is shipped.

These are maintainer-recorded engineering results for the existing implementation, not independently reproduced results for every fresh installation. Public CI re-runs the available source-level suites; private receipts are not uploaded as proof artifacts.

## Remaining gates

- Independent clean-machine installation from this public checkout, including artifact provenance and a complete operator handover.
- Native ARM64: not validated or advertised. x86_64 syscall rules need architecture-specific review and actual isolation evidence before support.
- Real MiniMax-driven autonomous browser end-to-end; vision and the full provider/region/plan matrix.
- Enabled schedule delivery, actual Web Push delivery and browser-profile backup restore across the full product acceptance matrix.
- Complete fault/resource/upgrade/rollback matrices, independent security review and stronger supply-chain reproducibility, including a complete Python dependency lock.

The real browser transport, isolated sandbox checks, simulated model flows and actual paid-provider samples are different evidence classes. Passing one does not establish the others. The source is public under MIT; the project's alpha status does not silently relax sandbox, identity, billing or approval checks.
