# Jarvis : double clap → réveil du bureau (Windows)

Au double clap, Jarvis ouvre **Claude, Gmail, Google Agenda et WhatsApp (application)**
et dit une phrase de bienvenue avec une voix ElevenLabs.

## Installation

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

## Clé ElevenLabs

Copie `.env.example` en `.env` et remplis :

- `ELEVENLABS_API_KEY` : elevenlabs.io → ton profil → *API Keys* → créer une clé.
- `ELEVENLABS_VOICE_ID` : *Voices* → choisis une voix → copie son *Voice ID*.

Plan gratuit : les voix de la Voice Library sont refusées par l'API (erreur 402).
Utilise une voix native (« Default voices »).

## Lancer

```powershell
.venv\Scripts\python jarvis.py
```

Autorise le micro si Windows le demande. Arrêt : **Ctrl+C**.
La séquence ne se déclenche qu'une fois par lancement.

## Mode debug

```powershell
$env:JARVIS_DEBUG=1; .venv\Scripts\python jarvis.py
```

(ou `JARVIS_DEBUG=1` dans `.env`). À chaque pic : niveau mesuré, seuil, niveau 100 ms
après, et verdict clap / voix.

## Réglages (haut de `jarvis.py`)

| Constante | Effet |
| --- | --- |
| `MIN_RMS` (0.28) | Plancher de volume. Baisse-le si tes claps ne sont pas détectés. |
| `CLAP_DECAY_RATIO` (0.35) | Le son doit retomber sous 35 % du pic après 100 ms. Un clap oui, une voix non. |
| `MIN/MAX_DOUBLE_GAP_S` | Écart entre les deux claps : 0.12 s à 0.35 s. |
| `OPEN_URLS` | Sites ouverts. |

Variables `.env` optionnelles : `JARVIS_INPUT_DEVICE` (numéro ou nom du micro),
`JARVIS_SAMPLE_RATE` (par défaut la fréquence native du micro, 44100 ou 48000).
