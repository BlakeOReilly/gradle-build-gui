# Gradle Build GUI (Flask)

Simple GUI to point at a project root and run a Gradle build.
- Shows status icon for build file presence.
- Runs the build via `gradlew` if available, otherwise `gradle` on PATH.
- On success: app auto-stops.
- On failure: saves full build log and prepares a ChatGPT-ready prompt that includes the repo URL if detected from `.git/config`.

## Quick start

```bash
python -m venv .venv
# Windows
.venv\Scripts\python -m pip install -U pip
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python app.py

# macOS/Linux
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:5000

## Notes
- Build file detection supports `build.gradle` and `build.gradle.kts`.
- Repo URL is read from `.git/config` `[remote "origin"] url = ...` if present.
- Artifacts are saved under `logs/` and `prompts/`.
