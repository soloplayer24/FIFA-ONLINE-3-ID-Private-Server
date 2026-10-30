# Capture DNS + HTTP with built-in pktmon while running run3.ps1. Run elevated.
# ETW rejects long paths, so capture to a short temp path and copy back.
param([string]$d)
$etl = "$env:SystemRoot\Temp\fo3cap.etl"; $pcap = "$env:SystemRoot\Temp\fo3cap.pcapng"
pktmon filter remove | Out-Null
pktmon filter add FO3DNS -p 53 | Out-Null
pktmon filter add FO3HTTP -p 80 | Out-Null
pktmon start --capture --pkt-size 0 --file-name $etl | Out-Null
& "$d\run3.ps1" -d $d
pktmon stop | Out-Null
pktmon filter remove | Out-Null          # leave no persistent filter
pktmon etl2pcap $etl --out $pcap | Out-Null
Copy-Item $pcap "$d\g4.pcapng" -Force
Remove-Item $etl, $pcap -Force -ErrorAction SilentlyContinue
"pktmon done"
