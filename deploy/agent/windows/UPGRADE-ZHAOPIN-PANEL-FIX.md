# Desktop Agent zhaopin-panel-fix.3 upgrade

This hotfix updates the Zhaopin adapter for the current
`www.zhaopin.com/jobs` split search layout:

- reads job links from the left `job-list-panel`;
- parses the current `job-card` title, salary, skill tags, company, location,
  and position id fields;
- clicks a left-side job and reads details from the right `job-detail-panel`;
- parses `job-detail-summary__company-meta` into financing, company scale, and
  industry fields without splitting industry values that contain `/`;
- keeps the legacy detail-page navigation as a compatibility fallback;
- uses the new `/jobs?jl=...&pageMode=search&kw=...` search URL;
- recognizes current EdgeOne/Tencent verification pages and expanded empty-result markers.

It does not change the Agent identity, device credential, configuration file,
browser Profile, local spool schema, control-plane API, or database schema.
The patch replaces only the `desktop_collection_agent` wheel in the existing
Python 3.12 virtual environment. The earlier lease hotfixes remain included
in the cumulative wheel.

Run every command as the interactive Windows user that owns the scheduled task
and Agent credential. Start from the extracted patch package root. Do not run a
second foreground Agent while the scheduled task is running.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\JobCollectionAgent\release\agent\windows\stop.ps1
Get-ScheduledTask -TaskName 'Job Collection Desktop Agent' | Select-Object TaskName,State

C:\JobCollectionAgent\release\venv\Scripts\python.exe -m pip install --no-index --no-deps --force-reinstall .\wheelhouse\desktop_collection_agent-0.1.0-py3-none-any.whl

C:\JobCollectionAgent\release\venv\Scripts\desktop-collection-agent-check-config.exe --config C:\JobCollectionAgent\release\agent-node-01.env
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\JobCollectionAgent\release\agent\windows\start.ps1

Get-ScheduledTask -TaskName 'Job Collection Desktop Agent' | Select-Object TaskName,State
Get-ScheduledTaskInfo -TaskName 'Job Collection Desktop Agent' | Format-List LastRunTime,LastTaskResult
```

Expected results:

- configuration check prints `Agent configuration is valid`;
- scheduled task state becomes `Running`;
- `LastTaskResult` is `267009` (`0x41301`) while the task is running;
- the next task opens the new `www.zhaopin.com/jobs` page and keeps the
  browser on the split list/detail page while collecting each listing.

The upgrade deliberately retains the existing Agent data directory, SQLite
spool, browser Profile, credentials, and scheduled task. If the source shows
an EdgeOne verification page, complete it in the visible browser; the Adapter
will then continue polling the same search page.
