# Game window control: screenshot via PrintWindow (works while the desktop compositor is
# frozen), key/click via PostMessage (SendKeys does not reach the game).
# Usage: . gamectl.ps1; Shot out.png; Click 958 91; Key 0x20; Req (new http log lines)
Add-Type -ReferencedAssemblies System.Drawing -TypeDefinition @'
using System; using System.Drawing; using System.Runtime.InteropServices;
public static class GameCtl {
  [DllImport("user32.dll")] static extern bool PrintWindow(IntPtr h, IntPtr hdc, uint f);
  [DllImport("user32.dll")] static extern bool GetClientRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] static extern bool PostMessage(IntPtr h, uint m, IntPtr w, IntPtr l);
  public struct RECT { public int L, T, R, B; }
  public static void Save(IntPtr h, string path) {
    RECT r; GetClientRect(h, out r);
    using (var bmp = new Bitmap(Math.Max(1, r.R - r.L), Math.Max(1, r.B - r.T)))
    using (var g = Graphics.FromImage(bmp)) { var dc = g.GetHdc(); PrintWindow(h, dc, 3); g.ReleaseHdc(dc); bmp.Save(path); }
  }
  public static void Key(IntPtr h, int vk) {
    PostMessage(h, 0x0100, (IntPtr)vk, (IntPtr)1); System.Threading.Thread.Sleep(80);
    PostMessage(h, 0x0101, (IntPtr)vk, (IntPtr)unchecked((int)0xC0000001));
  }
  public static void Type(IntPtr h, string s) {
    foreach (char c in s) { PostMessage(h, 0x0102, (IntPtr)c, (IntPtr)1); System.Threading.Thread.Sleep(60); }
  }
  public static void Click(IntPtr h, int x, int y) {
    IntPtr lp = (IntPtr)((y << 16) | x);
    PostMessage(h, 0x0200, IntPtr.Zero, lp); System.Threading.Thread.Sleep(50);
    PostMessage(h, 0x0201, (IntPtr)1, lp); System.Threading.Thread.Sleep(80); PostMessage(h, 0x0202, IntPtr.Zero, lp);
  }
}
'@
$script:GameLog = 'D:\FO3ID PRIVATE SERVER\captures\http_config.log'
function GameHwnd { (Get-Process fifazf -ErrorAction Stop | Select-Object -First 1).MainWindowHandle }
function Shot([string]$name) { [GameCtl]::Save((GameHwnd), (Join-Path $PSScriptRoot $name)) }
function Click([int]$x, [int]$y) { [GameCtl]::Click((GameHwnd), $x, $y) }
function Key([int]$vk) { [GameCtl]::Key((GameHwnd), $vk) }
function TypeText([string]$s) { [GameCtl]::Type((GameHwnd), $s) }  # not "Type": that is a built-in alias
function Launch {
    # fresh windowed game with launcher args; returns once it is at "PRESS ANY KEY"
    $env:__COMPAT_LAYER = 'RunAsInvoker'
    Get-Process fifazf -ErrorAction SilentlyContinue | ForEach-Object { taskkill /F /T /PID $_.Id | Out-Null }
    $ini = 'D:\FO3ID\32795\graphics.ini'
    (Get-Content -LiteralPath $ini) -replace '^FULLSCREEN=1$', 'FULLSCREEN=0' | Set-Content -Encoding ascii -LiteralPath $ini
    Start-Process 'D:\FO3ID\32795\fifazf.exe' -WorkingDirectory 'D:\FO3ID\32795' -ArgumentList '-QUICKMATCH/SERVER=127.0.0.1:28000', '-QUICKMATCH/PROTOCOL=tcp', '-QUICKMATCH/NOTISERVER=127.0.0.1:28001', "-FIFA/LAUNCHER/PID=$PID", '-PUBLISHER/GARENA/TOKEN=testtoken', '-LOCALE/PUBLISHING_REGION=ID', '-DUMPTRUCK/SERVER=127.0.0.1', 'region', 'id', 'token', 'testtoken' | Out-Null
    Start-Sleep 45
}
function Mark { $script:LogMark = (Get-Content $script:GameLog).Count }
function Req { Get-Content $script:GameLog | Select-Object -Skip $script:LogMark | ForEach-Object { $l = $_ -replace ' from 127\.0\.0\.1 host=APSEODESK172', ''; $l.Substring(0, [Math]::Min(600, $l.Length)) } }
