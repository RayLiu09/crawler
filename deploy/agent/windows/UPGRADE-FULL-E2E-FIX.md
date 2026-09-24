# Desktop Agent 0.2.0 full-e2e-fix.7 upgrade

This cumulative patch contains the current Zhaopin split-panel parser, company
metadata alignment, complete-description section extraction, nationwide search
when `region` is empty, reviewed core city-code aliases, control-plane retry
handling, and task-location logging.

The upgrade is wheel-only. It does not replace the Agent identity, device
credential, environment file, scheduled task, browser Profile, or SQLite spool.
Run commands as the Windows user that owns the scheduled task. Do not start a
second foreground Agent while the scheduled task is running.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\JobCollectionAgent\release\agent\windows\stop.ps1
Get-ScheduledTask -TaskName 'Job Collection Desktop Agent' |
  Select-Object TaskName,State

$Python = 'C:\JobCollectionAgent\release\venv\Scripts\python.exe'
$Wheelhouse = '.\wheelhouse'

& $Python -m pip install `
  --no-index --no-deps --force-reinstall `
  "$Wheelhouse\crawler_contracts-0.2.0-py3-none-any.whl"

& $Python -m pip install `
  --no-index --no-deps --force-reinstall `
  "$Wheelhouse\desktop_collection_agent-0.2.0-py3-none-any.whl"

& $Python -c @'
from importlib.metadata import version
from crawler_contracts.parsed_jobs import ParsedJobFields

print("crawler-contracts:", version("crawler-contracts"))
print("desktop-collection-agent:", version("desktop-collection-agent"))
assert version("crawler-contracts") == "0.2.0"
assert version("desktop-collection-agent") == "0.2.0"
required = (
    "job_responsibilities_text",
    "job_requirements_text",
    "qualifications_text",
    "other_matters_text",
)
assert all(name in ParsedJobFields.model_fields for name in required)
print("ParsedJobFields: new description sections present")
'@

C:\JobCollectionAgent\release\venv\Scripts\desktop-collection-agent-check-config.exe `
  --config C:\JobCollectionAgent\release\agent-node-01.env

powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File C:\JobCollectionAgent\release\agent\windows\start.ps1
```

Expected configuration output is `Agent configuration is valid`. The scheduled
task should return to `Running`, with `LastTaskResult` equal to `267009`
(`0x41301`) while active.

New diagnostic lines are written to:

```text
C:\JobCollectionAgent\release\data-node-01\logs\desktop-agent.log
```

Look for `task lease acquired`, `adapter collection started`, `zhaopin search
URL built`, `zhaopin listing page loaded`, `record batch uploaded`, `task
checkpoint acknowledged`, and `task execution completed`. These lines contain
task and page identifiers but never access tokens or lease tokens.

For the requested nationwide query, omit `region` from the API query or send an
empty string. The Agent then builds a Zhaopin URL without `jl`; named cities use
the reviewed `CITY_CODES` mapping.
