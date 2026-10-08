"""Fixed private ordinary-account PowerShell scripts and failure stages."""

from __future__ import annotations

from pathlib import Path

from muninn.obs_service import literal
from tests.lifecycle_account_desktop import PROCESS_SOURCE
from tests.lifecycle_account_desktop import SOURCE as DESKTOP_SOURCE
from tests.lifecycle_account_privileges import SOURCE

OWNED = r"""
function Test-Owned($task,$base){
 [xml]$wanted=[IO.File]::ReadAllText((Join-Path $base 'task.xml'))
 [xml]$actual=$task.Xml
 $ns=[Xml.XmlNamespaceManager]::new($wanted.NameTable)
 $ns.AddNamespace('t','http://schemas.microsoft.com/windows/2004/02/mit/task')
 foreach($path in @('Actions','Principals','Triggers')){
  if($actual.SelectNodes('/t:Task/t:'+$path+'/*',$ns).Count -ne 1){
   return $false
  }
 }
 foreach($path in @(
  'Principals/Principal/UserId','Principals/Principal/LogonType',
  'Principals/Principal/RunLevel','Actions/Exec/Command','Actions/Exec/Arguments',
  'Settings/AllowHardTerminate','Settings/ExecutionTimeLimit',
  'Settings/MultipleInstancesPolicy','Settings/StopIfGoingOnBatteries',
  'Settings/DisallowStartIfOnBatteries','Settings/StartWhenAvailable',
  'Settings/Enabled','Settings/RestartOnFailure/Interval',
  'Settings/RestartOnFailure/Count','Triggers/LogonTrigger/Enabled',
  'Triggers/LogonTrigger/UserId')){
  $xpath='/t:Task/t:'+($path.Replace('/','/t:'))
  $a=$actual.SelectSingleNode($xpath,$ns)
  $w=$wanted.SelectSingleNode($xpath,$ns)
  if($null -eq $a -or $null -eq $w -or $a.InnerText -ne $w.InnerText){
   return $false
  }
 }
 return $actual.SelectSingleNode('/t:Task/t:Actions',$ns).Context -eq
        $wanted.SelectSingleNode('/t:Task/t:Actions',$ns).Context
}
"""

CHILD = r"""
$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue'
$base=Split-Path -Parent $MyInvocation.MyCommand.Path
$report=@{phase='account_child_start';ordinary_child_admin=1;
 scheduler_child_admin=1;scheduler_exit=-1;scheduler_instances=-1;
 interactive_recognized=0;account_retained=1}
$name=Split-Path -Leaf $base;$task=$null;$service=$null
try {
 $identity=[Security.Principal.WindowsIdentity]::GetCurrent()
 $principal=[Security.Principal.WindowsPrincipal]::new($identity)
 $admin=[Security.Principal.WindowsBuiltInRole]::Administrator
 $report.ordinary_child_admin=[int]$principal.IsInRole($admin)
 $report.ordinary_child_session=[Diagnostics.Process]::GetCurrentProcess().SessionId
 if($report.ordinary_child_admin -ne 0){throw 'ordinary_identity_required'}
 $service=New-Object -ComObject Schedule.Service;$service.Connect()
 $folder=$service.GetFolder('\')
 $definition=$service.NewTask(0)
 $definition.XmlText=[IO.File]::ReadAllText((Join-Path $base 'task.xml'))
 if($definition.Principal.UserId -ne $identity.User.Value -or
    $definition.Principal.LogonType -ne 3 -or
    $definition.Principal.RunLevel -ne 0){throw 'task_identity_mismatch'}
 $report.phase='account_task_create'
 $task=$folder.RegisterTaskDefinition($name,$definition,2,$null,$null,3,$null)
 [IO.File]::WriteAllText((Join-Path $base 'registered.xml'),$task.Xml)
 if(!(Test-Owned $task $base)){throw 'task_definition_mismatch'}
 $report.phase='account_task_run';$running=$task.Run($null)
 $report.phase='account_task_wait'
 $deadline=[DateTime]::UtcNow.AddSeconds(30)
 $result=Join-Path $base 'task-result.json'
 while(!(Test-Path -LiteralPath $result) -or
       $task.GetInstances(0).Count -ne 0){
  if([DateTime]::UtcNow -gt $deadline){throw 'task_execution_unproven'}
  Start-Sleep -Milliseconds 100
 }
 $scheduled=Get-Content -LiteralPath $result -Raw|ConvertFrom-Json
 if($scheduled.sid -ne $identity.User.Value -or $scheduled.pid -eq $PID -or
    $scheduled.created -le 0){
  throw 'scheduled_identity_mismatch'
 }
 $report.scheduler_child_admin=[int]$scheduled.admin
 $report.scheduler_child_session=[int]$scheduled.session
 if($report.scheduler_child_session -ne $report.ordinary_child_session){
  throw 'scheduled_session_mismatch'
 }
 $report.scheduler_exit=[int]$task.LastTaskResult
 $report.scheduler_instances=[int]$task.GetInstances(0).Count
 if($report.scheduler_child_admin -ne 0 -or $report.scheduler_exit -ne 0){
  throw 'scheduled_ordinary_execution_required'
 }
 $report.interactive_recognized=1
} catch {
 $report.winerror=[int]$_.Exception.HResult
} finally {
 if($null -ne $task){
  try {$report.scheduler_instances=[int]$task.GetInstances(0).Count}
  catch {$report.scheduler_instances=-1}
 }
 [IO.File]::WriteAllText((Join-Path $base 'child-result.json'),
  ($report|ConvertTo-Json -Compress))
}
"""

_OUTER = r"""
$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue'
$base=@BASE@;$name='mn-'+[Guid]::NewGuid().ToString('N').Substring(0,15)
$report=@{phase='account_create';account_retained=0}
$created=$false;$quiescent=$true;$token=[IntPtr]::Zero
$process=New-Object MuninnCiLogon+Process
$environment=[IntPtr]::Zero
try {
 $caller=[Security.Principal.WindowsIdentity]::GetCurrent()
 try {$privileges=[MuninnCiPrivileges]::Read($caller.Token);
      $callerSid=$caller.User.Value}
 finally {$caller.Dispose()}
 foreach($key in $privileges.Keys){$report[$key]=$privileges[$key]}
 if($privileges['caller_impersonate_present'] -ne 1){
  throw 'already_held_impersonation_required'
 }
 $report.caller_session=[Diagnostics.Process]::GetCurrentProcess().SessionId
 $password=[Guid]::NewGuid().ToString('N')+'aA7!'
 $secure=ConvertTo-SecureString -String $password -AsPlainText -Force
 $user=New-LocalUser -Name $name -Password $secure -AccountNeverExpires
 $created=$true;$report.account_retained=1
 Add-LocalGroupMember -SID 'S-1-5-32-545' -Member $user
 $acl=New-Object Security.AccessControl.DirectorySecurity
 $acl.SetAccessRuleProtection($true,$false);$acl.SetOwner($user.SID)
 foreach($sid in @($user.SID.Value,'S-1-5-18','S-1-5-32-544')){
  $rule=[Security.AccessControl.FileSystemAccessRule]::new(
   [Security.Principal.SecurityIdentifier]::new($sid),'FullControl',
   'ContainerInherit,ObjectInherit','None','Allow')
  $acl.AddAccessRule($rule)
 }
 Set-Acl -LiteralPath $base -AclObject $acl
 foreach($file in Get-ChildItem -LiteralPath $base -File){
  $fileAcl=New-Object Security.AccessControl.FileSecurity
  $fileAcl.SetAccessRuleProtection($true,$false);$fileAcl.SetOwner($user.SID)
  foreach($sid in @($user.SID.Value,'S-1-5-18','S-1-5-32-544')){
   $fileAcl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
    [Security.Principal.SecurityIdentifier]::new($sid),'FullControl','Allow'))
  }
  Set-Acl -LiteralPath $file.FullName -AclObject $fileAcl
 }
 $xml=Join-Path $base 'task.xml'
 [IO.File]::WriteAllText($xml,
  [IO.File]::ReadAllText($xml).Replace('@SID@',$user.SID.Value),
  [Text.Encoding]::Unicode)
 $report.phase='account_logon'
 if(![MuninnCiLogon]::LogonUserW($name,'.',$password,2,0,[ref]$token)){
  $code=[Runtime.InteropServices.Marshal]::GetLastWin32Error()
  throw [ComponentModel.Win32Exception]::new($code)
 }
 $password=$null;$secure.Dispose()
 $startup=New-Object MuninnCiLogon+Startup
 $startup.cb=[Runtime.InteropServices.Marshal]::SizeOf($startup)
 $quiescent=$false
 try {$startup.desktop=[MuninnCiDesktop]::Prepare($user.SID.Value,$callerSid)}
 finally {
  $report.desktop_restored=[int][MuninnCiDesktop]::Restored
  $report.station_restore_ok=[int][MuninnCiDesktop]::StationRestoreOk
  $report.station_restore_error=[int][MuninnCiDesktop]::StationRestoreError
  $report.desktop_restore_ok=[int][MuninnCiDesktop]::DesktopRestoreOk
  $report.desktop_restore_error=[int][MuninnCiDesktop]::DesktopRestoreError
  $report.station_identity_ok=[int][MuninnCiDesktop]::StationIdentityOk
  $report.desktop_identity_ok=[int][MuninnCiDesktop]::DesktopIdentityOk
  $report.desktop_identity_before=[int][MuninnCiDesktop]::DesktopIdentityBefore
  $report.desktop_restore_called=[int][MuninnCiDesktop]::DesktopRestoreCalled
 }
 if(![MuninnCiDesktop]::Restored){throw 'desktop_restore_unproven'}
 $quiescent=$true;$report.private_desktop_created=1
 $report.token_session=[MuninnCiDesktop]::Session($token)
 $relative='System32\WindowsPowerShell\v1.0\powershell.exe'
 $exe=Join-Path $env:SystemRoot $relative
 $childArgs=' -NoProfile -NonInteractive -ExecutionPolicy Bypass -File '
 $command=[Text.StringBuilder]::new('"'+$exe+'"'+$childArgs+'"'+
  (Join-Path $base 'child.ps1')+'"')
 if($command.Length -gt 1024){throw 'native_command_too_large'}
 $zero=[char]0
 $minimal='SystemRoot='+$env:SystemRoot+$zero+'TEMP='+$base+$zero+
  'TMP='+$base+$zero+'WINDIR='+$env:SystemRoot+$zero+$zero
 $environment=[Runtime.InteropServices.Marshal]::StringToHGlobalUni($minimal)
 $report.phase='account_child_start';$report.child_stage=1
 $launched=[MuninnCiLogon]::CreateProcessWithTokenW($token,0,$exe,$command,
  0x08000400,$environment,$base,[ref]$startup,[ref]$process)
 if(!$launched){
  $code=[Runtime.InteropServices.Marshal]::GetLastWin32Error()
  $report.child_launch_ok=[int]$launched
  throw [ComponentModel.Win32Exception]::new($code)
 }
 $report.child_launch_ok=[int]$launched
 $quiescent=$false
 $report.child_stage=2
 $wait=[MuninnCiLogon]::WaitForSingleObject($process.process,60000)
 if($wait -eq -1){
  $code=[Runtime.InteropServices.Marshal]::GetLastWin32Error()
  $report.child_wait_result=$wait
  throw [ComponentModel.Win32Exception]::new($code)
 }
 $report.child_wait_result=$wait
 if($wait -ne 0){throw 'ordinary_child_exit_unproven'}
 $report.child_stage=3;$exitCode=0
 $queried=[MuninnCiLogon]::GetExitCodeProcess($process.process,[ref]$exitCode)
 if(!$queried){
  $code=[Runtime.InteropServices.Marshal]::GetLastWin32Error()
  $report.child_exit_query_ok=[int]$queried
  throw [ComponentModel.Win32Exception]::new($code)
 }
 $report.child_exit_query_ok=[int]$queried
 $report.child_exit_code=$exitCode
 if($exitCode -ne 0){throw 'ordinary_child_exit_failed'}
 $report.child_stage=4
 $child=Join-Path $base 'child-result.json'
 $parsed=Get-Content -LiteralPath $child -Raw|ConvertFrom-Json
 $report.phase=[string]$parsed.phase
 foreach($key in $privileges.Keys){$report[$key]=$privileges[$key]}
 foreach($key in @('ordinary_child_admin','scheduler_child_admin',
  'scheduler_exit','scheduler_instances','interactive_recognized',
  'ordinary_child_session','scheduler_child_session')){
  if($null -eq $parsed.$key){throw 'ordinary_child_report_invalid'}
  $report[$key]=[int]$parsed.$key
 }
 if($null -ne $parsed.winerror){$report.winerror=[int]$parsed.winerror}
 if($report.ordinary_child_session -ne $report.caller_session){
  throw 'ordinary_session_mismatch'
 }
 $report.child_stage=5
 $service=New-Object -ComObject Schedule.Service;$service.Connect()
 $folder=$service.GetFolder('\')
 try {$owned=$folder.GetTask((Split-Path -Leaf $base))}
 catch {
  if($_.Exception.HResult -ne -2147024894){throw}
  $owned=$null
 }
 if($null -ne $owned){
  if(!(Test-Owned $owned $base)){throw 'retained_task_definition_mismatch'}
  $owned.Enabled=$false
  if($owned.GetInstances(0).Count -ne 0){throw 'retained_task_still_running'}
  $folder.DeleteTask((Split-Path -Leaf $base),0)
 }
 if($report.scheduler_instances -lt 0){throw 'scheduler_instances_unknown'}
 $quiescent=$report.scheduler_instances -eq 0
} catch {
 $nativeFailure=$_.Exception.GetBaseException()
 $report.winerror=[int]$nativeFailure.HResult
 if($nativeFailure -is [ComponentModel.Win32Exception]){
  $report.winerror=[int]$nativeFailure.NativeErrorCode
 }
} finally {
 $password=$null
 if($process.thread -ne [IntPtr]::Zero){
  [void][MuninnCiLogon]::CloseHandle($process.thread)
 }
 if($process.process -ne [IntPtr]::Zero){
  [void][MuninnCiLogon]::CloseHandle($process.process)
 }
 if($environment -ne [IntPtr]::Zero){
  [Runtime.InteropServices.Marshal]::FreeHGlobal($environment)
 }
 if($quiescent -and [MuninnCiDesktop]::Restored){
  try {[MuninnCiDesktop]::CloseOwned()}
  catch {$quiescent=$false;$report.winerror=$_.Exception.HResult}
 }
 if($created -and $quiescent){
  try {
   $remaining=Get-LocalUser -Name $name
   if($remaining.SID.Value -ne $user.SID.Value){
    throw 'account_identity_mismatch'
   }
   Remove-LocalUser -SID $user.SID;$report.account_retained=0
  } catch {$report.account_retained=1;$report.winerror=$_.Exception.HResult}
 }
 if($token -ne [IntPtr]::Zero){[void][MuninnCiLogon]::CloseHandle($token)}
}
$report|ConvertTo-Json -Compress
"""


def outer_script(parent: Path) -> str:
    """Build fixed native calls without embedding runtime credentials."""
    body = (
        _OUTER.replace("@BASE@", literal(str(parent)))
        .replace("$process=New-Object", "$outerStage=2;$process=New-Object")
        .replace(
            "$environment=[IntPtr]::Zero",
            "$outerStage=3;$environment=[IntPtr]::Zero",
        )
        .replace(
            "} finally {\n $password=$null",
            "} finally {\n $bodyStage=$outerStage;$outerStage=4;"
            "$password=$null",
        )
    ).replace(
        "if($token -ne [IntPtr]::Zero){"
        "[void][MuninnCiLogon]::CloseHandle($token)}",
        "if($token -ne [IntPtr]::Zero){"
        "[void][MuninnCiLogon]::CloseHandle($token)}\n $outerStage=$bodyStage",
    )
    return (
        "$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue';"
        "$report=@{phase='account_create';account_retained=0};"
        "$outerStage=1;try {\nAdd-Type -TypeDefinition @'\n"
        + PROCESS_SOURCE
        + SOURCE
        + DESKTOP_SOURCE
        + "\n'@\n"
        + OWNED
        + body
        + "\n} catch {"
        "$probeFailure=$_.Exception.GetBaseException();"
        "$report.outer_stage=$outerStage;"
        "$report.winerror=[int]$probeFailure.HResult;"
        "if($probeFailure -is [ComponentModel.Win32Exception]){"
        "$report.winerror=[int]$probeFailure.NativeErrorCode};"
        "$report|ConvertTo-Json -Compress;exit 1}"
    )
