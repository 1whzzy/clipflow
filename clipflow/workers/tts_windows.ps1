# Stand-in TTS for the pilot: Windows' built-in speech engine (System.Speech), offline.
# Reads UTF-8 text from -TextFile and writes a 16-bit mono WAV to -OutFile.
# Used by clipflow/workers/pilot.py (WindowsTTS) until a real TTS service is chosen.
param(
    [Parameter(Mandatory = $true)][string]$TextFile,
    [Parameter(Mandatory = $true)][string]$OutFile,
    [string]$Voice = "Microsoft Huihui Desktop",
    [int]$Rate = 1
)
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Speech
$text = [System.IO.File]::ReadAllText($TextFile, [System.Text.Encoding]::UTF8)
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $synth.SelectVoice($Voice)
    $synth.Rate = $Rate
    $format = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(24000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
    $synth.SetOutputToWaveFile($OutFile, $format)
    $synth.Speak($text)
}
finally {
    $synth.Dispose()
}
