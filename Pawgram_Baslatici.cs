using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Windows.Forms;

internal static class PawgramBaslatici
{
    [STAThread]
    private static int Main(string[] args)
    {
        try
        {
            string telRoot = FindTelRoot();
            string python = FindPythonRuntime();

            if (args.Any(arg => string.Equals(arg, "--check", StringComparison.OrdinalIgnoreCase)))
                return File.Exists(Path.Combine(telRoot, "run.py")) && File.Exists(python) ? 0 : 2;

            TryAutomaticUpdate(telRoot, python);
            StartPawgram(telRoot, python);
            return 0;
        }
        catch (Exception error)
        {
            MessageBox.Show(
                "Pawgram başlatılamadı:\n\n" + error.Message,
                "Pawgram Başlatıcı",
                MessageBoxButtons.OK,
                MessageBoxIcon.Error
            );
            return 1;
        }
    }

    private static void TryAutomaticUpdate(string telRoot, string python)
    {
        if (string.Equals(Environment.GetEnvironmentVariable("PAWGRAM_SKIP_UPDATE"), "1", StringComparison.Ordinal))
            return;

        string updater = Path.Combine(telRoot, "scripts", "update.ps1");
        if (!File.Exists(updater) || !Directory.Exists(Path.Combine(telRoot, ".git")))
            return;

        try
        {
            var updateInfo = new ProcessStartInfo
            {
                FileName = "powershell.exe",
                Arguments = "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File " + Quote(updater) +
                            " -ProjectRoot " + Quote(telRoot) + " -PythonPath " + Quote(python),
                WorkingDirectory = telRoot,
                UseShellExecute = false,
                CreateNoWindow = true,
                WindowStyle = ProcessWindowStyle.Hidden
            };

            using (Process updaterProcess = Process.Start(updateInfo))
            {
                if (updaterProcess != null)
                    updaterProcess.WaitForExit(120000);
            }
        }
        catch
        {
            // Güncelleme sorunu uygulamanın açılmasını engellemez.
        }
    }

    private static void StartPawgram(string telRoot, string python)
    {
        var startInfo = new ProcessStartInfo
        {
            FileName = python,
            Arguments = Quote(Path.Combine(telRoot, "run.py")),
            WorkingDirectory = telRoot,
            UseShellExecute = false,
            CreateNoWindow = true,
            WindowStyle = ProcessWindowStyle.Hidden
        };

        string packages = Path.Combine(telRoot, ".packages");
        string existingPythonPath = Environment.GetEnvironmentVariable("PYTHONPATH") ?? string.Empty;
        startInfo.EnvironmentVariables["PYTHONPATH"] = string.IsNullOrWhiteSpace(existingPythonPath)
            ? packages
            : packages + Path.PathSeparator + existingPythonPath;
        startInfo.EnvironmentVariables["PYTHONDONTWRITEBYTECODE"] = "1";
        Process.Start(startInfo);
    }

    private static string FindTelRoot()
    {
        string launcherDirectory = AppDomain.CurrentDomain.BaseDirectory;
        string[] candidates =
        {
            Path.Combine(launcherDirectory, "Tel"),
            launcherDirectory,
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory), "Tel")
        };

        string result = candidates.FirstOrDefault(path => File.Exists(Path.Combine(path, "run.py")));
        if (result == null)
            throw new DirectoryNotFoundException("Tel klasörü veya run.py bulunamadı. EXE'yi Tel klasörünün içinde tutun.");
        return Path.GetFullPath(result);
    }

    private static string FindPythonRuntime()
    {
        string configured = Environment.GetEnvironmentVariable("PAWGRAM_PYTHON");
        if (!string.IsNullOrWhiteSpace(configured) && File.Exists(configured))
            return configured;

        string runtimeRoot = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
            ".cache",
            "codex-runtimes"
        );

        if (Directory.Exists(runtimeRoot))
        {
            string runtime = Directory
                .EnumerateFiles(runtimeRoot, "python.exe", SearchOption.AllDirectories)
                .Where(path => path.IndexOf(Path.Combine("dependencies", "python"), StringComparison.OrdinalIgnoreCase) >= 0)
                .OrderByDescending(File.GetLastWriteTimeUtc)
                .FirstOrDefault();
            if (runtime != null)
                return runtime;
        }

        throw new FileNotFoundException(
            "Uyumlu Python çalışma ortamı bulunamadı. PAWGRAM_PYTHON ortam değişkenine Python yolunu tanımlayabilirsiniz."
        );
    }

    private static string Quote(string value)
    {
        return "\"" + value.Replace("\"", "\\\"") + "\"";
    }
}
