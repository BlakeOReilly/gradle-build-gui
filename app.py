import os
import json
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_from_directory

app = Flask(__name__)

PROJECT_STATE = {
    "last_checked_path": None,
    "last_check_result": None,
    "last_run_output_file": None,
    "last_prompt_file": None,
    "repo_url": None,
}

def find_build_file(root: Path):
    if not root.exists() or not root.is_dir():
        return None
    for name in ("build.gradle", "build.gradle.kts"):
        p = root / name
        if p.is_file():
            return p
    return None

def detect_repo_url(root: Path):
    git_config = root / ".git" / "config"
    if not git_config.exists():
        return None
    try:
        url = None
        current_remote = None
        with git_config.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if line.startswith("[remote "):
                    current_remote = line.split('"')[1] if '"' in line else None
                elif line.startswith("url") and "=" in line:
                    _, val = line.split("=", 1)
                    val = val.strip()
                    if current_remote == "origin":
                        url = val
                        break
        return url
    except Exception:
        return None

def shutdown_flask():
    func = request.environ.get("werkzeug.server.shutdown")
    if func:
        func()

@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")

@app.route("/api/check_dir", methods=["POST"])
def api_check_dir():
    try:
        data = request.get_json(force=True, silent=True) or {}
        path_str = data.get("path", "").strip()
        PROJECT_STATE["last_checked_path"] = path_str
        status = "invalid"
        found_file = None
        repo_url = None
        if path_str:
            root = Path(path_str)
            build_file = find_build_file(root)
            if build_file:
                status = "ok"
                found_file = str(build_file)
                repo_url = detect_repo_url(root)
            else:
                status = "missing"
        PROJECT_STATE["last_check_result"] = status
        PROJECT_STATE["repo_url"] = repo_url
        return jsonify({"status": status, "build_file": found_file, "repo_url": repo_url})
    except Exception as e:
        return jsonify({"ok": False, "error": f"/api/check_dir failed: {e.__class__.__name__}: {e}"}), 500

def run_gradle_build(build_root: Path):
    is_windows = os.name == "nt"
    gradlew = "gradlew.bat" if is_windows else "gradlew"
    gradlew_path = build_root / gradlew

    if gradlew_path.exists():
        cmd = ["cmd.exe", "/c", str(gradlew_path), "build", "--no-daemon", "--stacktrace", "--info"] if is_windows \
              else [str(gradlew_path), "build", "--no-daemon", "--stacktrace", "--info"]
    else:
        cmd = ["cmd.exe", "/c", "gradle", "build", "--no-daemon", "--stacktrace", "--info"] if is_windows \
              else ["gradle", "build", "--no-daemon", "--stacktrace", "--info"]

    env = os.environ.copy()
    env["ORG_GRADLE_COLOR"] = "false"
    env["GRADLE_OPTS"] = env.get("GRADLE_OPTS", "") + " -Dorg.gradle.console=plain"

    proc = subprocess.Popen(
        cmd, cwd=str(build_root),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, shell=False, bufsize=1, universal_newlines=True,
        encoding="utf-8", errors="replace", env=env,
    )
    out_lines = []
    for line in proc.stdout:
        out_lines.append(line)
    proc.wait()
    return proc.returncode, "".join(out_lines)

def extract_error_summary(full_output: str, max_tail_lines: int = 400):
    lines = full_output.splitlines()
    tail = lines[-max_tail_lines:] if len(lines) > max_tail_lines else lines
    markers = (" FAILED", "error:", "FAILURE:", "Exception", "Caused by:")
    picked = [ln for ln in tail if any(m in ln for m in markers)]
    return "\n".join(picked if picked else tail)

@app.route("/api/run", methods=["POST"])
def api_run():
    try:
        data = request.get_json(force=True, silent=True) or {}
        path_str = data.get("path", "").strip()
        if not path_str:
            return jsonify({"ok": False, "error": "No path provided"}), 400

        root = Path(path_str)
        build_file = find_build_file(root)
        if not build_file:
            return jsonify({"ok": False, "error": "No Gradle build file found"}), 400

        code, out = run_gradle_build(root)

        Path("logs").mkdir(exist_ok=True)
        Path("prompts").mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        out_file = Path("logs") / f"build_output_{ts}.log"
        out_file.write_text(out, encoding="utf-8", errors="ignore")
        PROJECT_STATE["last_run_output_file"] = str(out_file)

        repo_url = PROJECT_STATE.get("repo_url") or detect_repo_url(root)

        if code == 0:
            resp = jsonify({"ok": True, "result": "success", "log_file": str(out_file), "message": "Build succeeded. Application will stop."})
            threading.Timer(1.0, shutdown_flask).start()
            return resp

        summary = extract_error_summary(out)
        repo_note = repo_url if repo_url else "<ADD_YOUR_REPOSITORY_URL_HERE>"
        prompt = f"""===== PROMPT =====
You are a senior Gradle/Java build doctor for Minecraft plugins.

Context:
- Stack: Java 21, Gradle, Paper/Velocity
- Goal: Identify primary cause(s) of the failed build and propose the minimal fix.
- Constraints: Prefer concise reasoning and concrete diffs/commands.

Repository (read files here): {repo_note}

Below is the exact Gradle build output (stdout+stderr). Please analyze and respond with:
1) Root cause(s)
2) Minimal code/config diffs
3) Exact commands to verify the fix

---
===== FULL BUILD OUTPUT (verbatim) =====
{out}

---
===== ERROR SUMMARY (tail) =====
{summary}
"""
        prompt_file = Path("prompts") / f"chatgpt_prompt_{ts}.txt"
        prompt_file.write_text(prompt, encoding="utf-8")
        PROJECT_STATE["last_prompt_file"] = str(prompt_file)

        return jsonify({
            "ok": True,
            "result": "failure",
            "log_file": str(out_file),
            "prompt_file": str(prompt_file),
            "prompt_text": prompt,
            "repo_url": repo_url
        })
    except Exception as e:
        return jsonify({"ok": False, "error": f"/api/run failed: {e.__class__.__name__}: {e}"}), 500

@app.route("/api/get_last_prompt", methods=["GET"])
def api_get_last_prompt():
    path = PROJECT_STATE.get("last_prompt_file")
    if not path or not Path(path).exists():
        return jsonify({"ok": False, "error": "No prompt available"}), 404
    text = Path(path).read_text(encoding="utf-8", errors="ignore")
    return jsonify({"ok": True, "prompt_file": path, "prompt_text": text})

def _extract_openai_output_text(resp_json: dict) -> str:
    # Responses API: prefer unified output_text if present
    if "output_text" in resp_json and isinstance(resp_json["output_text"], str):
        return resp_json["output_text"]
    # Fallback: traverse output -> content -> text
    try:
        output = resp_json.get("output", [])
        parts = []
        for item in output:
            for c in item.get("content", []):
                if c.get("type") == "output_text" and "text" in c:
                    parts.append(c["text"])
        return "\n".join(parts) if parts else json.dumps(resp_json)
    except Exception:
        return json.dumps(resp_json)

@app.route("/api/send_prompt", methods=["POST"])
def api_send_prompt():
    try:
        data = request.get_json(force=True, silent=True) or {}
        prompt_path = data.get("prompt_path") or PROJECT_STATE.get("last_prompt_file")
        model = data.get("model") or os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")
        if not prompt_path or not Path(prompt_path).exists():
            return jsonify({"ok": False, "error": "Prompt file not found"}), 400
        prompt_text = Path(prompt_path).read_text(encoding="utf-8", errors="ignore")

        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            return jsonify({"ok": False, "error": "OPENAI_API_KEY not set"}), 400

        import requests  # local import to keep startup clean

        url = "https://api.openai.com/v1/responses"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": model,
            "input": prompt_text,
        }
        r = requests.post(url, headers=headers, data=json.dumps(body), timeout=120)
        if r.status_code >= 300:
            return jsonify({"ok": False, "error": f"OpenAI HTTP {r.status_code}", "details": r.text}), 502

        resp_json = r.json()
        output_text = _extract_openai_output_text(resp_json)

        # Save raw response
        Path("prompts").mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        resp_path = Path("prompts") / f"chatgpt_response_{ts}.json"
        resp_path.write_text(json.dumps(resp_json, ensure_ascii=False, indent=2), encoding="utf-8")

        return jsonify({"ok": True, "model": model, "response_text": output_text, "response_file": str(resp_path)})
    except Exception as e:
        return jsonify({"ok": False, "error": f"/api/send_prompt failed: {e.__class__.__name__}: {e}"}), 500

@app.route("/download/log/<path:filename>", methods=["GET"])
def download_log(filename):
    return send_from_directory("logs", filename, as_attachment=True)

@app.route("/download/prompt/<path:filename>", methods=["GET"])
def download_prompt(filename):
    return send_from_directory("prompts", filename, as_attachment=True)

if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    app.run(host=host, port=port, debug=True)
