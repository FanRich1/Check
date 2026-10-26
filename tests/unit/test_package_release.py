import subprocess

from scripts import package_release


def test_zip_internal_integrity_cannot_replace_current_source_comparison(tmp_path, monkeypatch):
    root = tmp_path / "source"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-c", "user.name=Release Test", "-c", "user.email=test@example.invalid",
                    "commit", "-q", "--allow-empty", "-m", "Authored release fixture"], cwd=root, check=True)
    app = root / "src/shipping_billing/app.py"
    app.parent.mkdir(parents=True)
    app.write_text("VERSION = 1\n", encoding="utf-8")
    (root / ".env").write_text("PRIVATE_CONFIGURATION=excluded\n", encoding="utf-8")
    private = root / "data/local/business.txt"
    private.parent.mkdir(parents=True)
    private.write_text("PRIVATE AUTHORED FIXTURE", encoding="utf-8")
    monkeypatch.setattr(package_release, "ROOT", root)
    archive = tmp_path / "release.zip"
    package_release.build(archive, "authored-v1")
    assert package_release.compare_source(archive, root)["passed"]
    assert [f["path"] for f in package_release.verify(archive)["files"]] == ["src/shipping_billing/app.py"]
    app.write_text("VERSION = 2\n", encoding="utf-8")
    extra = app.with_name("new.py")
    extra.write_text("NEW = True\n", encoding="utf-8")
    assert package_release.verify(archive)["version"] == "authored-v1"
    comparison = package_release.compare_source(archive, root)
    assert not comparison["passed"]
    assert comparison["changed"] == ["src/shipping_billing/app.py"]
    assert comparison["missing"] == ["src/shipping_billing/new.py"]
