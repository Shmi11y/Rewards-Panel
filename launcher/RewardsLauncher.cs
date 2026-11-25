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
    private const int WebViewSetupFailed = 10;
    private const int ChromiumSetupFailed = 11;
    private readonly string root;
    private readonly string logPath;
    private readonly Label status = new Label();
    private readonly Label percentage = new Label();
    private readonly ProgressBar progress = new ProgressBar();
    private readonly FlowLayoutPanel actions = new FlowLayoutPanel();
    private readonly Button openLog;
    private readonly Button useChromium;
    private readonly object logLock = new object();
    private bool retryWithChromium;

    internal RewardsLauncher(string root)
    {
        this.root = root;
        logPath = Path.Combine(root, "logs", "launcher.log");
        Text = "启动 Rewards 面板";
        Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath);
        StartPosition = FormStartPosition.CenterScreen;
        FormBorderStyle = FormBorderStyle.FixedDialog;
        ControlBox = false;
        MaximizeBox = false;
        MinimizeBox = false;
        ClientSize = new Size(460, 174);
        BackColor = Color.FromArgb(249, 250, 252);
        Font = new Font("Microsoft YaHei UI", 9);

        var heading = new Label
        {
            Text = "Rewards 面板",
            Font = new Font(Font, FontStyle.Bold),
            AutoSize = true,
            Location = new Point(24, 20)
        };
        status.Location = new Point(24, 52);
        status.Size = new Size(412, 42);

        progress.AccessibleName = "下载进度";
        progress.Location = new Point(24, 106);
        progress.Size = new Size(354, 10);
        progress.MarqueeAnimationSpeed = 28;
        percentage.Location = new Point(382, 99);
        percentage.Size = new Size(54, 24);
        percentage.TextAlign = ContentAlignment.MiddleRight;

        actions.Location = new Point(24, 132);
        actions.Size = new Size(412, 32);
        actions.WrapContents = false;
        actions.Visible = false;
        AddAction("重试", 74, (sender, args) => StartPreparation(retryWithChromium));
        openLog = AddAction("打开日志", 86, (sender, args) => OpenLog());
        useChromium = AddAction("用 Chromium 启动", 132, (sender, args) => StartPreparation(true));
        AddAction("关闭", 64, (sender, args) => Close());

        Controls.AddRange(new Control[] { heading, status, progress, percentage, actions });
        ShowStatus("正在检查启动环境…");
        Shown += (sender, args) => StartPreparation(false);
    }

    private Button AddAction(string label, int width, EventHandler click)
    {
        var button = new Button
        {
            Text = label,
            Size = new Size(width, 28),
            Margin = new Padding(0, 0, 8, 0)
        };
        button.Click += click;
        actions.Controls.Add(button);
        return button;
    }

    private async void StartPreparation(bool useBrowserWindow)
    {
        retryWithChromium = useBrowserWindow;
        actions.Visible = false;
        ShowStatus("正在检查启动环境…");

        try
        {
            int result = await Task.Run(() => Prepare(useBrowserWindow));
            if (result == 0)
            {
                Close();
                return;
            }

            if (result == WebViewSetupFailed)
                ShowFailure("WebView2 配置失败。可以重试、查看日志，或用 Chromium 打开面板。", true);
            else if (result == ChromiumSetupFailed)
                ShowFailure("Chromium 配置失败。请检查网络，或打开日志查看详情。", false);
            else
                ShowFailure("启动失败。请打开日志查看详情。", false);
        }
        catch (Exception error)
        {
            ShowFailure(error.Message, false);
        }
    }

    private void ShowStatus(string message)
    {
        status.Text = message;
        progress.Visible = true;
        progress.Style = ProgressBarStyle.Marquee;
        percentage.Text = "";
    }

    private void ShowFailure(string message, bool canUseChromium)
    {
        status.Text = message;
        progress.Visible = false;
        percentage.Text = "";
        useChromium.Visible = canUseChromium;
        openLog.Enabled = File.Exists(logPath);
        actions.Visible = true;
        actions.Controls[0].Focus();
    }

    private void OpenLog()
    {
        try
        {
            Process.Start(new ProcessStartInfo(logPath) { UseShellExecute = true });
        }
        catch (Exception)
        {
            MessageBox.Show(this, "无法打开日志文件。\n\n" + logPath,
                "Rewards 启动日志", MessageBoxButtons.OK, MessageBoxIcon.Warning);
        }
    }

    private int Prepare(bool useBrowserWindow)
    {
        var python = Path.Combine(root, "runtime", "python", "python.exe");
        var script = Path.Combine(root, "portable_launcher.py");
        if (!File.Exists(python) || !File.Exists(script))
            throw new InvalidOperationException("启动文件不完整，请完整解压 ZIP 后重试。");

        Directory.CreateDirectory(Path.GetDirectoryName(logPath));
        using (var log = new StreamWriter(logPath, true, new UTF8Encoding(false)))
        using (var process = new Process())
        {
            log.WriteLine("\n[{0:yyyy-MM-dd HH:mm:ss}] Rewards 启动", DateTime.Now);
            log.Flush();

            var arguments = "-X utf8 -u \"" + script + "\" --interactive";
            if (useBrowserWindow)
                arguments += " --use-chromium";
            process.StartInfo = new ProcessStartInfo(python, arguments)
            {
                WorkingDirectory = root,
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                StandardOutputEncoding = Encoding.UTF8,
                StandardErrorEncoding = Encoding.UTF8
            };
            process.OutputDataReceived += (sender, args) => HandleLine(log, args.Data, true);
            process.ErrorDataReceived += (sender, args) => HandleLine(log, args.Data, false);
            process.Start();
            process.BeginOutputReadLine();
            process.BeginErrorReadLine();
            process.WaitForExit();
            return process.ExitCode;
        }
    }

    private void HandleLine(StreamWriter log, string line, bool containsStatus)
    {
        if (line == null)
            return;
        lock (logLock)
        {
            log.WriteLine(line);
            log.Flush();
        }

        if (containsStatus && (line.StartsWith("STATUS ", StringComparison.Ordinal) ||
                line.StartsWith("DOWNLOAD ", StringComparison.Ordinal) ||
                line.StartsWith("PROGRESS ", StringComparison.Ordinal)))
            BeginInvoke((Action)(() => UpdateProgress(line)));
    }

    private void UpdateProgress(string line)
    {
        int delimiter = line.IndexOf(' ');
        string value = line.Substring(delimiter + 1);
        switch (line.Substring(0, delimiter))
        {
            case "STATUS":
                ShowStatus(value);
                break;
            case "DOWNLOAD":
                status.Text = "正在下载 " + value;
                progress.Style = ProgressBarStyle.Continuous;
                progress.Value = 0;
                percentage.Text = "0%";
                break;
            case "PROGRESS":
                int amount;
                if (progress.Style == ProgressBarStyle.Continuous && int.TryParse(value, out amount) &&
                    amount >= 0 && amount <= progress.Maximum)
                {
                    progress.Value = amount;
                    percentage.Text = amount + "%";
                }
                break;
        }
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
