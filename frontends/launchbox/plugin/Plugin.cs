using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Net.Sockets;
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
///
/// Desktop LaunchBox 14 raises SystemEventTypes.SelectionChanged on every game pick, same
/// as Big Box. Selections are debounced (only the game you stop on is sent) so fast
/// scrolling cannot deliver cards out of order. The plugin also starts cyd_daemon.py at
/// LaunchBox startup when nothing is listening on 47291 (AUTOSTART_DAEMON=0 turns it off).
/// Log: %TEMP%\cyd-pinball-cards\plugin.log
/// </summary>
public sealed class Plugin : ISystemEventsPlugin, IGameLaunchingPlugin
{
    const int DaemonPort = 47291;
    const int SelectDebounceMs = 300;
    static readonly object Gate = new();
    static readonly UTF8Encoding Utf8NoBom = new(false);   // Python json.loads rejects a BOM
    static string? _python;
    static string? _script;
    static string? _home;
    static bool _autostart = true;
    static string _daemonArgs = "--profile arcade --log %TEMP%\\cyd-daemon.log";
    static string? _lastSentKey;
    static IGame? _pendingGame;
    static Timer? _selectTimer;
    static int _seq;

    public void OnEventRaised(string eventType)
    {
        try
        {
            if (eventType == SystemEventTypes.PluginInitialized)
            {
                ResolvePaths();
                Log($"initialized in {Process.GetCurrentProcess().ProcessName}; script={_script ?? "(not found)"} python={_python ?? "?"}");
                EnsureDaemon();
                return;
            }
            if (eventType == SystemEventTypes.SelectionChanged)
                OnSelectionChanged();
        }
        catch (Exception e)
        {
            Log($"OnEventRaised({eventType}) failed: {e.GetType().Name}: {e.Message}");
        }
    }

    static void OnSelectionChanged()
    {
        var game = PluginHelper.StateManager?.GetAllSelectedGames()?.FirstOrDefault();
        if (game == null) return;
        lock (Gate)
        {
            _pendingGame = game;
            _selectTimer ??= new Timer(_ => FlushSelection(), null, Timeout.Infinite, Timeout.Infinite);
            _selectTimer.Change(SelectDebounceMs, Timeout.Infinite);
        }
    }

    static void FlushSelection()
    {
        IGame? game;
        lock (Gate)
        {
            game = _pendingGame;
            _pendingGame = null;
            if (game == null) return;
            var key = (game.Id ?? "") + "|" + (game.Title ?? "");
            if (key == _lastSentKey) return;    // LaunchBox re-raises for the same game
            _lastSentKey = key;
        }
        Push("select", game);
    }

    public void OnBeforeGameLaunching(IGame? game, IAdditionalApplication? app, IEmulator? emulator) { }

    public void OnAfterGameLaunched(IGame? game, IAdditionalApplication? app, IEmulator? emulator)
    {
        if (game == null) return;
        lock (Gate)
        {
            _pendingGame = null;
            _selectTimer?.Change(Timeout.Infinite, Timeout.Infinite);
            _lastSentKey = null;   // after exit, re-selecting the same game sends again
        }
        Push("launch", game);
    }

    public void OnGameExited()
        => PushIdle();

    static void Push(string eventName, IGame game)
    {
        try
        {
            ResolvePaths();
            if (string.IsNullOrEmpty(_script)) { Log($"{eventName} '{game.Title}': cyd_launchbox.py not found"); return; }
            EnsureDaemon();

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
            File.WriteAllText(jsonPath, JsonSerializer.Serialize(payload), Utf8NoBom);
            var pid = StartPython(new[] { "--event-file", jsonPath });
            Log($"{eventName} '{game.Title}' ({game.Platform}) -> {Path.GetFileName(jsonPath)} pid {pid}");
        }
        catch (Exception e)
        {
            // Never break LaunchBox because the display bridge failed.
            Log($"{eventName} '{game.Title}' failed: {e.GetType().Name}: {e.Message}");
        }
    }

    static void PushIdle()
    {
        try
        {
            ResolvePaths();
            if (string.IsNullOrEmpty(_script)) return;
            lock (Gate) _lastSentKey = null;
            var pid = StartPython(new[] { "--idle" });
            Log($"exit -> idle pid {pid}");
        }
        catch (Exception e) { Log($"idle failed: {e.GetType().Name}: {e.Message}"); }
    }

    static bool DaemonListening()
    {
        try
        {
            using var c = new TcpClient();
            return c.ConnectAsync("127.0.0.1", DaemonPort).Wait(400) && c.Connected;
        }
        catch { return false; }
    }

    static DateTime _lastDaemonStartUtc = DateTime.MinValue;

    static void EnsureDaemon()
    {
        try
        {
            if (!_autostart || string.IsNullOrEmpty(_home) || DaemonListening()) return;
            lock (Gate)
            {
                if ((DateTime.UtcNow - _lastDaemonStartUtc).TotalSeconds < 20) return;
                _lastDaemonStartUtc = DateTime.UtcNow;
            }
            var daemon = Path.Combine(_home!, "host", "cyd_daemon.py");
            if (!File.Exists(daemon)) { Log("autostart: host\\cyd_daemon.py not found"); return; }
            var psi = new ProcessStartInfo
            {
                FileName = _python!,
                Arguments = "\"" + daemon + "\" " + Environment.ExpandEnvironmentVariables(_daemonArgs),
                UseShellExecute = false,
                CreateNoWindow = true,
                WorkingDirectory = _home!
            };
            var p = Process.Start(psi);
            Log($"autostart: nothing on 127.0.0.1:{DaemonPort}; started cyd_daemon.py pid {p?.Id} {psi.Arguments}");
            for (var i = 0; i < 20 && !DaemonListening(); i++) Thread.Sleep(250);
        }
        catch (Exception e) { Log($"autostart failed: {e.GetType().Name}: {e.Message}"); }
    }

    static void Log(string text)
    {
        try
        {
            var dir = Path.Combine(Path.GetTempPath(), "cyd-pinball-cards");
            Directory.CreateDirectory(dir);
            var path = Path.Combine(dir, "plugin.log");
            lock (Gate)
            {
                var fi = new FileInfo(path);
                if (fi.Exists && fi.Length > 512 * 1024) File.Move(path, path + ".1", true);
                File.AppendAllText(path, DateTime.Now.ToString("HH:mm:ss.fff ") + text + Environment.NewLine, Utf8NoBom);
            }
        }
        catch { }
    }

    static int StartPython(string[] args)
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
        using var p = Process.Start(psi);
        return p?.Id ?? -1;
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
                    else if (k.Equals("AUTOSTART_DAEMON", StringComparison.OrdinalIgnoreCase))
                        _autostart = !(v == "0" || v.Equals("false", StringComparison.OrdinalIgnoreCase) || v.Equals("no", StringComparison.OrdinalIgnoreCase));
                    else if (k.Equals("DAEMON_ARGS", StringComparison.OrdinalIgnoreCase) && v.Length > 0) _daemonArgs = v;
                }
            }
            home ??= FindCydHome(dllDir);
            if (string.IsNullOrEmpty(home)) return;
            var script = Path.Combine(home, "frontends", "launchbox", "cyd_launchbox.py");
            if (!File.Exists(script)) return;
            _script = script;
            _home = home;
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
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), "projects", "cyd-pinball-cards")
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
