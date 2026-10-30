// Replacement for the Garena fifalauncher.exe (Themida-packed; its Garena SSO / version
// servers are dead). Does what the original does at the end of its flow: start fifazf.exe
// with server args and stay alive, because the game is handed the launcher PID.
// Anti-cheat is untouched: fifazf.exe still runs its own XignCode/HackShield init.
//
// Build: C:\Windows\Microsoft.NET\Framework\v4.0.30319\csc.exe /nologo /target:winexe /out:fifalauncher.exe fifalauncher.cs
// Run:   fifalauncher.exe                     -> local defaults below (same as launch_game.ps1)
//        fifalauncher.exe -QUICKMATCH/... ... -> those args instead of the defaults
// fifazf.exe is looked up next to this exe, then in the default install dir.
using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Windows.Forms;

static class Launcher
{
    const string GameDir = @"D:\FO3ID\32795";
    const string Defaults =
        "-QUICKMATCH/SERVER=127.0.0.1:28000 -QUICKMATCH/PROTOCOL=tcp " +
        "-QUICKMATCH/NOTISERVER=127.0.0.1:28001 -PUBLISHER/GARENA/TOKEN=testtoken " +
        "-LOCALE/PUBLISHING_REGION=ID -DUMPTRUCK/SERVER=127.0.0.1 " +
        // Alias pairs (no dash) select the Garena login path; see launch_game.ps1.
        "region id token testtoken";

    [STAThread]
    static int Main(string[] args)
    {
        string exe = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "fifazf.exe");
        if (!File.Exists(exe))
            exe = Path.Combine(GameDir, "fifazf.exe");

        string gameArgs = args.Length == 0
            ? Defaults
            : string.Join(" ", args.Select(a => a.Contains(" ") ? "\"" + a + "\"" : a));
        gameArgs += " -FIFA/LAUNCHER/PID=" + Process.GetCurrentProcess().Id;

        // ShellExecute so fifazf.exe's requireAdministrator manifest gets its UAC prompt.
        var psi = new ProcessStartInfo(exe, gameArgs)
        {
            WorkingDirectory = Path.GetDirectoryName(exe),
            UseShellExecute = true,
        };
        try
        {
            using (var game = Process.Start(psi))
            {
                game.WaitForExit();
                return game.ExitCode;
            }
        }
        catch (Exception e)
        {
            MessageBox.Show(exe + "\n\n" + e.Message, "fifalauncher", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }
}
