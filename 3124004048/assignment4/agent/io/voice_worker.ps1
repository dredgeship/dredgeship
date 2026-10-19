<#
⚠ 重要：本文件必须保存为 **UTF-8 with BOM**。
   PowerShell 5.1 读取 .ps1 时若没有 BOM，会按系统 ANSI 代码页（简体中文环境即 GBK）
   解析源码，导致脚本里的中文字面量变成乱码并使语法检查失败。仓库中的本文件已带 BOM；
   若你用编辑器重存导致 BOM 丢失，请执行：
       python -c "import pathlib;p=pathlib.Path('agent/io/voice_worker.ps1');p.write_text(p.read_text('utf-8'),'utf-8-sig')"

.SYNOPSIS
    Windows SAPI 语音 worker —— 常驻进程，用本机离线语音引擎完成中文识别与合成。

.DESCRIPTION
    由 agent/io/sapi.py 拉起，通过 stdin/stdout 与 Python 交换请求与结果。

    协议设计（关键）：
      管道里**只传 ASCII 的文件路径**，中文字符一律走 UTF-8 JSON 文件。
      原因是 PowerShell 5.1 的 stdin/stdout 编码行为在不同区域设置下并不一致，
      直接用管道传中文极易乱码。而 ConvertTo-Json 会把非 ASCII 自动转义为
      \uXXXX，因此 stdout 上永远只有 ASCII，Python 侧解析零风险。

      Python → worker ：一行纯 ASCII 的请求文件路径，或 "__QUIT__" 表示退出
      请求文件内容   ：{"id": "1", "action": "transcribe", "audio": "...", ...}
      worker → Python：一行 JSON（同 id 回传，便于按序配对）

    支持的动作：
      configure   —— 载入受限指令短语（Python 侧是唯一事实来源）
      probe       —— 探测本机可用的识别引擎与合成音色
      transcribe  —— 对 16kHz/16bit/单声道 WAV 做识别
      synthesize  —— 把文本合成为 WAV 文件

.NOTES
    stdout 是协议通道，任何调试输出都必须写 stderr（与 MCP Server 的约定一致）。
    脚本以 -NoProfile -NonInteractive 启动，避免用户 profile 污染输出。
    PowerShell 5.1 不支持三元运算符 ? :，本脚本一律使用 if/else。
#>

[CmdletBinding()]
param(
    [switch]$Once,
    [string]$Request = ""
)

$ErrorActionPreference = 'Stop'

Add-Type -AssemblyName System.Speech | Out-Null

# stdout / stderr 统一 UTF-8，避免中文乱码
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8

$script:CommandPhrases = @()
$script:Recognizer = $null
$script:Synthesizer = $null
$script:EngineName = ""

function Write-Log {
    param([string]$Message)
    [Console]::Error.WriteLine("[voice-worker] $Message")
}

function Write-Result {
    param([hashtable]$Payload)
    # ConvertTo-Json 会把中文转义成 \uXXXX，stdout 上只留 ASCII
    $json = $Payload | ConvertTo-Json -Compress -Depth 6
    [Console]::Out.WriteLine($json)
    [Console]::Out.Flush()
}

function Get-RequestPayload {
    param([string]$Line)

    # B64 形式：整条请求内联成 base64，用于路径含中文、或不想落临时文件的场景
    if ($Line.StartsWith("B64:")) {
        $json = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($Line.Substring(4)))
        return $json | ConvertFrom-Json
    }

    if (-not (Test-Path -LiteralPath $Line)) {
        throw "请求文件不存在: $Line"
    }
    $raw = [System.IO.File]::ReadAllText($Line, [System.Text.Encoding]::UTF8)
    return $raw | ConvertFrom-Json
}

# --------------------------------------------------------------------------- #
# 引擎探测
# --------------------------------------------------------------------------- #
function Get-SapiEngines {
    $items = @()
    foreach ($info in [System.Speech.Recognition.SpeechRecognitionEngine]::InstalledRecognizers()) {
        $items += [hashtable]@{
            name        = $info.Name
            culture     = $info.Culture.Name
            description = $info.Description
        }
    }
    return $items
}

function Get-SapiVoices {
    $items = @()
    $synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
    try {
        foreach ($voice in $synth.GetInstalledVoices()) {
            $items += [hashtable]@{
                name    = $voice.VoiceInfo.Name
                culture = $voice.VoiceInfo.Culture.Name
                gender  = $voice.VoiceInfo.Gender.ToString()
                enabled = [bool]$voice.Enabled
            }
        }
    }
    finally {
        $synth.Dispose()
    }
    return $items
}

function Get-PreferredRecognizer {
    param([string]$Preferred)

    $recognizers = [System.Speech.Recognition.SpeechRecognitionEngine]::InstalledRecognizers()
    if (-not $recognizers -or $recognizers.Count -eq 0) {
        return $null
    }
    if ($Preferred) {
        foreach ($info in $recognizers) {
            if ($info.Name -eq $Preferred) { return $info }
        }
    }
    # 优先中文引擎，其次任意可用引擎
    foreach ($info in $recognizers) {
        if ($info.Culture.Name -like "zh*") { return $info }
    }
    return $recognizers[0]
}

function Ensure-Recognizer {
    param([string]$Preferred, [string]$Mode)

    if ($null -ne $script:Recognizer) {
        return $script:Recognizer
    }

    $info = Get-PreferredRecognizer -Preferred $Preferred
    if ($null -eq $info) {
        throw "本机没有安装任何 Windows 语音识别引擎"
    }

    $engine = New-Object System.Speech.Recognition.SpeechRecognitionEngine($info)
    $engine.MaxAlternates = 1
    $script:EngineName = $info.Name

    $wantsDictation = ($Mode -ne "commands")
    $wantsCommands = (($Mode -eq "commands") -or ($Mode -eq "auto")) -and ($script:CommandPhrases.Count -gt 0)

    if ($wantsCommands) {
        $choices = New-Object System.Speech.Recognition.Choices
        foreach ($phrase in $script:CommandPhrases) {
            $choices.Add([string]$phrase) | Out-Null
        }
        $builder = New-Object System.Speech.Recognition.GrammarBuilder
        $builder.Append($choices) | Out-Null
        $grammar = New-Object System.Speech.Recognition.Grammar($builder)
        $grammar.Name = "commands"
        $engine.LoadGrammar($grammar) | Out-Null
    }
    if ($wantsDictation) {
        $dictation = New-Object System.Speech.Recognition.DictationGrammar
        $dictation.Name = "dictation"
        $engine.LoadGrammar($dictation) | Out-Null
    }

    $script:Recognizer = $engine
    return $engine
}

# --------------------------------------------------------------------------- #
# 动作实现
# --------------------------------------------------------------------------- #
function Invoke-Probe {
    param($Payload)
    # 用 @() 包裹，避免 PowerShell 在"只有 1 个元素"时把数组解包成标量，
    # 否则 Python 侧会因为字段类型在 1 个/多个之间摇摆而难以解析。
    return @{
        id      = [string]$Payload.id
        ok      = $true
        engines = @(Get-SapiEngines)
        voices  = @(Get-SapiVoices)
        phrases = $script:CommandPhrases.Count
        psver   = $PSVersionTable.PSVersion.ToString()
    }
}

function Invoke-Configure {
    param($Payload)
    $phrases = @()
    if ($null -ne $Payload.phrases) {
        foreach ($item in $Payload.phrases) {
            $text = ([string]$item).Trim()
            if ($text) { $phrases += $text }
        }
    }
    $script:CommandPhrases = $phrases
    # 指令集变化后需要重建识别引擎（语法在加载时固化）
    if ($null -ne $script:Recognizer) {
        $script:Recognizer.Dispose()
        $script:Recognizer = $null
    }
    return @{
        id      = [string]$Payload.id
        ok      = $true
        phrases = $phrases.Count
    }
}

function Invoke-Transcribe {
    param($Payload)

    $audioPath = [string]$Payload.audio
    if (-not (Test-Path -LiteralPath $audioPath)) {
        throw "音频文件不存在: $audioPath"
    }

    $mode = "auto"
    if ($Payload.mode) { $mode = [string]$Payload.mode }
    $engine = Ensure-Recognizer -Preferred ([string]$Payload.engine) -Mode $mode

    $engine.SetInputToWaveFile($audioPath)

    $texts = @()
    $bestConfidence = 0.0
    $matchedGrammar = ""
    # 单条短指令通常一次 Recognize 即可。注意：wave 输入被读完后再次调用
    # Recognize() 会抛出 "No audio input is supplied to this recognizer"，
    # 因此每一次调用都必须单独 try/catch，遇到 null 或异常立即停止。
    for ($i = 0; $i -lt 3; $i++) {
        $result = $null
        try {
            $result = $engine.Recognize()
        }
        catch {
            Write-Log "第 $($i + 1) 次 Recognize 结束: $($_.Exception.Message)"
            break
        }
        if ($null -eq $result) { break }
        $segment = ([string]$result.Text).Trim()
        if ($segment) { $texts += $segment }
        if ($result.Confidence -gt $bestConfidence) { $bestConfidence = $result.Confidence }
        if ($result.Grammar -and $result.Grammar.Name) { $matchedGrammar = $result.Grammar.Name }
    }

    $text = ($texts -join "").Trim()
    if (-not $text) {
        return @{
            id    = [string]$Payload.id
            ok    = $false
            error = "no-speech：没有识别到有效语音，请靠近麦克风再说一次"
            mode  = $mode
            engine = $script:EngineName
        }
    }
    return @{
        id          = [string]$Payload.id
        ok          = $true
        text        = $text
        confidence  = [Math]::Round($bestConfidence, 3)
        grammar     = $matchedGrammar
        mode        = $mode
        engine      = $script:EngineName
    }
}

function Invoke-Synthesize {
    param($Payload)

    $text = [string]$Payload.text
    if (-not $text.Trim()) {
        throw "合成文本不能为空"
    }
    $outPath = [string]$Payload.out
    if (-not $outPath) {
        throw "缺少输出路径 out"
    }
    $outDir = Split-Path -Parent $outPath
    if ($outDir -and -not (Test-Path -LiteralPath $outDir)) {
        New-Item -ItemType Directory -Path $outDir -Force | Out-Null
    }

    if ($null -eq $script:Synthesizer) {
        $script:Synthesizer = New-Object System.Speech.Synthesis.SpeechSynthesizer
    }
    $synth = $script:Synthesizer

    $voiceName = [string]$Payload.voice
    if ($voiceName) {
        try {
            $synth.SelectVoice($voiceName)
        }
        catch {
            Write-Log "指定音色不可用，回退默认音色: $voiceName"
        }
    }
    if ($null -ne $Payload.rate) { $synth.Rate = [int]$Payload.rate }
    if ($null -ne $Payload.volume) { $synth.Volume = [int]$Payload.volume }

    # $synth.Voice 在真正发声前可能为 $null，因此在 Speak 之后、SetOutputToNull 之前读取
    $usedVoice = ""
    $synth.SetOutputToWaveFile($outPath)
    try {
        $synth.Speak($text)
        if ($null -ne $synth.Voice) { $usedVoice = $synth.Voice.VoiceInfo.Name }
    }
    finally {
        $synth.SetOutputToNull()
    }

    $size = 0
    if (Test-Path -LiteralPath $outPath) {
        $size = (Get-Item -LiteralPath $outPath).Length
    }
    return @{
        id    = [string]$Payload.id
        ok    = $true
        path  = $outPath
        bytes = $size
        voice = $usedVoice
        rate  = $synth.Rate
    }
}

function Invoke-Action {
    param($Payload)
    $action = [string]$Payload.action
    switch ($action) {
        "probe"      { return Invoke-Probe -Payload $Payload }
        "configure"  { return Invoke-Configure -Payload $Payload }
        "transcribe" { return Invoke-Transcribe -Payload $Payload }
        "synthesize" { return Invoke-Synthesize -Payload $Payload }
        default      { throw "未知动作: $action" }
    }
}

function Invoke-RequestLine {
    param([string]$Line)

    $payload = $null
    try {
        $payload = Get-RequestPayload -Line $Line
        $result = Invoke-Action -Payload $payload
        Write-Result -Payload $result
    }
    catch {
        $id = ""
        if ($null -ne $payload) { $id = [string]$payload.id }
        Write-Log "处理失败: $($_.Exception.Message)"
        Write-Result -Payload @{
            id    = $id
            ok    = $false
            error = $_.Exception.Message
        }
    }
}

# --------------------------------------------------------------------------- #
# 入口
#
# 两种模式共用同一套"逐行读请求"的逻辑：
#   常驻模式（默认）—— Python 通过 stdin 持续投递请求
#   单次模式（-Once）—— 读一行处理完后退出，供脚本化自检使用
# --------------------------------------------------------------------------- #
Write-Log ("worker 启动，PowerShell " + $PSVersionTable.PSVersion.ToString())

$stdin = [Console]::In

if ($Request) {
    Invoke-RequestLine -Line $Request
    exit 0
}

while ($true) {
    $line = $stdin.ReadLine()
    if ($null -eq $line) { break }          # 父进程关闭了 stdin
    $line = $line.Trim()
    if (-not $line) { continue }
    if ($line -eq "__QUIT__") { break }
    Invoke-RequestLine -Line $line
    if ($Once) { break }
}

if ($null -ne $script:Recognizer) { $script:Recognizer.Dispose() }
if ($null -ne $script:Synthesizer) { $script:Synthesizer.Dispose() }
Write-Log "worker 退出"
exit 0
