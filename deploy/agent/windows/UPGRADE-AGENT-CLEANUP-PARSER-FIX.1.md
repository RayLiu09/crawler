# Desktop Agent 0.2.0 browser-cleanup-parser-fix.1 upgrade

This wheel-only upgrade includes:

- closes the source browser when a leased query reaches completed, paused,
  cancelled, or failed state, with Agent shutdown cleanup retained as a
  fallback;
- preserves unlabelled numbered or bulleted Zhaopin descriptions as
  `jobResponsibilitiesText`;
- recognizes `工作职责` as a description section heading;
- keeps trailing unlabelled text after numbered duties in
  `otherMattersText`;
- raises the collection page-limit contract and Zhaopin default maximum to 50;
- reads the configurable maximum from `ZHAOPIN_MAX_PAGE_LIMIT` (default `50`).

The upgrade replaces only the `crawler_contracts` and
`desktop_collection_agent` wheels. It retains the Agent identity, device
credential, environment file, scheduled task, browser Profile, and SQLite
spool.

Run these commands as the Windows user that owns the scheduled task, from the
extracted package root:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\JobCollectionAgent\release\agent\windows\stop.ps1

$Python = 'C:\JobCollectionAgent\release\venv\Scripts\python.exe'
$Wheelhouse = '.\wheelhouse'

& $Python -m pip install --no-index --no-deps --force-reinstall `
  "$Wheelhouse\crawler_contracts-0.2.0-py3-none-any.whl"
& $Python -m pip install --no-index --no-deps --force-reinstall `
  "$Wheelhouse\desktop_collection_agent-0.2.0-py3-none-any.whl"

& $Python -c @'
from importlib.metadata import version
from crawler_contracts.collection_jobs import CollectionQuery

assert version("crawler-contracts") == "0.2.0"
assert version("desktop-collection-agent") == "0.2.0"
assert CollectionQuery(keyword="test", page_limit=50).page_limit == 50
print("Agent upgrade package verification passed")
'@

C:\JobCollectionAgent\release\venv\Scripts\desktop-collection-agent-check-config.exe `
  --config C:\JobCollectionAgent\release\agent-node-01.env

powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File C:\JobCollectionAgent\release\agent\windows\start.ps1
```

The existing `ZHAOPIN_MAX_PAGE_LIMIT` value is optional. If omitted, the
default maximum is 50.
