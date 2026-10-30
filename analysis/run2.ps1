# Run fifazf.exe with 3 command-line syntax variants, 25s each. Run elevated.
param([string]$d)
$py  = (Get-Command python).Source
$exe = 'D:\FO3ID\32795\fifazf.exe'
$kv = [ordered]@{
  'QUICKMATCH/SERVER'        = '127.0.0.1:28000'
  'QUICKMATCH/PROTOCOL'      = 'tcp'
  'QUICKMATCH/NOTISERVER'    = '127.0.0.1:28001'
  'FIFA/LAUNCHER/PID'        = '{PID}'
  'PUBLISHER/GARENA/TOKEN'   = 'testtoken'
  'LOCALE/PUBLISHING_REGION' = 'ID'
  'DUMPTRUCK/SERVER'         = '127.0.0.1'
}
$variants = @{
  'v1' = @($kv.GetEnumerator() | ForEach-Object { "-$($_.Key)=$($_.Value)" })
  'v2' = @($kv.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" })
  'v3' = @($kv.GetEnumerator() | ForEach-Object { "-$($_.Key)"; "$($_.Value)" })
}
foreach ($v in 'v1','v2','v3') {
  "===== $v : $($variants[$v] -join ' ')"
  & $py -I "$d\game.py" $exe 25 "$d\g2$v" '28000,28001' @($variants[$v]) 2>&1
  Get-Process fifazf,xm -ErrorAction SilentlyContinue | Stop-Process -Force
}
