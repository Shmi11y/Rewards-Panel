using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Management;
using System.Text.RegularExpressions;

internal static class RestorePrevious
{
    private sealed class FileChange
    {
        public string Destination;
        public string Staged;
        public string Original;
    }

    private static readonly string[] UserPaths = {
        "data", "browser", "logs", "ui-profile", "updates", ".git"
    };

    private static string ResolveDestination(string root, string member)
    {
        const string Prefix = "RewardsPanel/";
        if (!member.StartsWith(Prefix, StringComparison.Ordinal))
            throw new InvalidDataException("Invalid backup path.");
        string[] parts = member.Substring(Prefix.Length).Split('/');
        if (UserPaths.Contains(parts[0], StringComparer.OrdinalIgnoreCase) ||
            (parts.Length > 1 && parts[0].Equals("app", StringComparison.OrdinalIgnoreCase) &&
                parts[1].Equals("logs", StringComparison.OrdinalIgnoreCase)))
            throw new InvalidDataException("A recovery point cannot contain user data.");
        string path = root;
        foreach (string part in parts)
        {
            if (part.Length == 0 || part.EndsWith(".") || part.EndsWith(" ") ||
                part.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0 ||
                part.StartsWith(".env", StringComparison.OrdinalIgnoreCase) ||
                Regex.IsMatch(part, @"^(con|prn|aux|nul|com[1-9]|lpt[1-9]|sessions|sessions\.db|accounts\.json|config\.json)(\.|$)",
                    RegexOptions.IgnoreCase))
                throw new InvalidDataException("Invalid backup path.");
            path = Path.Combine(path, part);
            if ((File.Exists(path) || Directory.Exists(path)) &&
                (File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0)
                throw new IOException("A file or directory is a symbolic link or junction.");
        }
        return path;
    }

    private static void CheckProcesses(string root)
    {
        int ownId = Process.GetCurrentProcess().Id;
        using (var search = new ManagementObjectSearcher("SELECT ProcessId, ExecutablePath, CommandLine FROM Win32_Process"))
        using (var processes = search.Get())
        {
            foreach (ManagementBaseObject process in processes)
            {
                using (process)
                {
                    if (Convert.ToInt32(process["ProcessId"]) == ownId)
                        continue;
                    string executable = Convert.ToString(process["ExecutablePath"]);
                    string command = Convert.ToString(process["CommandLine"]);
                    if (executable.StartsWith(root + "\\", StringComparison.OrdinalIgnoreCase) ||
                        new[] {"desktop.py", "scheduler.py", "run_history.py"}.Any(name =>
                            command.IndexOf(Path.Combine(root, name), StringComparison.OrdinalIgnoreCase) >= 0))
                        throw new IOException("Exit Rewards, its active tasks, and its scheduled runner before restoring.");
                }
            }
        }
    }

    private static void Restore(string root)
    {
        string folder = Path.Combine(root, "updates");
        string backup = Path.Combine(folder, "rollback.zip");
        if (!File.Exists(backup))
            throw new FileNotFoundException("No recovery point is available.");
        if ((File.GetAttributes(folder) & FileAttributes.ReparsePoint) != 0 ||
            (File.GetAttributes(backup) & FileAttributes.ReparsePoint) != 0)
            throw new IOException("The recovery directory or archive is a symbolic link or junction.");
        CheckProcesses(root);
        Console.WriteLine("Restore the previous version? Type YES to confirm.");
        if (Console.ReadLine() != "YES")
            return;

        string work = Path.Combine(folder, "restore-" + Guid.NewGuid().ToString("N"));
        var changes = new List<FileChange>();
        var moved = new List<FileChange>();
        var destinations = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        bool cleanup = true;
        try
        {
            Directory.CreateDirectory(work);
            using (ZipArchive zip = ZipFile.OpenRead(backup))
            {
                long bytes = 0;
                foreach (ZipArchiveEntry entry in zip.Entries)
                {
                    if (entry.FullName == "rewards-rollback.json")
                        continue;
                    string destination = ResolveDestination(root, entry.FullName);
                    if (destination.Equals(Path.Combine(root, "Restore-Previous.exe"), StringComparison.OrdinalIgnoreCase))
                        continue;
                    bytes += entry.Length;
                    if (!destinations.Add(destination) || bytes > 2L * 1024 * 1024 * 1024)
                        throw new InvalidDataException("Duplicate or oversized recovery archive.");
                    var change = new FileChange {
                        Destination = destination,
                        Staged = Path.Combine(work, "new-" + changes.Count),
                        Original = Path.Combine(work, "old-" + changes.Count)
                    };
                    entry.ExtractToFile(change.Staged);
                    if (new FileInfo(change.Staged).Length != entry.Length)
                        throw new InvalidDataException("Incomplete recovery file.");
                    changes.Add(change);
                }
            }
            if (!new[] {"VERSION", "Rewards.exe", "panel/server.py"}.All(
                    name => destinations.Contains(Path.Combine(root, name.Replace('/', Path.DirectorySeparatorChar)))))
                throw new InvalidDataException("The recovery point is missing required program files.");
            CheckProcesses(root);
            foreach (FileChange change in changes)
            {
                Directory.CreateDirectory(Path.GetDirectoryName(change.Destination));
                if (File.Exists(change.Destination))
                    File.Move(change.Destination, change.Original);
                moved.Add(change);
                File.Move(change.Staged, change.Destination);
            }
            string version = File.ReadAllText(Path.Combine(root, "VERSION")).Trim();
            try
            {
                File.AppendAllText(Path.Combine(folder, "upgrade-history.txt"),
                    DateTime.Now.ToString("s") + " Restored v" + version + Environment.NewLine);
            }
            catch (IOException)
            {
                Console.WriteLine("The version was restored. Upgrade history could not be written.");
            }
            Console.WriteLine("Restored v" + version + ". Open Rewards.exe to continue.");
        }
        catch
        {
            moved.Reverse();
            foreach (FileChange change in moved)
            {
                try
                {
                    if (!File.Exists(change.Staged))
                        File.Move(change.Destination, change.Staged);
                    if (File.Exists(change.Original))
                        File.Move(change.Original, change.Destination);
                }
                catch (IOException)
                {
                    cleanup = false;
                    Console.Error.WriteLine("Recovery files for " + change.Destination + " remain in " + work);
                }
            }
            throw;
        }
        finally
        {
            if (cleanup && Directory.Exists(work))
            {
                try
                {
                    foreach (string path in Directory.EnumerateFiles(work))
                        File.Delete(path);
                    Directory.Delete(work);
                }
                catch (IOException) { }
            }
        }
    }

    private static int Main()
    {
        int code = 0;
        try
        {
            Restore(AppDomain.CurrentDomain.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar));
        }
        catch (Exception error)
        {
            Console.Error.WriteLine(error.Message);
            code = 1;
        }
        if (!Console.IsInputRedirected)
        {
            Console.WriteLine("Press Enter to close.");
            Console.ReadLine();
        }
        return code;
    }
}
