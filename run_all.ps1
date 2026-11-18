[CmdletBinding()]
param(
    [switch]$Quick
)

# Reproducible CPU-only offline evaluation for Windows PowerShell 5.1.
# Never regenerates benchmark inputs or expected labels.
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$PythonExe = if ($env:PYTHON) { $env:PYTHON } else { 'python' }
New-Item -ItemType Directory -Force -Path 'reports' | Out-Null

function Invoke-Python {
    param([Parameter(Mandatory = $true)][string[]]$PythonArgs)
    & $PythonExe @PythonArgs
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        throw "Python command failed (exit $code): $PythonExe $($PythonArgs -join ' ')"
    }
}

Write-Host '== Frozen input verification =='
Invoke-Python -PythonArgs @('-m', 'eval.frozen_inputs', '--check')

Write-Host '== Unit and regression tests =='
Invoke-Python -PythonArgs @('-m', 'unittest', 'discover', '-s', 'tests')

Write-Host '== Baseline and final evaluations =='
Invoke-Python -PythonArgs @('-m', 'eval.run_eval', '--impl', 'v0', '--out', 'reports/v0_all.json')
Invoke-Python -PythonArgs @('-m', 'eval.run_eval', '--impl', 'v0', '--split', 'holdout', '--out', 'reports/v0_holdout.json')
Invoke-Python -PythonArgs @('-m', 'eval.run_eval', '--impl', 'final', '--out', 'reports/final_all.json')
Invoke-Python -PythonArgs @('-m', 'eval.run_eval', '--impl', 'final', '--split', 'holdout', '--out', 'reports/final_holdout.json')

Write-Host '== Controlled ablations =='
foreach ($ablation in @('random_sampling', 'no_approvals', 'no_pruning')) {
    Invoke-Python -PythonArgs @('-m', 'eval.run_eval', '--impl', 'final', '--ablation', $ablation, '--out', "reports/ablation_$ablation.json")
}

Write-Host '== Differential fuzzing =='
foreach ($seed in @(1, 2)) {
    $fuzzOutput = & $PythonExe -m eval.fuzz_differential --n 400 --seed $seed 2>&1
    $fuzzCode = $LASTEXITCODE
    $fuzzOutput | Set-Content -LiteralPath "reports/fuzz_seed$seed.txt" -Encoding UTF8
    $fuzzOutput | Write-Output
    if ($fuzzCode -ne 0) { throw "Differential fuzzer failed for seed $seed (exit $fuzzCode)." }
}

Write-Host '== Adversarial probe =='
$probeOutput = & $PythonExe -m eval.adversarial_probe 2>&1
$probeCode = $LASTEXITCODE
$probeOutput | Set-Content -LiteralPath 'reports/adversarial_v1.2.0.txt' -Encoding UTF8
$probeOutput | Write-Output
if ($probeCode -ne 0) { throw "Adversarial probe failed (exit $probeCode)." }
$mismatches = @($probeOutput | Select-String -Pattern 'MISMATCH')
if ($mismatches.Count -gt 0) { throw "Adversarial probe reported $($mismatches.Count) mismatch(es)." }
$probeLines = @($probeOutput | Select-String -Pattern '->')
$probeCount = $probeLines.Count
Write-Host "Adversarial probe: 0 mismatches ($probeCount probes)"

Write-Host '== Second policy family =='
Invoke-Python -PythonArgs @('-m', 'eval.second_family', '--out', 'reports/second_family.json')

if (-not $Quick) {
    Write-Host '== Scaling experiment =='
    Invoke-Python -PythonArgs @('-m', 'eval.scaling_probe', '--max-k', '8', '--out', 'reports/scaling.json')
    Write-Host '== Deliberate mutation testing =='
    Invoke-Python -PythonArgs @('-m', 'eval.mutation_test', '--out', 'reports/mutation_test.json')
}

Write-Host 'ALL CHECKS COMPLETE' -ForegroundColor Green
