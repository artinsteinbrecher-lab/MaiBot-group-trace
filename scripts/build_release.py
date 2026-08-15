"""构建可直接放入 MaiBot plugins 目录的发行包。"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import json


ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
PACKAGE_DIR_NAME = "MaiBot_group_trace"

INCLUDED_ROOT_FILES = {
    "_manifest.json",
    "plugin.py",
    "config_models.py",
    "config.example.toml",
    "README.md",
    "CHANGELOG.md",
    "LICENSE",
    "CONTRIBUTING.md",
    "SUPPORT.md",
    "SECURITY.md",
    "CODE_OF_CONDUCT.md",
    "requirements.txt",
}
INCLUDED_DIRECTORIES = {"core", "docs", "i18n"}


def main() -> None:
    manifest = json.loads((ROOT / "_manifest.json").read_text(encoding="utf-8"))
    version = str(manifest["version"])
    archive_path = DIST / f"MaiBot-group-trace-v{version}.zip"
    files = collect_release_files()
    validate_release_files(files)
    DIST.mkdir(parents=True, exist_ok=True)
    if archive_path.exists():
        archive_path.unlink()
    with ZipFile(archive_path, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for source in files:
            relative = source.relative_to(ROOT)
            archive.write(source, Path(PACKAGE_DIR_NAME) / relative)
    digest = sha256(archive_path.read_bytes()).hexdigest()
    (DIST / "SHA256SUMS.txt").write_text(f"{digest}  {archive_path.name}\n", encoding="utf-8")
    print(f"已生成 {archive_path}")
    print(f"SHA256 {digest}")


def collect_release_files() -> list[Path]:
    files = [ROOT / name for name in sorted(INCLUDED_ROOT_FILES)]
    for directory_name in sorted(INCLUDED_DIRECTORIES):
        directory = ROOT / directory_name
        files.extend(
            path
            for path in sorted(directory.rglob("*"))
            if path.is_file() and "__pycache__" not in path.parts and path.suffix not in {".pyc", ".pyo"}
        )
    return files


def validate_release_files(files: list[Path]) -> None:
    missing = [str(path.relative_to(ROOT)) for path in files if not path.is_file()]
    if missing:
        raise FileNotFoundError("发行文件缺失：" + "、".join(missing))
    relative_names = {str(path.relative_to(ROOT)).replace("\\", "/") for path in files}
    for required in {"_manifest.json", "plugin.py", "config_models.py", "README.md"}:
        if required not in relative_names:
            raise RuntimeError(f"发行包缺少必要文件：{required}")
    forbidden = {"config.toml", "group_trace.sqlite3"}
    leaked = [name for name in relative_names if Path(name).name in forbidden]
    if leaked:
        raise RuntimeError("发行包包含运行时私有文件：" + "、".join(leaked))


if __name__ == "__main__":
    main()
