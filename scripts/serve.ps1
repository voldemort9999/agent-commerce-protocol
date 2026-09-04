# Dono merchants apni-apni DIKHNE WALI window me chalao, phir readiness check.
#
#   powershell -ExecutionPolicy Bypass -File scripts\serve.ps1
#
# Do wajah se ye script hai:
#
#  1. Teen command yaad rakhna ek command yaad rakhne se hamesha bura hai, aur ye kaam
#     har test session aur har recording se pehle dobara karna padta hai.
#
#  2. WINDOW DIKHNI CHAHIYE. Is project me server teen baar dhokha de chuka hai: ek baar
#     chup-chaap mar gaya, ek baar LISTENING rehte hue jawab dena band kar diya, aur ek
#     baar file badalne par bhi purana code chalata raha. Teenon baar bahut saare test ek
#     saath laal hue aur pehla shak CODE par gaya - jabki wajah server thi. Ek dikhti hui
#     window me teenon ek nazar me pakde jate hain. Isi liye -WindowStyle Normal hai aur
#     koi background job nahi.
#
# Chalu hone ka saboot port ka khulna NAHI hai - bina key ek asli 401.
#
# NOTE: ye file JAAN-BOOJHKAR sirf ASCII me hai. PowerShell 5.1 .ps1 ko ANSI maan kar
# padhta hai jab tak BOM na ho, aur pehle version me ek em-dash ne poori string tod di
# thi - script apna hi source echo karne lagi thi. Wahi parivaar jo Claude Desktop ke
# config wale BOM ka tha. Yahan koi non-ASCII akshar mat likhna.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { Write-Error "venv nahi mila: $python"; exit 1 }

$merchants = @(
    @{ Name = "Northwind Apparel (FastAPI)"
       Port = 8001
       File = $python
       Args = @("-m", "uvicorn", "merchants.northwind.main:app", "--port", "8001") }
    @{ Name = "Voltline Electronics (Express)"
       Port = 8002
       File = "node"
       Args = @("merchants\voltline\main.mjs") }
    @{ Name = "Marigold Bazaar (Rust)"
       Port = 8003
       File = (Join-Path $root "merchants\marigold\target\release\marigold.exe")
       Args = @()
       Build = $true }
)

# Merchant C compiled hai, aur uske do nateeje hain jo baaki dono par nahi the:
#
#  1. Chalane se pehle BUILD karna padta hai. Ye yahin hota hai, taaki ek reviewer ko
#     "pehle cargo build chalao" yaad na rakhna pade.
#  2. Chalta hua server apni hi .exe par lock rakhta hai, isliye build KILL ke baad hota
#     hai. Ulta karne par cargo "Access is denied (os error 5)" par girta hai, aur wo
#     galti dikhne me "build toot gaya" jaisi lagti hai jabki wajah ek zinda process hai.
foreach ($m in $merchants) {
    if (-not $m.ContainsKey("Build")) { continue }
    $pids = @(netstat -ano | Select-String ":$($m.Port)\s.*LISTENING" | ForEach-Object { ($_ -split '\s+')[-1] } | Sort-Object -Unique)
    foreach ($processId in $pids) {
        Write-Host ("  port {0}: pehle se pid {1} chal raha hai, build se pehle maar rahe hain" -f $m.Port, $processId)
        & taskkill /F /PID $processId 2>$null | Out-Null
    }
    Write-Host ("  building {0} (release)..." -f $m.Name)
    Push-Location (Join-Path $root "merchants\marigold")
    # NOTE: yahan `2>&1` JAAN-BOOJHKAR nahi hai. PowerShell 5.1 ek native exe ki har
    # stderr line ko ErrorRecord me lapet deta hai (NativeCommandError) aur $? ko $false
    # kar deta hai - chahe exe ne 0 hi lauta ya ho. cargo apna progress stderr par likhta
    # hai, to `2>&1` ke saath ek KAAMYAAB build bhi is script ko `$ErrorActionPreference
    # = "Stop"` par gira deta hai. Sach sirf $LASTEXITCODE me hai.
    # Redirection `cmd` ke andar hoti hai, PowerShell ke andar NAHI. Wajah: 5.1 ek native
    # exe ki har stderr line ko ErrorRecord me lapet deta hai, aur cargo apna poora
    # progress stderr par likhta hai — yaani ek KAAMYAAB build bhi is script ko error
    # dikha deta hai (aur output pipe karne par exit code 255). OS-level redirection ke
    # baad PowerShell ko alag stderr stream dikhta hi nahi.
    $buildLog = & cmd /c "cargo build --release 2>&1"
    $buildFailed = ($LASTEXITCODE -ne 0)
    if ($buildFailed) { $buildLog | ForEach-Object { Write-Host "    $_" } }
    else { Write-Host ("    " + ($buildLog | Select-Object -Last 1)) }
    Pop-Location
    if ($buildFailed) { Write-Host "  BUILD FAIL - upar dekho"; exit 1 }
}

foreach ($m in $merchants) {
    # Purana process sach me maaro: PID dhoondho, phir taskkill. Poore project me yahi
    # tareeka likha hai, kyunki pkill Windows par exit code 0 deta hai aur process zinda
    # chhod deta hai.
    $pids = @(netstat -ano | Select-String ":$($m.Port)\s.*LISTENING" | ForEach-Object { ($_ -split '\s+')[-1] } | Sort-Object -Unique)
    foreach ($processId in $pids) {
        Write-Host ("  port {0}: pehle se pid {1} chal raha hai, maar rahe hain" -f $m.Port, $processId)
        & taskkill /F /PID $processId 2>$null | Out-Null
    }

    Write-Host ("starting {0} on port {1}" -f $m.Name, $m.Port)
    if ($m.Args.Count -gt 0) {
        Start-Process -FilePath $m.File -ArgumentList $m.Args -WorkingDirectory $root -WindowStyle Normal
    } else {
        Start-Process -FilePath $m.File -WorkingDirectory $root -WindowStyle Normal
    }
}

$allUp = $true
foreach ($m in $merchants) {
    $ok = $false
    foreach ($attempt in 1..40) {
        try {
            Invoke-WebRequest -Uri "http://127.0.0.1:$($m.Port)/agent/manifest" -TimeoutSec 3 -UseBasicParsing | Out-Null
        } catch {
            $response = $_.Exception.Response
            if ($response -and ([int]$response.StatusCode) -eq 401) { $ok = $true; break }
        }
        Start-Sleep -Milliseconds 400
    }
    if ($ok) {
        Write-Host ("  OK   port {0} ne bina key 401 diya" -f $m.Port)
    } else {
        Write-Host ("  MISS port {0} ne jawab nahi diya - us window me error dekho" -f $m.Port)
        $allUp = $false
    }
}

if (-not $allUp) { exit 1 }

Write-Host ""
& $python (Join-Path $root "scripts\readiness.py")
exit $LASTEXITCODE
