using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.Json;
using System.Threading;
using Unbroken.LaunchBox.Plugins;
using Unbroken.LaunchBox.Plugins.Data;

namespace CydPinballCards;

/// <summary>
/// LaunchBox / Big Box plugin: on selection, launch, and exit, hand the game (and its
/// LaunchBox media paths) to frontends/launchbox/cyd_launchbox.py, which pushes role
/// cards to the cyd-pinball-cards daemon on 127.0.0.1:47291.
/// </summary>
public sealed class Plugin : ISystemEventsPlugin, IGameLaunchingPlugin
{
    static readonly object Gate = new();
    static string? _python;
    static string? _script;
    static string? _lastSelectKey;
    static DateTime _lastSelectUtc = DateTime.MinValue;
    static int _seq;

    public void OnEventRaised(string eventType)
    {
        if (eventType == SystemEventTypes.PluginInitialized)
        {
            ResolvePaths();
            return;
        }
        if (eventType == SystemEventTypes.SelectionChanged)
        {
            var games = PluginHelper.StateManager?.GetAllSelectedGames();
            var game = games?.FirstOrDefault();
            if (game == null) return;
            var key = (game.Id ?? "") + "|" + (game.Title ?? "");
            lock (Gate)
            {
                var now = DateTime.UtcNow;
                if (key == _lastSelectKey && (now - _lastSelectUtc).TotalMilliseconds < 250)
                    return;
                _lastSelectKey = key;
                _lastSelectUtc = now;
            }
            Push("select", game);
        }
    }

    public void OnBeforeGameLaunching(IGame? game, IAdditionalApplication? app, IEmulator? emulator) { }

    public void OnAfterGameLaunched(IGame? game, IAdditionalApplication? app, IEmulator? emulator)
    {
        if (game != null) Push("launch", game);
    }

    public void OnGameExited()
        => PushIdle();

    static void Push(string eventName, IGame game)
    {
        try
        {
            ResolvePaths();
            if (string.IsNullOrEmpty(_script)) return;

            var payload = new
            {
                event_name = eventName,
                title = game.Title ?? "",
                platform = game.Platform ?? "",
                notes = game.Notes ?? "",
                application_path = game.ApplicationPath ?? "",
                video_path = FirstPath(game.VideoPath, SafeGet(() => game.GetVideoPath())),
                manual_path = FirstPath(game.ManualPath, SafeGet(() => game.GetManualPath())),
                control_panel = FirstImage(game, ImageTypes.ArcadeControlPanel),
                controls_info = FirstImage(game, ImageTypes.ArcadeControlsInformation),
                box_front = FirstImage(game, ImageTypes.BoxFront)
                            ?? FirstImage(game, ImageTypes.BoxFrontReconstructed),
                screenshot = FirstImage(game, ImageTypes.ScreenshotGameplay)
                             ?? FirstImage(game, ImageTypes.ScreenshotGameTitle),
                launched = eventName == "launch"
            };

            var dir = Path.Combine(Path.GetTempPath(), "cyd-pinball-cards");
            Directory.CreateDirectory(dir);
            var n = Interlocked.Increment(ref _seq);
            var jsonPath = Path.Combine(dir, $"lb-event-{n}.json");
            File.WriteAllText(jsonPath, JsonSerializer.Serialize(payload), Encoding.UTF8);
            StartPython(new[] { "--event-file", jsonPath });
        }
        catch
        {
            // Never break LaunchBox because the display bridge failed.
        }
    }

    static void PushIdle()
    {
        try
        {
            ResolvePaths();
            if (string.IsNullOrEmpty(_script)) return;
            StartPython(new[] { "--idle" });
        }
        catch { }
    }

    static void StartPython(string[] args)
    {
        var psi = new ProcessStartInfo
        {
            FileName = _python!,
            UseShellExecute = false,
            CreateNoWindow = true,
            WorkingDirectory = Path.GetDirectoryName(_script!) ?? ""
        };
        psi.ArgumentList.Add(_script!);
        foreach (var a in args) psi.ArgumentList.Add(a);
        Process.Start(psi);
    }

    static string? FirstImage(IGame game, string imageType)
    {
        try
        {
            var imgs = game.GetAllImagesWithDetails(imageType);
            var hit = imgs?.FirstOrDefault(i => !string.IsNullOrWhiteSpace(ImageFilePath(i)));
            return hit == null ? null : ImageFilePath(hit);
        }
        catch
        {
            return null;
        }
    }

    // ImageDetails property name varies by LaunchBox version; try FilePath then ImagePath.
    static string? ImageFilePath(object details)
    {
        var t = details.GetType();
        foreach (var name in new[] { "FilePath", "ImagePath", "Path" })
        {
            var p = t.GetProperty(name);
            if (p != null)
            {
                var v = p.GetValue(details) as string;
                if (!string.IsNullOrWhiteSpace(v)) return v;
            }
        }
        return null;
    }

    static string? FirstPath(params string?[] paths)
        => paths.FirstOrDefault(p => !string.IsNullOrWhiteSpace(p));

    static string? SafeGet(Func<string?> f)
    {
        try { return f(); }
        catch { return null; }
    }

    static void ResolvePaths()
    {
        if (!string.IsNullOrEmpty(_script) && !string.IsNullOrEmpty(_python)) return;
        try
        {
            var dllDir = Path.GetDirectoryName(typeof(Plugin).Assembly.Location) ?? "";
            var cfgPath = Path.Combine(dllDir, "cyd_launchbox.cfg");
            string? home = null;
            string? py = null;
            if (File.Exists(cfgPath))
            {
                foreach (var raw in File.ReadAllLines(cfgPath))
                {
                    var line = raw.Trim();
                    if (line.Length == 0 || line.StartsWith("#") || !line.Contains('=')) continue;
                    var i = line.IndexOf('=');
                    var k = line.Substring(0, i).Trim();
                    var v = line.Substring(i + 1).Trim().Trim('"');
                    if (k.Equals("CYD_HOME", StringComparison.OrdinalIgnoreCase)) home = v;
                    else if (k.Equals("PYTHON", StringComparison.OrdinalIgnoreCase)) py = v;
                }
            }
            home ??= FindCydHome(dllDir);
            if (string.IsNullOrEmpty(home)) return;
            var script = Path.Combine(home, "frontends", "launchbox", "cyd_launchbox.py");
            if (!File.Exists(script)) return;
            _script = script;
            _python = FindPython(py);
        }
        catch { }
    }

    static string? FindCydHome(string dllDir)
    {
        var cand = new[]
        {
            Environment.GetEnvironmentVariable("CYD_HOME"),
            Path.GetFullPath(Path.Combine(dllDir, "..", "..", "..", "..", "..")),
            Path.GetFullPath(Path.Combine(dllDir, "..", "..", "..", "..")),
            @"C:\Users\fcrews\projects\cyd-pinball-cards"
        };
        foreach (var c in cand)
        {
            if (string.IsNullOrWhiteSpace(c)) continue;
            if (File.Exists(Path.Combine(c, "frontends", "launchbox", "cyd_launchbox.py")))
                return c;
        }
        return null;
    }

    static string FindPython(string? preferred)
    {
        if (!string.IsNullOrWhiteSpace(preferred) && File.Exists(preferred)) return preferred!;
        foreach (var name in new[] { "pythonw.exe", "python.exe" })
        {
            try
            {
                var psi = new ProcessStartInfo
                {
                    FileName = "where.exe",
                    Arguments = name,
                    UseShellExecute = false,
                    RedirectStandardOutput = true,
                    CreateNoWindow = true
                };
                using var p = Process.Start(psi);
                var line = p?.StandardOutput.ReadLine()?.Trim();
                p?.WaitForExit(3000);
                if (!string.IsNullOrEmpty(line) && File.Exists(line)) return line;
            }
            catch { }
        }
        return "pythonw";
    }
}
