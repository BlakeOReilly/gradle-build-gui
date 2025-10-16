import os
import sys
import json
import base64
import subprocess
import shutil
from pathlib import Path
from datetime import datetime
from flask import Flask, render_template, request, jsonify
from jsonschema import validate, Draft7Validator, ValidationError

# ---- Configuration ----
APP_ROOT = Path(__file__).parent.resolve()
LOGS_DIR = (APP_ROOT / "logs"); LOGS_DIR.mkdir(exist_ok=True, parents=True)
PROMPTS_DIR = (APP_ROOT / "prompts"); PROMPTS_DIR.mkdir(exist_ok=True, parents=True)
STATIC_DIR = (APP_ROOT / "static"); STATIC_DIR.mkdir(exist_ok=True, parents=True)
TEMPLATES_DIR = (APP_ROOT / "templates"); TEMPLATES_DIR.mkdir(exist_ok=True, parents=True)

JSON_SCHEMA_V1 = {
    "type": "object",
    "required": ["version", "intent", "changes"],
    "properties": {
        "version": {"type": "string", "enum": ["1"]},
        "intent": {"type": "string", "enum": ["apply_fixes"]},
        "changes": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["action", "path"],
                "properties": {
                    "action": {"type": "string", "enum": ["write", "create", "delete", "move"]},
                    "path": {"type": "string"},
                    "encoding": {"type": "string", "enum": ["utf-8", "base64"]},
                    "content": {"type": "string"},
                    "from": {"type": "string"},
                    "to": {"type": "string"}
                },
                "allOf": [
                    {"if": {"properties": {"action": {"const": "write"}}}, "then": {"required": ["content"]}},
                    {"if": {"properties": {"action": {"const": "create"}}}, "then": {"required": ["content"]}},
                    {"if": {"properties": {"action": {"const": "move"}}}, "then": {"required": ["from", "to"]}}
                ]
            }
        },
        "commands": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "string"}
    },
    "additionalProperties": False
}

# Optional: allow users to disable applying changes for safety
APPLY_CHANGES = os.environ.get("APPLY_CHANGES", "1") not in ("0", "false", "False")

app = Flask(__name__, static_folder=str(STATIC_DIR), template_folder=str(TEMPLATES_DIR))

def is_gradle_project(root: Path) -> bool:
    return (root / "build.gradle").exists() or (root / "build.gradle.kts").exists()

def detect_repo_url(root: Path) -> str | None:
    cfg = root / ".git" / "config"
    if not cfg.exists():
        return None
    try:
        txt = cfg.read_text(errors="replace")
        for line in txt.splitlines():
            line = line.strip()
            if line.startswith("url ="):
                return line.split("=", 1)[1].strip()
    except Exception:
        return None
    return None

def run_gradle(root: Path) -> tuple[int, str]:
    # Prefer wrapper
    gradlew = "gradlew.bat" if os.name == "nt" else "gradlew"
    cmd = [str(root / gradlew), "clean", "build"] if (root / gradlew).exists() else ["gradle", "clean", "build"]
    try:
        proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True)
        out = proc.stdout + "\n\n" + proc.stderr
        return proc.returncode, out
    except FileNotFoundError:
        return 127, "Gradle not found. Ensure gradle is on PATH or include a gradle wrapper.\n"
    except Exception as e:
        return 1, f"Unexpected error: {e}\n"

def write_file(target: Path, content: str, encoding: str = "utf-8"):
    target.parent.mkdir(parents=True, exist_ok=True)
    if encoding == "base64":
        data = base64.b64decode(content)
        target.write_bytes(data)
    else:
        target.write_text(content, encoding="utf-8", newline="\n")

def apply_patch_spec(root: Path, spec: dict) -> list[str]:
    actions_log = []
    validator = Draft7Validator(JSON_SCHEMA_V1)
    errors = sorted(validator.iter_errors(spec), key=lambda e: e.path)
    if errors:
        raise ValidationError("\n".join([f"{'/'.join(map(str, e.path))}: {e.message}" for e in errors]))

    for change in spec["changes"]:
        action = change["action"]
        path = change["path"]
        target = (root / path).resolve()
        if not str(target).startswith(str(root.resolve())):
            raise ValueError(f"Unsafe path outside project: {path}")

        if action in ("write", "create"):
            content = change["content"]
            encoding = change.get("encoding", "utf-8")
            if APPLY_CHANGES:
                write_file(target, content, encoding)
            actions_log.append(f"{action.upper()} {path}")
        elif action == "delete":
            if APPLY_CHANGES and target.exists():
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            actions_log.append(f"DELETE {path}")
        elif action == "move":
            src = (root / change["from"]).resolve()
            dst = (root / change["to"]).resolve()
            if not str(src).startswith(str(root.resolve())) or not str(dst).startswith(str(root.resolve())):
                raise ValueError("Unsafe move path outside project")
            if APPLY_CHANGES:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
            actions_log.append(f"MOVE {change['from']} -> {change['to']}")
        else:
            raise ValueError(f"Unknown action: {action}")
    return actions_log

def build_prompt(root: Path, build_output: str, repo_url: str | None) -> str:
    header = (
        "You are a code fix generator for a local Gradle Java project. "
        "Output ONLY a single JSON object that matches this schema exactly: "
        "{version:string,intent:\"apply_fixes\",changes:[{action:\"write|create|delete|move\",path:string,encoding?:\"utf-8|base64\",content?:string,from?:string,to?:string}],commands?:string[],notes?:string}. "
        "Never include Markdown, code fences, or explanations. All modified or new files MUST be full-file contents. "
        "If no changes are needed, output {\"version\":\"1\",\"intent\":\"apply_fixes\",\"changes\":[]}."
    )
    lines = [
        header,
        "",
        f"Project root: {root}",
        f"Gradle file present: {is_gradle_project(root)}",
        f"Repository URL: {repo_url or 'unknown'}",
        "Constraints: Java 21 if present, minimal invasive changes, keep API surface stable.",
        "",
        "==== LAST BUILD OUTPUT BEGIN ====",
        build_output.strip()[:300000],  # cap very large logs
        "==== LAST BUILD OUTPUT END ====",
    ]
    return "\n".join(lines)

def call_openai_json(prompt: str) -> dict:
    # Uses OpenAI "Responses" API with JSON response enforcement.
    # Requires: pip install openai>=1.40
    from openai import OpenAI
    client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
    resp = client.responses.create(
        model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        input=[
            {"role": "system", "content": "You are a strict JSON patch generator."},
            {"role": "user", "content": prompt}
        ],
        response_format={"type": "json_object"},
        temperature=0
    )
    # Support text or JSON output depending on SDK behavior
    if resp.output and hasattr(resp.output[0], "content") and resp.output[0].content:
        raw = resp.output_text
    else:
        # Fallback: attempt to read from top-level
        raw = getattr(resp, "output_text", None) or ""
    data = json.loads(raw)
    return data

@app.route("/", methods=["GET"])
def index():
    project_root = request.args.get("root", "")
    exists = is_gradle_project(Path(project_root)) if project_root else False
    return render_template("index.html", project_root=project_root, has_build_file=exists)

@app.route("/api/check", methods=["POST"])
def api_check():
    data = request.get_json(force=True)
    root = Path(data.get("project_root", "")).resolve()
    return jsonify({"ok": True, "has_build_file": is_gradle_project(root)})

@app.route("/api/run", methods=["POST"])
def api_run():
    data = request.get_json(force=True)
    project_root = Path(data.get("project_root", "")).resolve()
    if not project_root.exists():
        return jsonify({"ok": False, "error": "Project root does not exist."}), 400
    if not is_gradle_project(project_root):
        return jsonify({"ok": False, "error": "No build.gradle or build.gradle.kts in project root."}), 400

    rc, out = run_gradle(project_root)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = LOGS_DIR / f"build-{ts}.log"
    log_path.write_text(out, encoding="utf-8", newline="\n")

    if rc == 0:
        return jsonify({"ok": True, "status": "success", "log_file": str(log_path)})

    # Build failed -> craft prompt and call OpenAI
    repo_url = detect_repo_url(project_root)
    prompt = build_prompt(project_root, out, repo_url)
    prompt_path = PROMPTS_DIR / f"prompt-{ts}.txt"
    prompt_path.write_text(prompt, encoding="utf-8", newline="\n")

    try:
        spec = call_openai_json(prompt)
    except Exception as e:
        return jsonify({
            "ok": False,
            "status": "openai_error",
            "error": str(e),
            "prompt_file": str(prompt_path),
            "log_file": str(log_path)
        }), 500

    # Validate and optionally apply
    try:
        actions_log = []
        if APPLY_CHANGES:
            actions_log = apply_patch_spec(project_root, spec)
        else:
            # Validate only
            validate(instance=spec, schema=JSON_SCHEMA_V1)
        # Optional commands execution after apply
        cmd_outs = []
        for cmd in spec.get("commands", []):
            try:
                proc = subprocess.run(cmd, cwd=project_root, shell=True, capture_output=True, text=True)
                cmd_outs.append({"cmd": cmd, "returncode": proc.returncode, "output": (proc.stdout + "\n" + proc.stderr)})
            except Exception as e:
                cmd_outs.append({"cmd": cmd, "returncode": 1, "output": f"Error: {e}"})
        return jsonify({
            "ok": True,
            "status": "patch_applied" if APPLY_CHANGES else "validated_only",
            "actions": actions_log,
            "spec": spec,
            "prompt_file": str(prompt_path),
            "log_file": str(log_path),
            "commands": cmd_outs
        })
    except ValidationError as ve:
        return jsonify({"ok": False, "status": "schema_error", "error": str(ve), "spec": spec}), 400
    except Exception as e:
        return jsonify({"ok": False, "status": "apply_error", "error": str(e), "spec": spec}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="127.0.0.1", port=port, debug=True)
