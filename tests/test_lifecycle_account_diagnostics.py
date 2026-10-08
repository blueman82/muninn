"""Child diagnostics retain original failures and refuse bad reports."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from muninn.obs_service import powershell
from tests import lifecycle_account_child, lifecycle_account_scripts


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
