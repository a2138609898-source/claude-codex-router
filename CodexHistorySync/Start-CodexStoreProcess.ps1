param(
    [System.Diagnostics.ProcessStartInfo]$StartInfo,
    [string]$BootstrapPipeName,
    [int]$BootstrapParentPid,
    [switch]$VerifyOnly
)

# Preserve codex-sota's complete per-launch environment, but create the app
# inside its registered Store package. Never change user/machine environment,
# profile files, the installed app, update policy, or breakaway policy.
$ErrorActionPreference = 'Stop'
if (-not ('CodexStoreLaunch.Native' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Text;
using System.IO;
using System.ComponentModel;
using System.Collections;
using System.Collections.Generic;
using System.Runtime.InteropServices;
namespace CodexStoreLaunch {
 public static class Native {
  [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
  struct SI {
   public int cb; public string reserved, desktop, title;
   public int x, y, width, height, xchars, ychars, fill, flags;
   public short show, reserved2; public IntPtr bytes, input, output, error;
  }
  [StructLayout(LayoutKind.Sequential)]
  struct PI { public IntPtr process, thread; public uint pid, tid; }
  [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
  static extern bool CreateProcess(string app, StringBuilder command,
   IntPtr pa, IntPtr ta, bool inherit, uint flags, IntPtr environment,
   string directory, ref SI startup, out PI process);
  [DllImport("kernel32.dll", CharSet=CharSet.Unicode)]
  static extern int GetPackageFamilyName(IntPtr process, ref uint size, StringBuilder name);
  [DllImport("kernel32.dll", SetLastError=true)]
  static extern IntPtr OpenProcess(uint access, bool inherit, uint pid);
  [DllImport("kernel32.dll", SetLastError=true)]
  static extern uint ResumeThread(IntPtr thread);
  [DllImport("kernel32.dll")] static extern bool TerminateProcess(IntPtr process, uint code);
  [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
  [DllImport("kernel32.dll", SetLastError=true)]
  static extern bool GetNamedPipeClientProcessId(IntPtr pipe, out uint pid);
  [DllImport("kernel32.dll", SetLastError=true)]
  static extern bool GetNamedPipeServerProcessId(IntPtr pipe, out uint pid);
  [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
  static extern IntPtr CreateFile(string path, uint access, uint share,
   IntPtr security, uint disposition, uint flags, IntPtr template);
  [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
  static extern uint GetFinalPathNameByHandle(IntPtr handle, StringBuilder path, uint size, uint flags);
  static string Family(IntPtr process) {
   uint size=512; var name=new StringBuilder(512);
   int result=GetPackageFamilyName(process, ref size, name);
   if(result!=0) throw new Win32Exception(result, "Windows package identity is unavailable");
   return name.ToString();
  }
  public static string ProcessFamily(uint pid) {
   IntPtr handle=OpenProcess(0x1000, false, pid);
   if(handle==IntPtr.Zero) throw new Win32Exception(Marshal.GetLastWin32Error());
   try { return Family(handle); } finally { CloseHandle(handle); }
  }
  public static uint PipePeer(IntPtr pipe, bool server) {
   uint pid; bool ok=server ? GetNamedPipeClientProcessId(pipe,out pid) : GetNamedPipeServerProcessId(pipe,out pid);
   if(!ok) throw new Win32Exception(Marshal.GetLastWin32Error());
   return pid;
  }
  public static string PhysicalDirectory(string path) {
   IntPtr handle=CreateFile(path,0,7,IntPtr.Zero,3,0x02000000,IntPtr.Zero);
   if(handle==new IntPtr(-1)) return "missing:"+Marshal.GetLastWin32Error();
   try { var name=new StringBuilder(32768); uint n=GetFinalPathNameByHandle(handle,name,32768,0);
    if(n==0 || n>=32768) throw new Win32Exception(Marshal.GetLastWin32Error());
    return name.ToString();
   } finally { CloseHandle(handle); }
  }
  public static uint Launch(System.Diagnostics.ProcessStartInfo info, string family, bool verifyOnly) {
   var keys=new List<string>(); foreach(string key in info.EnvironmentVariables.Keys) keys.Add(key);
   keys.Sort(StringComparer.OrdinalIgnoreCase);
   var block=new StringBuilder();
   foreach(string key in keys) {
    string value=info.EnvironmentVariables[key] ?? "";
    if(key.IndexOf('\0')>=0 || value.IndexOf('\0')>=0) throw new ArgumentException("Invalid environment entry");
    block.Append(key).Append('=').Append(value).Append('\0');
   }
   block.Append('\0'); IntPtr env=Marshal.StringToHGlobalUni(block.ToString());
   PI process=new PI(); bool created=false, resumed=false;
   try {
    var startup=new SI(); startup.cb=Marshal.SizeOf(startup);
    var command=new StringBuilder("\""+info.FileName+"\""+(String.IsNullOrEmpty(info.Arguments)?"":" "+info.Arguments));
    uint flags=0x00000404; // CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT
    if(info.CreateNoWindow) flags|=0x08000000;
    if(!CreateProcess(info.FileName,command,IntPtr.Zero,IntPtr.Zero,false,flags,env,info.WorkingDirectory,ref startup,out process))
     throw new Win32Exception(Marshal.GetLastWin32Error());
    created=true;
    if(!String.Equals(Family(process.process),family,StringComparison.OrdinalIgnoreCase))
     throw new InvalidOperationException("Refusing to start the app without its expected Store identity");
    if(!verifyOnly) {
     if(ResumeThread(process.thread)==UInt32.MaxValue) throw new Win32Exception(Marshal.GetLastWin32Error());
     resumed=true;
    }
    return process.pid;
   } finally {
    // Only our own, never-resumed diagnostic/failed child can be terminated.
    if(created && !resumed) TerminateProcess(process.process,0);
    if(process.thread!=IntPtr.Zero) CloseHandle(process.thread);
    if(process.process!=IntPtr.Zero) CloseHandle(process.process);
    Marshal.FreeHGlobal(env);
   }
  }
 }
}
'@
}

function Get-StoreLaunchEnvironmentHash($Environment) {
    $keys = [string[]]@($Environment.Keys)
    [Array]::Sort($keys, [StringComparer]::OrdinalIgnoreCase)
    $text = [Text.StringBuilder]::new()
    foreach ($key in $keys) { [void]$text.Append($key).Append([char]0).Append([string]$Environment[$key]).Append([char]0) }
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($text.ToString())))).Replace('-', '').ToLowerInvariant() }
    finally { $sha.Dispose() }
}

function Get-StoreLaunchPackage([string]$Executable) {
    $full = [IO.Path]::GetFullPath($Executable)
    $packages = @(Get-AppxPackage -Name 'OpenAI.Codex' -ErrorAction Stop | Where-Object {
        $root = [IO.Path]::GetFullPath($_.InstallLocation)
        $full -ieq (Join-Path $root 'app\ChatGPT.exe') -or $full -ieq (Join-Path $root 'app\Codex.exe')
    })
    if ($packages.Count -ne 1) { throw 'The selected app executable does not match one registered OpenAI.Codex Store package.' }
    return $packages[0]
}

function Read-StoreLaunchMessage([IO.StreamReader]$Reader) {
    $task = $Reader.ReadLineAsync()
    if (-not $task.Wait(10000)) { throw 'The Store-identity launch handshake timed out; do not start a duplicate app.' }
    $line = $task.Result
    if (-not $line) { throw 'The Store-identity launch connection closed unexpectedly.' }
    return ($line | ConvertFrom-Json -ErrorAction Stop)
}

if ($BootstrapPipeName) {
    if ($BootstrapPipeName -notmatch '^codex-store-launch-[a-f0-9]{32}$' -or $BootstrapParentPid -le 0) { throw 'Invalid bootstrap binding.' }
    $client = [IO.Pipes.NamedPipeClientStream]::new('.', $BootstrapPipeName, [IO.Pipes.PipeDirection]::InOut)
    $writer = $reader = $null
    try {
        $client.Connect(10000)
        if ([CodexStoreLaunch.Native]::PipePeer($client.SafePipeHandle.DangerousGetHandle(), $false) -ne $BootstrapParentPid) { throw 'Unexpected launch-pipe server.' }
        $reader = [IO.StreamReader]::new($client)
        $writer = [IO.StreamWriter]::new($client)
        $writer.AutoFlush = $true
        try {
            $request = Read-StoreLaunchMessage $reader
            $package = Get-StoreLaunchPackage ([string]$request.executable)
            if ([CodexStoreLaunch.Native]::ProcessFamily($PID) -ine $package.PackageFamilyName) { throw 'The bootstrap has no matching Store identity.' }
            $info = [Diagnostics.ProcessStartInfo]::new()
            $info.FileName = [string]$request.executable
            $info.Arguments = [string]$request.arguments
            $info.WorkingDirectory = [string]$request.directory
            $info.UseShellExecute = $false
            $info.CreateNoWindow = [bool]$request.create_no_window
            $info.EnvironmentVariables.Clear()
            foreach ($entry in $request.environment.PSObject.Properties) { $info.EnvironmentVariables[$entry.Name] = [string]$entry.Value }
            $digest = Get-StoreLaunchEnvironmentHash $info.EnvironmentVariables
            if ($digest -cne [string]$request.environment_sha256) { throw 'The isolated launch environment changed during transfer.' }
            $bindings = @{}
            foreach ($directory in @($request.verify_directories)) { $bindings[[string]$directory] = [CodexStoreLaunch.Native]::PhysicalDirectory([string]$directory) }
            $appPid = [CodexStoreLaunch.Native]::Launch($info, $package.PackageFamilyName, [bool]$request.verify_only)
            $writer.WriteLine(([ordered]@{
                ok = $true; pid = $appPid; package_family = $package.PackageFamilyName
                environment_sha256 = $digest; verified_only = [bool]$request.verify_only
                directory_bindings = $bindings
            } | ConvertTo-Json -Compress -Depth 5))
        }
        catch { $writer.WriteLine((@{ok=$false; error=$_.Exception.Message} | ConvertTo-Json -Compress)); throw }
    }
    finally {
        if ($writer) { $writer.Dispose() }
        if ($reader) { $reader.Dispose() }
        $client.Dispose()
    }
    return
}

if (-not $StartInfo -or $StartInfo.UseShellExecute -or $StartInfo.RedirectStandardOutput -or $StartInfo.RedirectStandardError -or $StartInfo.RedirectStandardInput) {
    throw 'A non-shell, non-redirected isolated app ProcessStartInfo is required.'
}
$package = Get-StoreLaunchPackage $StartInfo.FileName
$environment = @{}
foreach ($key in $StartInfo.EnvironmentVariables.Keys) { $environment[[string]$key] = [string]$StartInfo.EnvironmentVariables[$key] }
$digest = Get-StoreLaunchEnvironmentHash $environment
$pipeName = 'codex-store-launch-' + [guid]::NewGuid().ToString('N')
$security = [IO.Pipes.PipeSecurity]::new()
$security.SetAccessRuleProtection($true, $false)
$userSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
$security.AddAccessRule([IO.Pipes.PipeAccessRule]::new($userSid, [IO.Pipes.PipeAccessRights]::FullControl, [Security.AccessControl.AccessControlType]::Allow))
$pipe = [IO.Pipes.NamedPipeServerStream]::new($pipeName, [IO.Pipes.PipeDirection]::InOut, 1,
    [IO.Pipes.PipeTransmissionMode]::Byte, [IO.Pipes.PipeOptions]::Asynchronous, 65536, 65536, $security)
$writer = $reader = $null
try {
    $wait = $pipe.BeginWaitForConnection($null, $null)
    $powerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $arguments = '-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $PSCommandPath + '" -BootstrapPipeName ' + $pipeName + ' -BootstrapParentPid ' + $PID
    # Do not use PreventBreakaway: other command/tool descendants retain their
    # normal Windows behavior. Only the in-package app inherits Store identity.
    Invoke-CommandInDesktopPackage -PackageFamilyName $package.PackageFamilyName -AppId 'App' -Command $powerShell -Args $arguments -ErrorAction Stop | Out-Null
    if (-not $wait.AsyncWaitHandle.WaitOne(10000)) { throw 'The Store-identity bootstrap did not connect; no fallback app was started.' }
    $pipe.EndWaitForConnection($wait)
    $peerPid = [CodexStoreLaunch.Native]::PipePeer($pipe.SafePipeHandle.DangerousGetHandle(), $true)
    $peer = [Diagnostics.Process]::GetProcessById([int]$peerPid)
    if ($peer.MainModule.FileName -ine $powerShell -or [CodexStoreLaunch.Native]::ProcessFamily($peerPid) -ine $package.PackageFamilyName) {
        throw 'Unexpected Store-identity bootstrap process.'
    }
    $reader = [IO.StreamReader]::new($pipe)
    $writer = [IO.StreamWriter]::new($pipe)
    $writer.AutoFlush = $true
    $directories = @()
    if ($VerifyOnly) {
        $directories = @($environment['CODEX_HOME'], (Join-Path $env:APPDATA 'Codex'), (Join-Path $env:APPDATA 'ChatGPT')) | Where-Object { $_ }
    }
    $writer.WriteLine(([ordered]@{
        executable = $StartInfo.FileName; arguments = $StartInfo.Arguments
        directory = $StartInfo.WorkingDirectory; create_no_window = $StartInfo.CreateNoWindow
        environment = $environment; environment_sha256 = $digest
        verify_only = [bool]$VerifyOnly; verify_directories = @($directories)
    } | ConvertTo-Json -Compress -Depth 5))
    $reply = Read-StoreLaunchMessage $reader
    if (-not $reply.ok) { throw ('Store-identity launch failed: ' + $reply.error) }
    if ($reply.package_family -ine $package.PackageFamilyName -or $reply.environment_sha256 -cne $digest -or [bool]$reply.verified_only -ne [bool]$VerifyOnly) { throw 'Invalid Store-identity launch receipt; do not start a duplicate app.' }
    if ($VerifyOnly) { return $reply }
    return [Diagnostics.Process]::GetProcessById([int]$reply.pid)
}
finally {
    if ($writer) { $writer.Dispose() }
    if ($reader) { $reader.Dispose() }
    $pipe.Dispose()
}
