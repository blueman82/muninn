"""Child diagnostics retain original failures and refuse bad reports."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from muninn.obs_service import literal, powershell
from tests import (
    lifecycle_account_child,
    lifecycle_account_native,
    lifecycle_account_scripts,
)


class AccountDiagnosticTests(unittest.TestCase):
    """Check diagnostic source contracts and native numeric validation."""

    def test_original_exception_kind_and_parent_numeric_boundaries(
        self,
    ) -> None:
        child = lifecycle_account_child.child_script(Path("/synthetic"))
        parent = lifecycle_account_scripts.outer_script(Path("/synthetic"))
        self.assertIn("Get-ChildExceptionKind $nativeChildFailure", child)
        self.assertIn("'child_exception_kind'", parent)
        self.assertIn("$number -notin @(-1,0,1,2,3,4,5)", parent)
        self.assertIn("finally {exit 1}", child)
        self.assertNotIn(".GetType()", child)
        self.assertNotIn("Exception.Message", child)
        if sys.platform == "win32":
            source = (
                lifecycle_account_child.EXCEPTION_KIND
                + lifecycle_account_scripts._CHILD_DIAGNOSTICS
                + r"""
$ErrorActionPreference='Stop';$observed=@()
$manifest=[IO.Path]::Combine($PSHOME,'Modules','Microsoft.PowerShell.Management',
 'Microsoft.PowerShell.Management.psd1')
Microsoft.PowerShell.Core\Import-Module -Name $manifest -ErrorAction Stop
$utilityManifest=[IO.Path]::Combine($PSHOME,'Modules',
 'Microsoft.PowerShell.Utility','Microsoft.PowerShell.Utility.psd1')
Microsoft.PowerShell.Core\Import-Module -Name $utilityManifest `
 -ErrorAction Stop
$version=New-Object System.Version -ArgumentList 1,2
$actualJson=@{major=$version.Major}|ConvertTo-Json -Compress
if($actualJson -ne '{"major":1}'){throw 'utility_commands_failed'}
$childPath=[IO.Path]::Combine($PSHOME,'child.ps1')
$actualParent=Microsoft.PowerShell.Management\Split-Path -Parent $childPath
if($actualParent -ne $PSHOME){throw 'parent_failed'}
$missing=[IO.Path]::Combine([IO.Path]::GetTempPath(),
 [Guid]::NewGuid().ToString()+'.psd1')
$importRefused=$false
try{Microsoft.PowerShell.Core\Import-Module -Name $missing -ErrorAction Stop}
catch{$importRefused=$true}
if(!$importRefused){throw 'missing_module_not_refused'}
$types=@([UnauthorizedAccessException],[Security.SecurityException],
 [Runtime.InteropServices.COMException],[ComponentModel.Win32Exception],
 [Management.Automation.RuntimeException])
foreach($type in $types){$observed+=Get-ChildExceptionKind ($type::new())}
if(($observed -join ',') -ne '1,2,3,4,5'){throw 'classification_failed'}
if((Get-ChildExceptionKind $null) -ne -1 -or
   (Get-ChildExceptionKind ([Exception]::new())) -ne 0){throw 'other_failed'}
foreach($number in @(-1,0,1,2,3,4,5)){
 $report=@{};$value=[pscustomobject]@{child_exception_kind=$number}
 Copy-ChildCodes $value $report
 if($report.child_exception_kind -ne $number){throw 'projection_failed'}
}
foreach($invalid in @(-2,6,'1')){
 $report=@{sentinel=7};$refused=$false
 $value=[pscustomobject]@{child_module_readable=1;child_exception_kind=$invalid}
 try{Copy-ChildCodes $value $report}catch{$refused=$true}
 if(!$refused -or $report.Count -ne 1 -or $report.sentinel -ne 7){
  throw 'invalid_projection_or_partial_update'
 }
}
'{"classified":5,"valid":7,"refused":3}'
"""
            )
            result = subprocess.run(
                powershell(source), capture_output=True, text=True, timeout=30
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                json.loads(result.stdout),
                {"classified": 5, "valid": 7, "refused": 3},
            )

    def test_body_failure_survives_distinct_report_expression_lines(
        self,
    ) -> None:
        child = lifecycle_account_child.child_script(Path("/synthetic"))
        parent = lifecycle_account_scripts.outer_script(Path("/synthetic"))
        caught = child.index("$originalBodyError=$_")
        original = child.index(
            "$report.winerror=[int]$_.Exception.HResult", caught
        )
        captured = child.index("$childBodyHResult=[int]", original)
        self.assertLess(caught, original)
        self.assertLess(original, captured)
        self.assertIn(
            "$originalBodyError.Exception.GetBaseException().HResult", child
        )
        self.assertIn(
            "$originalBodyError.InvocationInfo.ScriptLineNumber", child
        )
        self.assertIn("$childBodyHResult=0;$childBodyLine=0", child)
        self.assertIn("catch {}", child[captured:])
        expected = (
            "$childReportPath=Join-Path $base 'child-result.json'",
            "$childReportJson=$report|ConvertTo-Json -Compress",
            "[IO.File]::WriteAllText($childReportPath,$childReportJson)",
        )
        positions = [child.index(line) for line in expected]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(len({child[:i].count("\n") for i in positions}), 3)
        self.assertEqual(child.count("$childStage=3"), 1)
        self.assertNotIn("$childStage=4", child)
        self.assertNotIn("$childStage=5", child)
        for key in ("child_body_hresult", "child_body_line"):
            self.assertIn(key, child)
            self.assertIn(key, parent)
        self.assertIn("$number -lt 0 -or $number -gt 4096", parent)
        self.assertIn("finally {exit 1}", child)
        self.assertNotIn("Exception.Message", child)
        self.assertNotIn("ScriptStackTrace", child)

    def test_native_body_capture_and_parent_bounds_keep_atomic_refusal(
        self,
    ) -> None:
        parent = lifecycle_account_scripts._CHILD_DIAGNOSTICS
        self.assertIn("'child_body_hresult'", parent)
        self.assertIn("'child_body_line'", parent)
        if sys.platform != "win32":
            return
        body = lifecycle_account_child.CHILD
        start = body.index(" $originalBodyError=$_")
        capture = body[start : body.index("} finally {", start)]
        source = (
            parent + "$ErrorActionPreference='Stop';$report=@{};"
            "$childBodyHResult=0;$childBodyLine=0;\n"
            "try{throw [UnauthorizedAccessException]::new()}catch{\n"
            + capture
            + r"""
}
if($childBodyHResult -ne -2147024891 -or $childBodyLine -le 0 -or
   !$report.ContainsKey('winerror')){throw 'original_body_missing'}
foreach($number in @(-2147483648,0,2147483647)){
 foreach($line in @(0,4096)){
  $report=@{};$value=[pscustomobject]@{
   child_body_hresult=$number;child_body_line=$line}
  Copy-ChildCodes $value $report
  if($report.child_body_hresult -ne $number -or
     $report.child_body_line -ne $line){throw 'body_projection_failed'}
 }
}
$invalid=@(@{child_body_hresult=2147483648},
 @{child_body_hresult=-2147483649},@{child_body_hresult='1'},
 @{child_body_hresult=$true},@{child_body_line=-1},
 @{child_body_line=4097},@{child_body_line='1'},@{child_body_line=$true})
foreach($fields in $invalid){
 $fields.child_module_readable=1;$report=@{sentinel=7};$refused=$false
 try{Copy-ChildCodes ([pscustomobject]$fields) $report}catch{$refused=$true}
 if(!$refused -or $report.Count -ne 1 -or $report.sentinel -ne 7){
  throw 'body_invalid_or_partial_update'
 }
}
'{"captured":1,"valid":6,"refused":8}'
"""
        )
        result = subprocess.run(
            powershell(source), capture_output=True, text=True, timeout=30
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {"captured": 1, "valid": 6, "refused": 8},
        )

    def test_normal_failed_report_keeps_body_codes_before_required_fields(
        self,
    ) -> None:
        child = lifecycle_account_child.child_script(Path("/synthetic"))
        parent = lifecycle_account_scripts.outer_script(Path("/synthetic"))
        for key, variable in (
            ("child_body_hresult", "$childBodyHResult"),
            ("child_body_line", "$childBodyLine"),
        ):
            assigned = child.index(f"$report.{key}={variable}")
            self.assertLess(child.index("$originalBodyError=$_"), assigned)
            self.assertLess(assigned, child.index("$childReportPath="))
        normal = parent[parent.index("$parsed=Get-Content") :]
        guard = (
            "if($null -ne $parsed.winerror){Read-ChildEvidence $base $report}"
        )
        self.assertIn(guard, normal)
        self.assertLess(normal.index(guard), normal.index("$privileges.Keys"))
        self.assertLess(normal.index(guard), normal.index("$parsed.$key"))
        if sys.platform != "win32":
            return
        start = normal.index("$report.phase=")
        end = normal.index("if($null -ne $parsed.winerror){$report.winerror=")
        branch = normal[start:end]
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            path = base / "child-result.json"
            value = {
                "phase": "account_child_start",
                "winerror": -2147024891,
                "child_body_hresult": -2147024891,
                "child_body_line": 108,
                "ordinary_child_admin": 0,
                "scheduler_child_admin": 1,
                "scheduler_exit": -1,
                "scheduler_instances": -1,
                "interactive_recognized": 0,
                "ordinary_child_session": 2,
            }
            path.write_text(json.dumps(value), encoding="utf-8")
            prefix = (
                lifecycle_account_scripts._CHILD_DIAGNOSTICS
                + "$ErrorActionPreference='Stop';$privileges=@{};"
                + f"$base={literal(str(base))};"
                + "$report=@{};$refused=$false;"
                + "$parsed=Read-ChildJson ([IO.Path]::Combine("
                + "$base,'child-result.json'));try{\n"
            )
            source = prefix + branch + r"""
}catch{$refused=$true}
if(!$refused -or $report.child_body_hresult -ne -2147024891 -or
   $report.child_body_line -ne 108 -or $report.child_report_seen -ne 1 -or
   $report.child_failure_hresult -ne -2147024891){
 throw 'original_report_lost_before_refusal'
}
'{"retained":2,"refused":1}'
"""
            result = subprocess.run(
                powershell(source), capture_output=True, text=True, timeout=30
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout), {"retained": 2, "refused": 1}
        )

    def test_native_invalid_normal_reports_do_not_partly_project_codes(
        self,
    ) -> None:
        child = lifecycle_account_child.child_script(Path("/synthetic"))
        self.assertIn("$report.child_body_hresult=$childBodyHResult", child)
        if sys.platform != "win32":
            return
        invalid = (
            ("child_body_hresult", 2147483648),
            ("child_body_hresult", -2147483649),
            ("child_body_hresult", "1"),
            ("child_body_hresult", True),
            ("child_body_line", -1),
            ("child_body_line", 4097),
            ("child_body_line", "1"),
            ("child_body_line", True),
        )
        payloads = ",".join(
            literal(
                json.dumps(
                    {
                        "phase": "account_child_start",
                        "child_module_readable": 1,
                        key: value,
                    }
                )
            )
            for key, value in invalid
        )
        with tempfile.TemporaryDirectory() as temporary:
            source = (
                lifecycle_account_scripts._CHILD_DIAGNOSTICS
                + f"$base={literal(temporary)};$invalid=@({payloads});"
                + r"""
$ErrorActionPreference='Stop'
foreach($json in $invalid){
 [IO.File]::WriteAllText(([IO.Path]::Combine($base,'child-result.json')),$json)
 $report=@{sentinel=7};Read-ChildEvidence $base $report
 if($report.Count -ne 2 -or $report.sentinel -ne 7 -or
    $report.child_report_seen -ne 0){throw 'invalid_report_partly_projected'}
}
'{"refused":8}'
"""
            )
            result = subprocess.run(
                powershell(source), capture_output=True, text=True, timeout=30
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"refused": 8})

    def test_principal_observations_precede_unchanged_guard(self) -> None:
        child = lifecycle_account_child.child_script(Path("/synthetic"))
        parent = lifecycle_account_scripts._CHILD_DIAGNOSTICS
        guard = (
            " if((Get-TaskSid $definition.Principal.UserId) -ne "
            "$identity.User.Value -or\n"
            "    $definition.Principal.LogonType -ne 3 -or\n"
            "    $definition.Principal.RunLevel -ne 0){throw "
            "'task_identity_mismatch'}"
        )
        start = child.index(" try {\n  $principalUser=")
        capture = child[start : child.index(guard)]
        self.assertLess(child.index("$definition.XmlText="), start)
        self.assertEqual(child.count(guard), 1)
        self.assertEqual(capture.count("catch {}"), 3)
        for key in (
            "task_principal_sid_equal",
            "task_principal_logon_type",
            "task_principal_run_level",
        ):
            self.assertIn(f"$report.{key}=", capture)
            self.assertIn(f"'{key}'", parent)
        self.assertNotIn("$definition.Principal.UserId=", capture)

    def test_native_principal_readback_and_atomic_numeric_bounds(self) -> None:
        child = lifecycle_account_child.child_script(Path("/synthetic"))
        start = child.index(" try {\n  $principalUser=")
        capture = child[start : child.index(" if((Get-TaskSid", start)]
        if sys.platform != "win32":
            return
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            (base / "task.xml").write_bytes(
                lifecycle_account_native.noop_definition(base)
            )
            source = (
                lifecycle_account_scripts._CHILD_DIAGNOSTICS
                + f"$base={literal(temporary)};"
                + r"""
$ErrorActionPreference='Stop';$identity=[Security.Principal.WindowsIdentity]::GetCurrent()
$service=New-Object -ComObject Schedule.Service;$service.Connect()
$definition=$service.NewTask(0)
$inputXml=[IO.File]::ReadAllText(([IO.Path]::Combine($base,'task.xml')))
$definition.XmlText=$inputXml.Replace('@SID@',$identity.User.Value)
$before=$definition.XmlText;$report=@{}
"""
                + capture
                + r"""
if($report.task_principal_sid_equal -ne
   [int]((Get-TaskSid $definition.Principal.UserId) -eq
   $identity.User.Value) -or
   $report.task_principal_logon_type -ne
   [int]$definition.Principal.LogonType -or
   $report.task_principal_run_level -ne [int]$definition.Principal.RunLevel -or
   $definition.XmlText -ne $before){throw 'principal_observation_changed'}
$report.phase='account_child_start';$report.winerror=-2146233087
[IO.File]::WriteAllText(([IO.Path]::Combine($base,'child-result.json')),
 ($report|ConvertTo-Json -Compress))
$projected=@{};Read-ChildEvidence $base $projected
if($projected.child_report_seen -ne 1){throw 'principal_report_missing'}
foreach($key in @('task_principal_sid_equal','task_principal_logon_type',
                 'task_principal_run_level')){
 if($projected[$key] -ne $report[$key]){throw 'principal_projection_changed'}
}
$valid=0;$refused=0
foreach($entry in @(@('task_principal_sid_equal',1),
                   @('task_principal_logon_type',6),
                   @('task_principal_run_level',1))){
 foreach($number in @(0,$entry[1])){
  $fields=@{};$fields[$entry[0]]=$number;$target=@{}
  Copy-ChildCodes ([pscustomobject]$fields) $target
  if($target[$entry[0]] -ne $number){throw 'principal_boundary_failed'}
  $valid++
 }
 foreach($number in @(-1,($entry[1]+1),'1',$true)){
  $fields=@{child_module_readable=1};$fields[$entry[0]]=$number
  $target=@{sentinel=7};$rejected=$false
  try{Copy-ChildCodes ([pscustomobject]$fields) $target}catch{$rejected=$true}
  if(!$rejected -or $target.Count -ne 1 -or $target.sentinel -ne 7){
   throw 'principal_invalid_or_partial_update'
  }
  $refused++
 }
}
$definition=$null;$report=@{}
"""
                + capture
                + r"""
if($report.Count -ne 0){throw 'unknown_principal_observation_exported'}
'{"observed":3,"valid":6,"refused":12,"unknown":0}'
"""
            )
            result = subprocess.run(
                powershell(source), capture_output=True, text=True, timeout=30
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {"observed": 3, "valid": 6, "refused": 12, "unknown": 0},
        )
