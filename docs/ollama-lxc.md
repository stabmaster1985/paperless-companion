# Ollama als LXC unter Proxmox (nur CPU)

Diese Anleitung richtet ein lokales Sprachmodell für den Companion ein. Ziel ist ein
Intel-Host **mit AVX2** (z.B. i5-8259U). CPUs ohne AVX/AVX2 wie der Pentium G4560T
sind zu langsam; dort `LLM_PROVIDER=none` setzen, dann arbeitet der Companion nur mit Regeln.

## 1. AVX2 prüfen (auf dem Proxmox-Host)

```bash
grep -o -m1 avx2 /proc/cpuinfo   # Ausgabe "avx2" = passt
```

## 2. Container anlegen

Proxmox-Oberfläche → *Create CT*:

| Einstellung | Wert |
|---|---|
| Template | Debian 12 |
| Unprivileged | ja |
| CPU-Kerne | 4 |
| RAM | 6144 MB (für 3B-Modelle; 7B-Modelle brauchen ca. 9 GB) |
| Swap | 512 MB |
| Disk | 16 GB |
| Netzwerk | statische IP, z.B. `192.168.1.30/24` |

Das RAM-Limit eines LXC wird nicht vorab reserviert. Belegt ist der Speicher nur,
solange ein Modell geladen ist (durch `OLLAMA_KEEP_ALIVE` maximal 2 Minuten nach
der letzten Anfrage).

> Nimmst du statt eines LXC eine **VM**, muss der CPU-Typ auf **`host`** stehen
> (*Hardware → Processor → Type*), sonst sieht die VM kein AVX2.

## 3. Ollama installieren

Im Container:

```bash
apt update && apt install -y curl
curl -fsSL https://ollama.com/install.sh | sh
```

Ollama im Netzwerk erreichbar machen und auf sparsamen Betrieb einstellen:

```bash
systemctl edit ollama
```

Folgendes eintragen:

```ini
[Service]
Environment="OLLAMA_HOST=0.0.0.0:11434"
Environment="OLLAMA_KEEP_ALIVE=2m"
Environment="OLLAMA_NUM_PARALLEL=1"
Environment="OLLAMA_MAX_LOADED_MODELS=1"
```

```bash
systemctl daemon-reload && systemctl restart ollama
```

## 4. Modell laden und testen

```bash
ollama pull qwen2.5:3b
time ollama run qwen2.5:3b "Antworte nur mit OK"
```

Die erste Antwort dauert länger, weil das Modell erst geladen wird.

## 5. Companion verbinden

In der `.env` des Companion:

```env
LLM_PROVIDER=ollama
OLLAMA_URL=http://192.168.1.30:11434
OLLAMA_MODEL=qwen2.5:3b
```

Unter **System** im Companion sollte danach „✔ bereit“ stehen.

## Sicherheit

Ollama hat **keine Authentifizierung**. Port 11434 nicht ins Internet freigeben.
Ideal ist eine Proxmox-Firewall-Regel, die nur die IP des Companion zulässt.

## Modellwahl

| Modell | RAM | Dauer/Dokument (i5-8259U, grob) | Hinweis |
|---|---|---|---|
| `qwen2.5:3b` | ca. 3–4 GB | 30–90 s | Standard, gutes Deutsch |
| `llama3.2:3b` | ca. 3–4 GB | 30–90 s | Alternative |
| `qwen2.5:7b` | ca. 6–8 GB | 2–4 min | genauer bei Vertragsklauseln |

Das Modell wird nur für Felder gefragt, die die Regeln nicht sicher gefunden haben,
und bekommt nur die relevanten Textstellen (`LLM_MAX_CHARS`). Bei normalem
Privatvolumen ist die CPU-Last deshalb gering.
