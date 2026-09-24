# Desktop Agent lease-protocol-fix.2 upgrade

This cumulative hotfix corrects both lease protocol defects found during live
end-to-end validation:

- runtime requests use the current lease expiry after renewal;
- the real lease token is unwrapped only at the outbound HTTP boundary instead
  of sending Pydantic's redacted `**********` representation.

It does not change Agent identity, device credentials, configuration, browser
Profile, local spool schema, control-plane API, or database schema. The earlier
`lease-state-fix.1` artifact is superseded by this package.

Run every command as the interactive Windows user that owns the scheduled task
and credential. Start from the extracted hotfix package root.

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
- `LastTaskResult` is `267009` (`0x41301`) while the task is running.

Do not run a second foreground Agent while the scheduled task is running.
