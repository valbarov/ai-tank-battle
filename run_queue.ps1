# Runs games one after another: waits for the current game to finish, then starts the next.
param([int[]]$Seeds = @(10, 11), [string]$Models = "haiku sonnet opus fable")
$dir = $PSScriptRoot
function Wait-GameOver {
  while ($true) {
    Start-Sleep 10
    try { $s = Get-Content "$dir\state.json" -Raw | ConvertFrom-Json } catch { continue }
    $alive = Get-Process python -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like "*arena.py*" }
    if ($s.over -or -not $alive) { return }
  }
}
foreach ($seed in $Seeds) {
  Wait-GameOver
  Get-Process python -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like "*arena.py*" } | Stop-Process -Force
  Start-Sleep 3
  Start-Process -WindowStyle Hidden python -ArgumentList "arena.py --models $Models --rounds 60 --seed $seed --parallel --effort low --serve" -WorkingDirectory $dir -RedirectStandardOutput "$dir\runs\live_s$seed.log" -RedirectStandardError "$dir\runs\live_s$seed.err"
  Start-Sleep 20
}
Wait-GameOver
"queue done"
