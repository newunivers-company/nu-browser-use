$ErrorActionPreference = 'Continue'
$src = 'X:\nu-browser-use\shotdeck_export'
$dst = 'G:\공유 드라이브\개발팀\SHOTDECK'
robocopy $src $dst /E /XF _run_all.log _run_all.err _run_all.pid /R:1 /W:1 /MT:16 /NFL /NDL /NP /NJH /NJS | Out-Null
$code = $LASTEXITCODE
$files = @(Get-ChildItem $dst -Recurse -File -ErrorAction SilentlyContinue)
$mb = [math]::Round((($files | Measure-Object Length -Sum).Sum) / 1MB, 1)
$stamp = (Get-Date).ToString('HH:mm')
Write-Output "[$stamp] sync rc=$code | target: $($files.Count) files, $mb MB"
