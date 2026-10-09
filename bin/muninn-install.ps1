# Reuse the runtime bootstrap's interpreter, selection and handle checks.
& "$PSScriptRoot/muninn.ps1" --muninn-installer-entry @args
exit $LASTEXITCODE
