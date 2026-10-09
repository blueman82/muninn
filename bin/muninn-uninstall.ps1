# Removal uses the same validated interpreter without requiring git.
& "$PSScriptRoot/muninn.ps1" --muninn-installer-entry --uninstall @args
exit $LASTEXITCODE
