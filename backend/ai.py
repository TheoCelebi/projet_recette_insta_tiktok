"""Appel à Claude avec sortie JSON structurée, via l'API ou via Claude Code.

- Si ANTHROPIC_API_KEY contient une clé `sk-ant-…` : API Anthropic (crédits API).
- Sinon : CLI `claude -p` de Claude Code, qui utilise l'abonnement Claude.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
from typing import TypeVar

import anthropic
from pydantic import BaseModel

MODELS = {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5"}

T = TypeVar("T", bound=BaseModel)


def structured(system: str, content: list[dict] | str, schema: type[T], effort: str = "medium",
               client: anthropic.Anthropic | None = None, model: str = "opus") -> T:
    """Envoie `content` à Claude et renvoie une instance validée de `schema`."""
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    if client is None and not os.environ.get("ANTHROPIC_API_KEY", "").startswith("sk-ant-"):
        return _with_claude_code(system, content, schema, effort, model)
    return _with_api(system, content, schema, effort, client or anthropic.Anthropic(), model)


def _with_claude_code(system: str, content: list[dict], schema: type[T], effort: str, model: str) -> T:
    cli = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
    if not os.path.exists(cli):
        raise RuntimeError("Ni clé API Anthropic ni Claude Code installé.")
    message = {"type": "user", "message": {"role": "user", "content": content}}
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    errfile = tempfile.TemporaryFile(mode="w+")  # fichier plutôt que tube : pas de blocage si stderr est bavard
    proc = subprocess.Popen(
        [cli, "-p", "--model", model, "--effort", effort,
         "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
         "--no-session-persistence", "--tools", "", "--system-prompt", system,
         "--json-schema", json.dumps(schema.model_json_schema())],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errfile, text=True, env=env,
    )
    # Garde-fou : on coupe au bout de 15 min (le compteur s'arrête pendant la veille du Mac)
    watchdog = threading.Timer(900, proc.kill)
    watchdog.start()
    result = None
    try:
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.close()
        # On lit les événements au fil de l'eau et on s'arrête dès le résultat final,
        # sans attendre que le processus se ferme de lui-même.
        for line in proc.stdout:
            if line.startswith('{"') and '"type":"result"' in line:
                result = json.loads(line)
                break
    finally:
        watchdog.cancel()
        if proc.poll() is None:
            proc.terminate()
        proc.wait()
        errfile.seek(0)
        stderr = errfile.read()
        errfile.close()
    if result is None:
        raise RuntimeError(f"Claude Code n'a rien renvoyé : {stderr.strip()[:300] or 'délai dépassé'}")
    if result.get("is_error"):
        msg = result.get("result", "erreur inconnue")
        if "authenticate" in msg.lower() or "login" in msg.lower():
            msg = "Claude Code n'est pas connecté : lance `claude` dans un terminal puis /login."
        raise RuntimeError(msg)
    data = result.get("structured_output")
    if data is None:
        data = json.loads(result.get("result", "").strip().removeprefix("```json").removesuffix("```"))
    return schema.model_validate(data)


def _with_api(system: str, content: list[dict], schema: type[T], effort: str,
              client: anthropic.Anthropic, model: str) -> T:
    response = client.beta.messages.parse(
        model=MODELS[model],
        max_tokens=16000,
        system=system,
        messages=[{"role": "user", "content": content}],
        output_config={"effort": effort},
        output_format=schema,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude a refusé de traiter cette demande.")
    if response.parsed_output is None:
        raise RuntimeError(f"Réponse inexploitable (stop_reason={response.stop_reason}).")
    return response.parsed_output
