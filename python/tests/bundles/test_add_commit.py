"""`archi add` commits archi.lock and profiles/ with the deployment change.

okg refuses to publish while they are uncommitted ("repository provenance inputs are
not committed"); archi.yaml stays the user's to commit.
"""
import subprocess

from archi.bundles.lock import _git_commit


def test_archi_add_commits_the_lock_and_profiles_beside_the_deployment(tmp_path):
    def git(*args):
        cmd = ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@e.invalid", *args]
        return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout

    files = ("archi.yaml", "archi.lock", "profiles/p/x.yaml", "deployments/gp/deployment.yaml")
    for name in files:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("1\n")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    for name in files:
        (tmp_path / name).write_text("2\n")
    (tmp_path / "profiles/p/new.yaml").write_text("n\n")

    assert _git_commit(tmp_path / "deployments" / "gp", "archi add x", tmp_path) is not None
    assert git("status", "--short").split() == ["M", "archi.yaml"]
