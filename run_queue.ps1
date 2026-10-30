# Runs games one after another: waits for the current game to finish, then starts the next.
# Each item in -Games is the full argument string for arena.py (without --serve, which is added).
param(
  [string[]]$Games = @(
    "--models opus sonnet haiku fable --rounds 60 --seed 18 --effort low --fog 4",
    "--models opus sonnet haiku fable --rounds 60 --seed 19 --effort low --fog 4 --size 21",
    "--models opus sonnet haiku fable --rounds 60 --seed 20 --fog 4"
  )
)
$dir = $PSScriptRoot
function Wait-GameOver {
  while ($true) {
    Start-Sleep 10
    try { $s = Get-Content "$dir\state.json" -Raw | ConvertFrom-Json } catch { continue }
    $alive = Get-Process python -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like "*arena.py*" }
    if ($s.over -or -not $alive) { return }
  }
}
$i = 0
foreach ($g in $Games) {
  $i++
  Wait-GameOver
  Get-Process python -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like "*arena.py*" } | Stop-Process -Force
  Start-Sleep 3
  Start-Process -WindowStyle Hidden python -ArgumentList "arena.py $g --serve" -WorkingDirectory $dir -RedirectStandardOutput "$dir\runs\queue_game$i.log" -RedirectStandardError "$dir\runs\queue_game$i.err"
  Start-Sleep 20
}
Wait-GameOver
"queue done"
