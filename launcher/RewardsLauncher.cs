using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;

[assembly: System.Reflection.AssemblyTitle("Rewards")]
[assembly: System.Reflection.AssemblyDescription("Rewards Panel launcher")]
[assembly: System.Reflection.AssemblyProduct("Rewards Panel")]

internal sealed class RewardsLauncher : Form
{
    private readonly string root;
    private readonly Label status = new Label();
    private readonly object logLock = new object();

    internal RewardsLauncher(string root)
    {
        this.root = root;
        Text = "启动 Rewards 面板";
        Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath);
        StartPosition = FormStartPosition.CenterScreen;
        FormBorderStyle = FormBorderStyle.FixedDialog;
        ControlBox = false;
        MaximizeBox = false;
        MinimizeBox = false;
        ClientSize = new Size(384, 140);
        BackColor = Color.FromArgb(249, 250, 252);
        Font = new Font("Microsoft YaHei UI", 9);

        var heading = new Label
        {
            Text = "Rewards 面板",
            Font = new Font(Font, FontStyle.Bold),
            AutoSize = true,
            Location = new Point(24, 20)
        };
        status.Text = "正在检查启动环境…";
        status.Location = new Point(24, 52);
        status.Size = new Size(336, 36);

        var progress = new ProgressBar
        {
            Location = new Point(24, 104),
            Size = new Size(336, 8),
            Style = ProgressBarStyle.Marquee,
            MarqueeAnimationSpeed = 28
        };
        Controls.Add(heading);
        Controls.Add(status);
        Controls.Add(progress);
        Shown += StartPreparation;
    }

    private async void StartPreparation(object sender, EventArgs args)
    {
        var logPath = Path.Combine(root, "logs", "launcher.log");
        try
        {
            await Task.Run(() => Prepare(logPath));
        }
        catch (Exception error)
        {
            MessageBox.Show(this,
                error.Message + "\n\n启动日志位于 logs\\launcher.log",
                "Rewards 启动失败", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
        finally
        {
            Close();
        }
    }

    private void Prepare(string logPath)
    {
        var python = Path.Combine(root, "runtime", "python", "python.exe");
        var script = Path.Combine(root, "portable_launcher.py");
        if (!File.Exists(python) || !File.Exists(script))
            throw new InvalidOperationException("启动文件不完整，请完整解压 ZIP 后再双击 Rewards.exe。");

        Directory.CreateDirectory(Path.GetDirectoryName(logPath));
        using (var log = new StreamWriter(logPath, false, Encoding.UTF8))
        using (var process = new Process())
        {
            process.StartInfo = new ProcessStartInfo(python, "-X utf8 -u \"" + script + "\"")
            {
                WorkingDirectory = root,
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                StandardOutputEncoding = Encoding.UTF8,
                StandardErrorEncoding = Encoding.UTF8
            };
            process.OutputDataReceived += (sender, args) => HandleLine(log, args.Data);
            process.ErrorDataReceived += (sender, args) => HandleLine(log, args.Data);
            process.Start();
            process.BeginOutputReadLine();
            process.BeginErrorReadLine();
            process.WaitForExit();
            if (process.ExitCode != 0)
                throw new InvalidOperationException("环境配置失败。请检查网络和解压目录的写入权限，再运行 Rewards.exe。");
        }
    }

    private void HandleLine(StreamWriter log, string line)
    {
        if (line == null)
            return;
        lock (logLock)
        {
            log.WriteLine(line);
            log.Flush();
        }
        const string prefix = "STATUS ";
        if (line.StartsWith(prefix, StringComparison.Ordinal))
            BeginInvoke((Action)(() => status.Text = line.Substring(prefix.Length)));
    }

    [STAThread]
    private static void Main()
    {
        var root = AppDomain.CurrentDomain.BaseDirectory;
        var canonicalPath = Path.GetFullPath(root).TrimEnd(Path.DirectorySeparatorChar).ToUpperInvariant();
        string mutexName;
        using (var hash = SHA256.Create())
            mutexName = @"Local\RewardsLauncher-" + BitConverter.ToString(hash.ComputeHash(Encoding.UTF8.GetBytes(canonicalPath)));

        bool first;
        using (var guard = new Mutex(true, mutexName, out first))
        {
            if (!first)
                return;
            try
            {
                Application.EnableVisualStyles();
                Application.SetCompatibleTextRenderingDefault(false);
                Application.Run(new RewardsLauncher(root));
            }
            finally
            {
                guard.ReleaseMutex();
            }
        }
    }
}
