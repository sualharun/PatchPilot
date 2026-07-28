import subprocess

from agent.config import SandboxConfig
from agent.sandbox import DockerSandbox
from agent.tools import RepoTools


def test_apply_patch_updates_file(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    target = tmp_path / "example.py"
    target.write_text('print("old")\n', encoding="utf-8")
    patch = """diff --git a/example.py b/example.py
--- a/example.py
+++ b/example.py
@@ -1 +1 @@
-print("old")
+print("new")
"""

    RepoTools(tmp_path, DockerSandbox(SandboxConfig())).apply_patch(patch)

    assert target.read_text(encoding="utf-8") == 'print("new")\n'
