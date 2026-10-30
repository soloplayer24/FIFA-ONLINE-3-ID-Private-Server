# Run fifazf.exe v1 args for 60s with NO memory scan (no handle opened to game). Run elevated.
param([string]$d)
$env:NOSCAN = '1'
$py = (Get-Command python).Source
$a = @('-QUICKMATCH/SERVER=127.0.0.1:28000', '-QUICKMATCH/PROTOCOL=tcp', '-QUICKMATCH/NOTISERVER=127.0.0.1:28001',
       '-FIFA/LAUNCHER/PID={PID}', '-PUBLISHER/GARENA/TOKEN=testtoken', '-LOCALE/PUBLISHING_REGION=ID', '-DUMPTRUCK/SERVER=127.0.0.1')
& $py -I "$d\game.py" 'D:\FO3ID\32795\fifazf.exe' 60 "$d\g3" '28000,28001' @a 2>&1
Get-Process fifazf,xm -ErrorAction SilentlyContinue | Stop-Process -Force
